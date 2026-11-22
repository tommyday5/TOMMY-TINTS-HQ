"""Manual, revisioned HQ receivables. An invoice is never a verified receipt."""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from datetime import date, datetime, timezone

from flask import jsonify, render_template, request, session
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_core_records import _schema

HEX = re.compile(r"[0-9a-f]{32}\Z")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,39}\Z")
STATES = ("draft", "issued", "disputed", "void")
TRANSITIONS = {
    "draft": {"draft", "issued", "void"},
    "issued": {"issued", "disputed", "void"},
    "disputed": {"disputed", "issued", "void"},
    "void": {"void"},
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def tables(db):
    _schema(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_receivables (
        id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        work_id TEXT REFERENCES hq_work_records(id),
        invoice_ref TEXT NOT NULL UNIQUE COLLATE NOCASE,
        issue_date TEXT NOT NULL, due_date TEXT NOT NULL,
        amount_pence INTEGER NOT NULL CHECK(amount_pence > 0),
        status TEXT NOT NULL CHECK(status IN ('draft','issued','disputed','void')),
        memo TEXT NOT NULL, archived INTEGER NOT NULL CHECK(archived IN (0,1)),
        revision INTEGER NOT NULL CHECK(revision > 0),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_receivable_events (
        event_id TEXT PRIMARY KEY,
        record_id TEXT NOT NULL REFERENCES hq_receivables(id),
        revision INTEGER NOT NULL, snapshot TEXT NOT NULL, occurred_at TEXT NOT NULL,
        UNIQUE(record_id,revision)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_receivable_operations (
        operation_id TEXT PRIMARY KEY,
        record_id TEXT NOT NULL REFERENCES hq_receivables(id),
        fingerprint TEXT NOT NULL, result_revision INTEGER NOT NULL
    )""")


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("A valid ISO date is required")
    date.fromisoformat(value)
    return value


def _fields(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid form")
    record_id = data.get("id")
    operation_id = data.get("operation_id")
    customer_id = data.get("customer_id")
    work_id = data.get("work_id") or None
    if any(not isinstance(v, str) or not HEX.fullmatch(v) for v in (record_id, operation_id, customer_id)):
        raise ValueError("Invalid record, customer or submission ID")
    if work_id is not None and (not isinstance(work_id, str) or not HEX.fullmatch(work_id)):
        raise ValueError("Invalid linked work ID")
    revision = data.get("revision")
    if type(revision) is not int or not 0 <= revision < 2**63 - 1:
        raise ValueError("Valid revision required")
    invoice_ref = data.get("invoice_ref")
    if not isinstance(invoice_ref, str) or not REF.fullmatch(invoice_ref):
        raise ValueError("Enter a reference using letters, numbers, . _ / or -")
    issue_date = _day(data.get("issue_date"))
    due_date = _day(data.get("due_date"))
    if due_date < issue_date:
        raise ValueError("Due date cannot precede issue date")
    amount = data.get("amount_pence")
    if type(amount) is not int or not 1 <= amount <= 1000000000:
        raise ValueError("Enter a positive invoice amount in pence")
    status = data.get("status")
    if status not in STATES:
        raise ValueError("Invalid invoice lifecycle")
    archived = data.get("archived")
    if type(archived) is not bool:
        raise ValueError("Archive flag must be true or false")
    memo = data.get("memo", "")
    if not isinstance(memo, str) or len(memo.strip()) > 500:
        raise ValueError("Notes must not exceed 500 characters")
    return dict(id=record_id, operation_id=operation_id, customer_id=customer_id,
                work_id=work_id, revision=revision, invoice_ref=invoice_ref,
                issue_date=issue_date, due_date=due_date, amount_pence=amount,
                status=status, archived=archived, memo=memo.strip())


def save(data):
    if not storage_ready():
        raise ValueError("Persistent HQ storage is required")
    fields = _fields(data)
    op = fields.pop("operation_id")
    fingerprint = hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    now = _now()
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT record_id,fingerprint,result_revision FROM hq_receivable_operations WHERE operation_id=?", (op,)).fetchone()
        if prior:
            if prior["record_id"] != fields["id"] or not hmac.compare_digest(prior["fingerprint"], fingerprint):
                raise ValueError("This submission ID was already used for different information")
            return prior["result_revision"]
        existing = db.execute("SELECT * FROM hq_receivables WHERE id=?", (fields["id"],)).fetchone()
        if (existing and existing["revision"] != fields["revision"]) or (not existing and fields["revision"] != 0):
            raise StaleRevision("Record changed elsewhere. Reload before saving.")
        if existing and fields["status"] not in TRANSITIONS[existing["status"]]:
            raise ValueError("That invoice status change is not allowed")
        customer = db.execute("SELECT id FROM hq_customers WHERE id=?", (fields["customer_id"],)).fetchone()
        if not customer:
            raise ValueError("Select an existing HQ customer")
        if fields["work_id"]:
            work = db.execute("SELECT customer_id FROM hq_work_records WHERE id=?", (fields["work_id"],)).fetchone()
            if not work or work["customer_id"] != fields["customer_id"]:
                raise ValueError("Linked work belongs to a different customer")
        if existing and existing["customer_id"] != fields["customer_id"]:
            raise ValueError("To preserve invoice evidence, the customer cannot be changed")
        revision = fields["revision"] + 1
        if existing:
            db.execute("""UPDATE hq_receivables SET work_id=?,invoice_ref=?,issue_date=?,
                due_date=?,amount_pence=?,status=?,memo=?,archived=?,revision=?,updated_at=?
                WHERE id=?""", (fields["work_id"], fields["invoice_ref"],
                fields["issue_date"], fields["due_date"], fields["amount_pence"],
                fields["status"], fields["memo"], int(fields["archived"]), revision, now, fields["id"]))
        else:
            db.execute("""INSERT INTO hq_receivables
                (id,customer_id,work_id,invoice_ref,issue_date,due_date,amount_pence,
                status,memo,archived,revision,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                fields["id"], fields["customer_id"], fields["work_id"], fields["invoice_ref"],
                fields["issue_date"], fields["due_date"], fields["amount_pence"],
                fields["status"], fields["memo"], int(fields["archived"]), revision, now, now))
        snapshot = {**fields, "revision": revision}
        db.execute("INSERT INTO hq_receivable_events VALUES (?,?,?,?,?)", (
            uuid.uuid4().hex, fields["id"], revision,
            json.dumps(snapshot, sort_keys=True), now))
        db.execute("INSERT INTO hq_receivable_operations VALUES (?,?,?,?)", (
            op, fields["id"], fingerprint, revision))
    return revision


class StaleRevision(ValueError):
    pass


def snapshot():
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        tables(db)
        customers = [dict(row) for row in db.execute(
            "SELECT id,name FROM hq_customers ORDER BY name COLLATE NOCASE,id LIMIT 500")]
        works = [dict(row) for row in db.execute("""SELECT w.id,w.customer_id,w.service,w.status
            FROM hq_work_records w ORDER BY w.created_at DESC,w.id DESC LIMIT 500""")]
        rows = [dict(row) for row in db.execute("""SELECT r.*,c.name AS customer_name,
            w.service AS work_service
            FROM hq_receivables r JOIN hq_customers c ON c.id=r.customer_id
            LEFT JOIN hq_work_records w ON w.id=r.work_id
            ORDER BY r.archived,r.updated_at DESC,r.id DESC LIMIT 500""")]
        history = [dict(row) for row in db.execute("""SELECT record_id,revision,snapshot,occurred_at
            FROM hq_receivable_events ORDER BY occurred_at DESC,event_id DESC LIMIT 200""")]
    # No receipts have been reconciled. Face amount must not be reported as cash received,
    # verified outstanding balance, or bank balance.
    return dict(customers=customers, works=works, receivables=rows,
                history=history, receipts_verified=False, outstanding_verified=False,
                received_income=None, source="HQ owner-entered invoices; no bank reconciliation")


def register(bp):
    @bp.get("/hq/finance/receivables")
    @required
    def receivables_page():
        return render_template("hq_receivables.html", csrf=session.get("csrf", "")), 200, {"Cache-Control": "no-store"}

    @bp.route("/api/hq/finance/receivables", methods=["GET", "POST"])
    @required
    def receivables_api():
        if request.method == "GET":
            return jsonify(snapshot()), 200, {"Cache-Control": "no-store"}
        if not secrets.compare_digest(request.headers.get("X-HQ-CSRF", ""), session.get("csrf", "missing")):
            return jsonify(error="Invalid security token"), 403
        if not request.is_json or not request.content_length or request.content_length > 4096:
            return jsonify(error="Invalid JSON request"), 400
        try:
            result = save(request.get_json(silent=True))
            return jsonify(saved=True, revision=result, receipts_verified=False), 200, {"Cache-Control": "no-store"}
        except StaleRevision as exc:
            return jsonify(error=str(exc)), 409
        except sqlite3.IntegrityError:
            return jsonify(error="Invoice reference is already used. No changes saved."), 409
        except (ValueError, TypeError, OverflowError):
            return jsonify(error="Check the customer, work link, reference, amount, dates and current revision."), 400
        except sqlite3.Error:
            return jsonify(error="Receivables storage unavailable; nothing confirmed saved."), 503
