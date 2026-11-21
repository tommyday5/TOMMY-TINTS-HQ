"""Owner-only, revisioned actual additional-expense/overhead ledger for native HQ jobs.

A manually allocated cost is not an invoice, payment, bank movement or proof of
completeness. It is kept separate from legacy profitability and Sheets records.
"""
import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from flask import current_app

from customer_request_store import connect
from hq_core_records import _schema, _audit_work, StaleWorkRecordError
from hq_work_progress import _actor_fingerprint

_ID = re.compile(r"[0-9a-f]{32}\Z")
_AMOUNT = re.compile(r"[0-9]{1,9}\Z")
_ALLOWED = ("approved", "in_progress")
EXPENSE_TYPES = {
    "overhead": "Allocated workshop overhead",
    "subcontractor": "Subcontracted work",
    "transport": "Job transport / travel",
    "disposal": "Disposal / environmental charge",
    "other": "Other directly allocated expense",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _validate_id(value):
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("Invalid HQ reference")


def _validate_revision(value):
    if type(value) is not int or not 0 <= value < 2**63 - 1:
        raise ValueError("Invalid job revision")


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_work_expense_entries (
        entry_id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL REFERENCES hq_work_records(id),
        operation_id TEXT NOT NULL UNIQUE,
        payload_fingerprint TEXT NOT NULL,
        expense_type TEXT NOT NULL,
        description TEXT NOT NULL,
        allocated_cost_pence INTEGER CHECK(allocated_cost_pence BETWEEN 0 AND 100000000),
        source_note TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        voided_at TEXT,
        voided_by_fingerprint TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_hq_work_expense_work ON hq_work_expense_entries(work_id,created_at)")


def _payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid job expense")
    operation_id = payload.get("operation_id")
    _validate_id(operation_id)
    expense_type = payload.get("expense_type")
    if expense_type not in EXPENSE_TYPES:
        raise ValueError("Select an expense category")
    description = payload.get("description", "")
    note = payload.get("source_note", "")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 100 or re.search(r"[\x00-\x1f\x7f]", description):
        raise ValueError("Enter a description of at most 100 characters")
    if not isinstance(note, str) or len(note.strip()) > 160 or re.search(r"[\x00-\x1f\x7f]", note):
        raise ValueError("Source note must be at most 160 characters")
    raw = payload.get("allocated_cost_pence", "")
    if raw is None:
        raw = ""
    if not isinstance(raw, str) or (raw and _AMOUNT.fullmatch(raw) is None):
        raise ValueError("Enter actual allocated cost in whole pence or leave unknown")
    cost = None if raw == "" else int(raw)
    if cost is not None and cost > 100000000:
        raise ValueError("Cost exceeds the permitted allocation limit")
    return {
        "operation_id": operation_id, "expense_type": expense_type,
        "description": description.strip(), "allocated_cost_pence": cost,
        "source_note": note.strip(),
    }


def _fingerprint(fields):
    secret = current_app.secret_key
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("HQ session secret unavailable")
    raw = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _work(db, work_id):
    row = db.execute("""SELECT w.id,w.kind,w.status,w.revision,w.service,
        c.name AS customer_name,v.make,v.model,v.registration
        FROM hq_work_records w
        JOIN hq_customers c ON c.id=w.customer_id
        JOIN hq_vehicles v ON v.id=w.vehicle_id
        WHERE w.id=?""", (work_id,)).fetchone()
    if row is None:
        raise LookupError("HQ work not found")
    return dict(row)


def expense_ledger_snapshot(work_id):
    _validate_id(work_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        work = _work(db, work_id)
        totals = db.execute("""SELECT COUNT(*) AS entries,
            SUM(CASE WHEN allocated_cost_pence IS NULL THEN 1 ELSE 0 END) AS unknown,
            SUM(allocated_cost_pence) AS known_pence
            FROM hq_work_expense_entries WHERE work_id=? AND voided_at IS NULL""", (work_id,)).fetchone()
        total_history = db.execute(
            "SELECT COUNT(*) FROM hq_work_expense_entries WHERE work_id=?", (work_id,)).fetchone()[0]
        history = [dict(r) for r in db.execute("""SELECT entry_id,expense_type,description,
            allocated_cost_pence,source_note,created_at,voided_at
            FROM hq_work_expense_entries WHERE work_id=?
            ORDER BY created_at DESC,entry_id DESC LIMIT 100""", (work_id,))]
    count = totals["entries"]
    unknown = totals["unknown"] or 0
    work.update({
        "entry_count": count,
        "unknown_cost_count": unknown,
        "known_cost_subtotal_pence": totals["known_pence"],
        "actual_expenses_pence": (totals["known_pence"] or 0) if count and not unknown else None,
        "entries": history,
        "history_truncated": total_history > 100,
        "can_edit_expenses": work["kind"] == "job" and work["status"] in _ALLOWED,
        "payment_verified": False,
    })
    return work


def append_work_expense(work_id, payload, expected_revision, owner_identity):
    """Atomic new cost line, exactly-once per operation, conditional on work revision."""
    _validate_id(work_id)
    _validate_revision(expected_revision)
    fields = _payload(payload)
    actor = _actor_fingerprint(owner_identity)
    fingerprint = _fingerprint(fields)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        work = _work(db, work_id)
        previous = db.execute("""SELECT entry_id,work_id,payload_fingerprint
            FROM hq_work_expense_entries WHERE operation_id=?""", (fields["operation_id"],)).fetchone()
        if previous:
            if previous["work_id"] != work_id or not hmac.compare_digest(previous["payload_fingerprint"], fingerprint):
                raise StaleWorkRecordError("This operation was already used with different expense details")
            return previous["entry_id"]
        if work["kind"] != "job" or work["status"] not in _ALLOWED:
            raise ValueError("Expenses can only be recorded on approved or in-progress jobs")
        if work["revision"] != expected_revision:
            raise StaleWorkRecordError("This job changed. Reload before recording expenses.")
        updated = db.execute("UPDATE hq_work_records SET revision=revision+1 WHERE id=? AND revision=?",
                             (work_id, expected_revision))
        if updated.rowcount != 1:
            raise StaleWorkRecordError("This job changed. Reload before recording expenses.")
        entry_id = uuid.uuid4().hex
        db.execute("""INSERT INTO hq_work_expense_entries
            (entry_id,work_id,operation_id,payload_fingerprint,expense_type,description,
            allocated_cost_pence,source_note,actor_fingerprint,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (entry_id,work_id,fields["operation_id"],fingerprint,fields["expense_type"],
             fields["description"],fields["allocated_cost_pence"],fields["source_note"],actor,_now()))
        _audit_work(db, work_id, expected_revision + 1, "actual_expense_added",
                    {"entry_count_change": 0},
                    {"entry_count_change": 1, "entry_id": entry_id, "cost_known": fields["allocated_cost_pence"] is not None})
    return entry_id


def void_work_expense(work_id, entry_id, expected_revision, owner_identity):
    """Versioned correction; leave the source evidence and audit event intact."""
    _validate_id(work_id)
    _validate_id(entry_id)
    _validate_revision(expected_revision)
    actor = _actor_fingerprint(owner_identity)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        work = _work(db, work_id)
        row = db.execute("""SELECT voided_at FROM hq_work_expense_entries
            WHERE work_id=? AND entry_id=?""", (work_id, entry_id)).fetchone()
        if row is None:
            raise LookupError("Expense entry not found")
        if row["voided_at"] is not None:
            return work["revision"]
        if work["kind"] != "job" or work["status"] not in _ALLOWED:
            raise ValueError("Closed job costs are locked")
        if work["revision"] != expected_revision:
            raise StaleWorkRecordError("This job changed. Reload before correcting expenses.")
        updated = db.execute("UPDATE hq_work_records SET revision=revision+1 WHERE id=? AND revision=?",
                             (work_id, expected_revision))
        if updated.rowcount != 1:
            raise StaleWorkRecordError("This job changed. Reload before correcting expenses.")
        db.execute("""UPDATE hq_work_expense_entries
            SET voided_at=?,voided_by_fingerprint=? WHERE entry_id=? AND voided_at IS NULL""",
            (_now(),actor,entry_id))
        _audit_work(db, work_id, expected_revision + 1, "actual_expense_voided",
                    {"entry_id": entry_id, "voided": False},
                    {"entry_id": entry_id, "voided": True})
    return expected_revision + 1
