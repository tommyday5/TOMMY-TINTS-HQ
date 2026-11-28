"""Owner-approved statement evidence register, separate from all cash ledgers.

Explicit mapped CSV and statement balances are owner-supplied, not authenticated
bank evidence. No allocation, payment, provider call, or canonical cash import.
"""
import csv
import hashlib
import io
import json
import secrets
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from flask import abort, render_template, request, session
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_reconciliation_contracts import normalise_monzo_rows, preview_bank_rollforward
from hq_transaction_preview import preview_export, _unique_json_object


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=True).encode()).hexdigest()


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS finance_statement_evidence (
        id TEXT PRIMARY KEY, account_ref TEXT NOT NULL, start_day TEXT NOT NULL,
        end_day TEXT NOT NULL, opening INTEGER NOT NULL, closing INTEGER NOT NULL,
        source_digest TEXT NOT NULL, manifest TEXT NOT NULL, created_at TEXT NOT NULL,
        actor TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS finance_statement_events (
        batch_id TEXT NOT NULL, evidence_id TEXT NOT NULL, amount INTEGER NOT NULL,
        occurred_at TEXT NOT NULL, PRIMARY KEY(batch_id,evidence_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS finance_statement_withdrawals (
        batch_id TEXT PRIMARY KEY, reason TEXT NOT NULL, actor TEXT NOT NULL,
        created_at TEXT NOT NULL)""")


def prepare(csv_text, mapping, account, start, end, opening, closing_balance):
    start_day, end_day = date.fromisoformat(start), date.fromisoformat(end)
    if start_day.isoformat() != start or end_day.isoformat() != end or start_day > end_day:
        raise ValueError("Invalid statement period.")
    if (end_day - start_day).days > 366:
        raise ValueError("Use a statement period of at most 367 days.")
    result = preview_export("monzo", csv_text, mapping=mapping, scope_ref=account)
    if not result["canonical_preview_valid"] or result["exact_duplicate_rows"]:
        raise ValueError("Explicit complete mapping and no duplicate rows required.")
    reader = csv.DictReader(io.StringIO(csv_text.removeprefix("\ufeff"), newline=""), strict=True)
    rows = []
    for row in reader:
        rows.append({key: int(row[column]) if key == "amount_pence" else row[column]
                     for key, column in mapping.items()})
    canonical = normalise_monzo_rows(rows, account_ref=account)
    events = canonical["events"]
    for event in events:
        day = datetime.fromisoformat(event["occurred_at"]).astimezone(ZoneInfo("Europe/London")).date()
        if not start_day <= day <= end_day:
            raise ValueError("A transaction falls outside the declared UK statement period.")
    if type(opening) is not int or type(closing_balance) is not int:
        raise ValueError("Use exact integer pence for both balances.")
    roll = preview_bank_rollforward(canonical, opening_pence=opening, closing_pence=closing_balance)
    manifest = dict(account=account, start=start, end=end, opening=opening,
                    closing=closing_balance, mapping=mapping,
                    source_digest=hashlib.sha256(csv_text.encode("utf-8")).hexdigest(),
                    event_digest=canonical["canonical_event_digest"], count=len(events),
                    rollforward=roll)
    return dict(manifest=manifest, stamp=digest(manifest), events=events)


def save(preview, approval, actor):
    if not secrets.compare_digest(preview["stamp"], approval or ""):
        raise ValueError("Preview changed. Review the exact statement again.")
    m = preview["manifest"]
    if not m["rollforward"]["arithmetically_consistent"]:
        raise ValueError("Opening plus mapped movement does not equal closing balance.")
    with closing(connect()) as db, db:
        schema(db)
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT id FROM finance_statement_evidence WHERE id=?",
                              (approval,)).fetchone()
        if existing:
            if db.execute("SELECT 1 FROM finance_statement_withdrawals WHERE batch_id=?",
                          (approval,)).fetchone():
                raise ValueError("This exact statement was withdrawn; review its history.")
            return approval
        overlap = db.execute("""SELECT 1 FROM finance_statement_evidence s
            LEFT JOIN finance_statement_withdrawals w ON w.batch_id=s.id
            WHERE w.batch_id IS NULL AND s.account_ref=? AND s.start_day<=?
            AND s.end_day>=? LIMIT 1""", (m["account"], m["end"], m["start"])).fetchone()
        if overlap:
            raise ValueError("An active statement overlaps this account period.")
        for event in preview["events"]:
            previous = db.execute("""SELECT e.amount,e.occurred_at,w.batch_id AS withdrawn
                FROM finance_statement_events e LEFT JOIN finance_statement_withdrawals w
                ON w.batch_id=e.batch_id WHERE e.evidence_id=?""",
                (event["evidence_id"],)).fetchall()
            if any(r["withdrawn"] is None for r in previous):
                raise ValueError("A transaction is already present in active statement evidence.")
            if any(r["amount"] != event["amount_pence"] or
                   r["occurred_at"] != event["occurred_at"] for r in previous):
                raise ValueError("Conflicting historical transaction evidence requires investigation.")
        stamp = datetime.now(timezone.utc).isoformat()
        db.execute("INSERT INTO finance_statement_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (approval, m["account"], m["start"], m["end"], m["opening"],
                    m["closing"], m["source_digest"], json.dumps(m,sort_keys=True), stamp, actor))
        db.executemany("INSERT INTO finance_statement_events VALUES(?,?,?,?)",
                       [(approval,e["evidence_id"],e["amount_pence"],e["occurred_at"])
                        for e in preview["events"]])
    return approval


def withdraw(batch, reason, actor):
    reason = reason.strip()
    if not 1 <= len(reason) <= 500:
        raise ValueError("Provide a withdrawal reason.")
    with closing(connect()) as db, db:
        schema(db)
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM finance_statement_evidence WHERE id=?", (batch,)).fetchone():
            raise ValueError("Statement not found.")
        old = db.execute("SELECT reason FROM finance_statement_withdrawals WHERE batch_id=?",
                         (batch,)).fetchone()
        if old:
            if old["reason"] != reason:
                raise ValueError("Statement already withdrawn; original reason retained.")
            return
        db.execute("INSERT INTO finance_statement_withdrawals VALUES(?,?,?,?)",
                   (batch,reason,actor,datetime.now(timezone.utc).isoformat()))


def listing():
    with closing(connect()) as db, db:
        schema(db)
        return [dict(r) for r in db.execute("""SELECT s.*,w.reason AS withdrawal_reason
            FROM finance_statement_evidence s LEFT JOIN finance_statement_withdrawals w
            ON w.batch_id=s.id ORDER BY s.created_at DESC LIMIT 100""")]


def register(bp):
    @bp.route("/hq/finance/statement-evidence", methods=["GET","POST"])
    @required
    def statement_evidence():
        if not storage_ready():
            abort(503)
        form = {}
        preview = None
        notice = None
        error = None
        status = 200
        if request.method == "POST":
            request.max_content_length = 160000
            if not secrets.compare_digest(request.form.get("csrf",""),session.get("csrf","not-set")):
                abort(403)
            try:
                form = request.form.to_dict()
                actor = session.get("google_sub")
                if not actor:
                    abort(403)
                action = form.get("action")
                if action == "withdraw":
                    if form.get("confirm") != "yes":
                        raise ValueError("Explicit withdrawal confirmation required.")
                    withdraw(form.get("batch",""),form.get("reason",""),actor)
                    notice = "Statement withdrawn; evidence and reason retained."
                    form = {}
                elif action in ("preview","save"):
                    mapping = json.loads(form.get("mapping",""),object_pairs_hook=_unique_json_object)
                    if not isinstance(mapping,dict) or set(mapping) != {"transaction_id","amount_pence","currency","booked_at"}:
                        raise ValueError("Supply the four exact column mappings.")
                    preview = prepare(form.get("csv_text",""),mapping,form.get("account",""),
                                      form.get("start",""),form.get("end",""),
                                      int(form.get("opening","")),int(form.get("closing","")))
                    if action == "save":
                        if form.get("confirm") != "yes":
                            raise ValueError("Explicit statement evidence approval required.")
                        save(preview,form.get("stamp",""),actor)
                        notice = "Owner-supplied statement evidence saved; no bank verification or allocation."
                        form, preview = {}, None
                else:
                    raise ValueError("Unknown action.")
            except (ValueError,TypeError,KeyError,OverflowError,csv.Error):
                error = "Statement review not saved. Check mapping, period, exact balances, overlap, approval and duplicates."
                status = 400
            except (sqlite3.Error,OSError):
                error = "Persistent storage unavailable; no successful save confirmed."
                status = 503
        try:
            rows = listing()
        except (sqlite3.Error,OSError):
            rows = []
            status = 503
            error = "Statement register unavailable."
        return render_template("finance_statement_evidence.html", form=form, preview=preview,
                               rows=rows, notice=notice,error=error,csrf=session.get("csrf","")), status, {"Cache-Control":"no-store"}
