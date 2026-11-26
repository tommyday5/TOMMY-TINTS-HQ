"""Owner-only split classifications of existing manual finance transactions.

A split is a classification overlay, not a new transaction or verified receipt.
Parent transaction revisions and immutable events serialize all edits.
"""
import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from flask import abort, redirect, render_template, request, session, url_for
from customer_request_store import connect, storage_ready
from hq_auth import required

HEX = re.compile(r"[0-9a-f]{32}\Z")
MONEY = re.compile(r"-?[0-9]{1,8}(?:\.[0-9]{1,2})?\Z")
EXTRA = ("refund", "processor_fee", "settlement", "unclassified")


def tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS finance_split_events (
        transaction_id TEXT NOT NULL, revision INTEGER NOT NULL,
        operation TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
        created TEXT NOT NULL, actor TEXT NOT NULL,
        PRIMARY KEY(transaction_id,revision))""")


def latest(db, ref):
    return db.execute("""SELECT * FROM finance_split_events
        WHERE transaction_id=? ORDER BY revision DESC LIMIT 1""", (ref,)).fetchone()


def active(db, ref):
    row = latest(db, ref)
    return bool(row and json.loads(row["payload"])["action"] == "save")


def categories():
    from hq_native_finance import CATEGORIES
    return tuple(dict.fromkeys(CATEGORIES + EXTRA))


def validate_lines(lines, amount):
    if type(lines) is not list or not 2 <= len(lines) <= 5 or not amount:
        raise ValueError("Use two to five nonzero split lines.")
    clean = []
    for item in lines:
        if not isinstance(item, dict):
            raise ValueError("Invalid split line.")
        category, pence = item.get("category"), item.get("pence")
        if category not in categories() or type(pence) is not int or not pence:
            raise ValueError("Choose a category and nonzero amount.")
        if abs(pence) > abs(amount) or (pence > 0) != (amount > 0):
            raise ValueError("Each split must have the same direction as its parent.")
        from hq_native_finance import validate_explicit_direction
        validate_explicit_direction(category,pence)
        clean.append(dict(category=category, pence=pence))
    if sum(x["pence"] for x in clean) != amount:
        raise ValueError("Split amounts must add up exactly to the transaction.")
    return clean


def save(ref, revision, action, lines, actor):
    from hq_native_finance import schema, audit, transaction_archived
    from hq_work_progress import _actor_fingerprint
    if not storage_ready():
        raise ValueError("Persistent HQ storage is required.")
    if not HEX.fullmatch(ref) or type(revision) is not int or not 0 <= revision < 2**63-1:
        raise ValueError("Invalid transaction or revision.")
    if action not in ("save", "withdraw"):
        raise ValueError("Invalid action.")
    payload = dict(action=action, lines=lines if action == "save" else [])
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    actor = _actor_fingerprint(actor)
    op = hashlib.sha256(json.dumps([ref,revision,encoded,actor]).encode()).hexdigest()
    with closing(connect()) as db, db:
        schema(db)
        tables(db)
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT revision FROM finance_split_events WHERE operation=?", (op,)).fetchone()
        if previous:
            return previous["revision"]
        parent = db.execute("SELECT * FROM finance_transactions WHERE id=?", (ref,)).fetchone()
        if not parent:
            raise LookupError("Transaction not found.")
        if transaction_archived(db,ref):
            raise StaleSplit("Restore this archived transaction before splitting.")
        if parent["revision"] != revision:
            raise StaleSplit("Transaction changed elsewhere. Reload before editing.")
        if action == "save":
            validate_lines(lines, parent["amount"])
        elif not active(db, ref):
            raise ValueError("No active split to withdraw.")
        next_revision = revision + 1
        if db.execute("UPDATE finance_transactions SET revision=? WHERE id=? AND revision=?",
                      (next_revision,ref,revision)).rowcount != 1:
            raise StaleSplit("Transaction changed elsewhere.")
        db.execute("INSERT INTO finance_split_events VALUES(?,?,?,?,?,?)",
                   (ref,next_revision,op,encoded,datetime.now(timezone.utc).isoformat(),actor))
        audit(db,"transaction_split",ref,next_revision)
        return next_revision


class StaleSplit(ValueError):
    pass


def read(ref):
    from hq_native_finance import schema, transaction_archived
    with closing(connect()) as db, db:
        schema(db)
        tables(db)
        parent = db.execute("SELECT * FROM finance_transactions WHERE id=?", (ref,)).fetchone()
        if not parent:
            raise LookupError("Transaction not found.")
        history = [dict(revision=r["revision"], created=r["created"],
                        **json.loads(r["payload"])) for r in db.execute(
            """SELECT * FROM finance_split_events WHERE transaction_id=?
               ORDER BY revision DESC LIMIT 100""", (ref,))]
        return dict(parent, archived=transaction_archived(db,ref)),history


def parse_form(form):
    lines = []
    for i in range(5):
        raw = form.get("amount_"+str(i),"").strip()
        category = form.get("category_"+str(i),"")
        if not raw and not category:
            continue
        if not MONEY.fullmatch(raw):
            raise ValueError("Enter pounds with at most two decimal places.")
        lines.append(dict(category=category,pence=int(Decimal(raw)*100)))
    return lines



def classification_report(month):
    """One bounded month of manual entries; effective split replaces parent, never adds cash."""
    if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month) or month[:4] == "0000":
        raise ValueError("Select a valid month.")
    from hq_native_finance import schema
    with closing(connect()) as db, db:
        schema(db)
        tables(db)
        db.commit()
        db.execute("BEGIN")
        return classification_period(db, month+"-01", month+"-31")


def classification_period(db, start, end, *, include_revisions=False):
    """Caller owns transaction and schema. Bounded manual ledger, not bank evidence."""
    from hq_native_finance import ACTIVE_TRANSACTION_SQL
    rows = db.execute("""SELECT t.*, e.revision AS split_revision, e.payload AS split_payload
        FROM finance_transactions t LEFT JOIN finance_split_events e
        ON e.transaction_id=t.id AND e.revision=(
            SELECT MAX(s.revision) FROM finance_split_events s WHERE s.transaction_id=t.id)
        WHERE t.day BETWEEN ? AND ? AND """+ACTIVE_TRANSACTION_SQL+""" ORDER BY t.day DESC,t.id LIMIT 5001""",
        (start,end)).fetchall()
    if len(rows)>5000:
        raise ValueError("This period exceeds the report limit; no partial totals are shown.")
    archived_count = db.execute("SELECT COUNT(*) FROM finance_transactions t WHERE t.day BETWEEN ? AND ? AND NOT ("+ACTIVE_TRANSACTION_SQL+")",(start,end)).fetchone()[0]
    totals = {}
    entries = []
    incoming = outgoing = unclassified = split_count = 0
    for row in rows:
        amount = row["amount"]
        if type(amount) is not int:
            raise ValueError("Invalid stored amount; report withheld.")
        incoming += max(amount,0)
        outgoing += max(-amount,0)
        payload = json.loads(row["split_payload"]) if row["split_payload"] else None
        if payload is not None and payload.get("action") not in ("save","withdraw"):
            raise ValueError("Invalid split history; report withheld.")
        split = bool(payload and payload["action"]=="save")
        if split:
            if row["split_revision"] != row["revision"]:
                raise ValueError("A split revision is inconsistent; report withheld.")
            lines = validate_lines(payload["lines"],amount)
            split_count += 1
        else:
            if row["category"] not in categories():
                raise ValueError("Unknown stored category; report withheld.")
            lines = [dict(category=row["category"],pence=amount)]
        for line in lines:
            cat, pence = line["category"],line["pence"]
            bucket = totals.setdefault(cat,dict(incoming=0,outgoing=0))
            bucket["incoming"] += max(pence,0)
            bucket["outgoing"] += max(-pence,0)
            if cat=="unclassified":
                unclassified += abs(pence)
        entries.append(dict(id=row["id"], day=row["day"], description=row["description"],
                            amount=amount, split=split, lines=lines, reviewed=bool(row["reviewed"])))
    if include_revisions:
        for entry, row in zip(entries, rows):
            entry["revision"] = row["revision"]
    if sum(x["incoming"] for x in totals.values()) != incoming or sum(x["outgoing"] for x in totals.values()) != outgoing:
        raise ValueError("Classification totals do not conserve parent amounts.")
    return dict(entries=entries, totals=sorted(totals.items()), incoming=incoming,
                outgoing=outgoing, unclassified=unclassified, split_count=split_count, archived_count=archived_count)


def register(bp):
    @bp.get("/hq/finance/classifications")
    @required
    def classification_report_page():
        if not storage_ready():
            abort(503)
        month = request.args.get("month", datetime.now(timezone.utc).strftime("%Y-%m"))
        report, error, status = None, None, 200
        try:
            report = classification_report(month)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            error, status = "Classification report unavailable: "+str(exc), 400
        except (sqlite3.Error, OSError):
            abort(503)
        return render_template("finance_classifications.html", month=month, report=report,
                               error=error),status,{"Cache-Control":"no-store"}


    @bp.route("/hq/finance/transactions/<ref>/split", methods=["GET","POST"])
    @required
    def transaction_split_page(ref):
        if not HEX.fullmatch(ref):
            abort(404)
        if not storage_ready():
            abort(503)
        error, status = None, 200
        try:
            if request.method == "POST":
                request.max_content_length = 8192
                token = session.get("csrf")
                if not token or not secrets.compare_digest(request.form.get("csrf",""),token):
                    abort(403)
                revision = request.form.get("revision","")
                if not re.fullmatch(r"[0-9]{1,19}",revision):
                    raise ValueError("Invalid revision.")
                action = request.form.get("action")
                save(ref,int(revision),action,parse_form(request.form) if action=="save" else [],
                     session.get("google_sub",""))
                return redirect(url_for("native_finance.transaction_split_page",ref=ref),code=303)
        except StaleSplit as exc:
            error,status=str(exc),409
        except (ValueError,TypeError) as exc:
            error,status=str(exc),400
        except LookupError:
            abort(404)
        except (sqlite3.Error,OSError):
            abort(503)
        try:
            parent,history=read(ref)
        except LookupError:
            abort(404)
        except (sqlite3.Error,OSError):
            abort(503)
        current=history[0]["lines"] if history and history[0]["action"]=="save" else []
        return render_template("finance_transaction_split.html",parent=parent,history=history,
            current=current,categories=categories(),csrf=session.get("csrf",""),error=error),status,{"Cache-Control":"no-store"}
