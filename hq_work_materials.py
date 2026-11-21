"""Durable owner-only actual material usage for canonical HQ jobs.

This ledger is separate from the legacy editable profitability log and stock.
Recorded consumed quantity does not prove purchase, receipt, inventory depletion
or payment. Missing actual costs are unknown, never inferred to be zero.
"""
import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from flask import current_app
from customer_request_store import connect
from hq_core_records import _schema, _audit_work, StaleWorkRecordError
from hq_work_progress import _actor_fingerprint

_ID = re.compile(r"[0-9a-f]{32}\Z")
_QUANTITY = re.compile(r"[0-9]{1,7}(?:\.[0-9]{1,3})?\Z")
_INTEGER = re.compile(r"[0-9]{1,9}\Z")
_ALLOWED = ("approved", "in_progress")
UNITS = {
    "ml": "Millilitres (ml)",
    "g": "Grams (g)",
    "each": "Items (whole)",
    "m": "Metres (m)",
    "sqm": "Square metres (m²)",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _validate_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid HQ reference")


def _validate_revision(value):
    if type(value) is not int or not 0 <= value < 2**63 - 1:
        raise ValueError("Invalid work revision")


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_work_material_entries (
        entry_id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL REFERENCES hq_work_records(id),
        operation_id TEXT NOT NULL UNIQUE,
        payload_fingerprint TEXT NOT NULL,
        material_name TEXT NOT NULL,
        quantity_milli INTEGER NOT NULL CHECK(quantity_milli BETWEEN 1 AND 1000000000),
        unit TEXT NOT NULL,
        actual_cost_pence INTEGER CHECK(actual_cost_pence BETWEEN 0 AND 100000000),
        source_note TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        voided_at TEXT,
        voided_by_fingerprint TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_hq_work_material_work ON hq_work_material_entries(work_id, created_at)")


def _payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid material entry")
    operation_id = payload.get("operation_id")
    _validate_id(operation_id)
    name = payload.get("material_name", "")
    note = payload.get("source_note", "")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100 or re.search(r"[\x00-\x1f\x7f]", name):
        raise ValueError("Enter a material name of at most 100 characters")
    if not isinstance(note, str) or len(note.strip()) > 160 or re.search(r"[\x00-\x1f\x7f]", note):
        raise ValueError("Source note must be at most 160 characters")
    unit = payload.get("unit")
    if unit not in UNITS:
        raise ValueError("Select an actual material unit")
    quantity = payload.get("quantity", "")
    if not isinstance(quantity, str) or not _QUANTITY.fullmatch(quantity):
        raise ValueError("Quantity must have at most three decimal places")
    quantity_milli = int(Decimal(quantity) * 1000)
    if not 1 <= quantity_milli <= 1000000000:
        raise ValueError("Quantity out of permitted range")
    if unit == "each" and quantity_milli % 1000:
        raise ValueError("Item counts must be whole numbers")
    raw_cost = payload.get("actual_cost_pence", "")
    if raw_cost is None:
        raw_cost = ""
    if not isinstance(raw_cost, str) or (raw_cost and not _INTEGER.fullmatch(raw_cost)):
        raise ValueError("Enter actual allocated cost in whole pence, or leave unknown")
    actual_cost = None if raw_cost == "" else int(raw_cost)
    if actual_cost is not None and actual_cost > 100000000:
        raise ValueError("Material cost out of permitted range")
    return {
        "operation_id": operation_id,
        "material_name": name.strip(),
        "quantity_milli": quantity_milli,
        "unit": unit,
        "actual_cost_pence": actual_cost,
        "source_note": note.strip(),
    }


def _fingerprint(fields):
    secret = current_app.secret_key
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("HQ session secret unavailable")
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _work(db, work_id):
    row = db.execute("""SELECT w.id,w.kind,w.status,w.revision,w.service,
        w.price_pence,c.name AS customer_name,v.make,v.model,v.registration
        FROM hq_work_records w
        JOIN hq_customers c ON c.id=w.customer_id
        JOIN hq_vehicles v ON v.id=w.vehicle_id
        WHERE w.id=?""", (work_id,)).fetchone()
    if row is None:
        raise LookupError("HQ job not found")
    return dict(row)


def material_ledger_snapshot(work_id):
    _validate_id(work_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        work = _work(db, work_id)
        totals = db.execute("""SELECT COUNT(*) AS count,
            SUM(CASE WHEN actual_cost_pence IS NULL THEN 1 ELSE 0 END) AS unknown_count,
            SUM(actual_cost_pence) AS known_cost
            FROM hq_work_material_entries WHERE work_id=? AND voided_at IS NULL""",
            (work_id,)).fetchone()
        history_count = db.execute(
            "SELECT COUNT(*) FROM hq_work_material_entries WHERE work_id=?", (work_id,)).fetchone()[0]
        entries = [dict(x) for x in db.execute("""SELECT entry_id,material_name,quantity_milli,
            unit,actual_cost_pence,source_note,created_at,voided_at
            FROM hq_work_material_entries WHERE work_id=?
            ORDER BY created_at DESC,entry_id DESC LIMIT 100""", (work_id,))]
    unknown = totals["unknown_count"] or 0
    count = totals["count"]
    work.update({
        "entry_count": count,
        "unknown_cost_count": unknown,
        "known_cost_subtotal_pence": totals["known_cost"],
        "actual_material_cost_pence": (totals["known_cost"] or 0)
            if count and unknown == 0 else None,
        "entries": entries,
        "history_truncated": history_count > 100,
        "can_edit_materials": work["kind"] == "job" and work["status"] in _ALLOWED,
        "payment_verified": False,
    })
    return work


def append_work_material(work_id, payload, expected_revision, owner_identity):
    """Exactly-once material entry, conditional on the canonical HQ revision."""
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
        previous = db.execute(
            "SELECT entry_id,work_id,payload_fingerprint FROM hq_work_material_entries WHERE operation_id=?",
            (fields["operation_id"],)).fetchone()
        if previous:
            if previous["work_id"] != work_id or not hmac.compare_digest(previous["payload_fingerprint"], fingerprint):
                raise StaleWorkRecordError("Operation was already submitted with different material details")
            return previous["entry_id"]
        if work["kind"] != "job" or work["status"] not in _ALLOWED:
            raise ValueError("Materials can only be recorded on approved or in-progress jobs")
        if work["revision"] != expected_revision:
            raise StaleWorkRecordError("This job changed. Reload before recording materials.")
        changed = db.execute(
            "UPDATE hq_work_records SET revision=revision+1 WHERE id=? AND revision=?",
            (work_id, expected_revision))
        if changed.rowcount != 1:
            raise StaleWorkRecordError("This job changed. Reload before recording materials.")
        entry_id = uuid.uuid4().hex
        db.execute("""INSERT INTO hq_work_material_entries
            (entry_id,work_id,operation_id,payload_fingerprint,material_name,quantity_milli,
             unit,actual_cost_pence,source_note,actor_fingerprint,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (entry_id, work_id, fields["operation_id"], fingerprint, fields["material_name"],
             fields["quantity_milli"], fields["unit"], fields["actual_cost_pence"],
             fields["source_note"], actor, _now()))
        _audit_work(db, work_id, expected_revision + 1, "material_usage_added",
                    {"entry_count_change": 0},
                    {"entry_count_change": 1, "entry_id": entry_id,
                     "cost_known": fields["actual_cost_pence"] is not None})
    return entry_id


def void_work_material(work_id, entry_id, expected_revision, owner_identity):
    """Reversal retains original quantities, allocated costs, and evidence."""
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
        entry = db.execute("SELECT voided_at FROM hq_work_material_entries WHERE entry_id=? AND work_id=?",
                           (entry_id, work_id)).fetchone()
        if entry is None:
            raise LookupError("Material entry not found")
        if entry["voided_at"] is not None:
            return work["revision"]
        if work["kind"] != "job" or work["status"] not in _ALLOWED:
            raise ValueError("Closed-job material history is locked")
        if work["revision"] != expected_revision:
            raise StaleWorkRecordError("This job changed. Reload before correcting materials.")
        changed = db.execute(
            "UPDATE hq_work_records SET revision=revision+1 WHERE id=? AND revision=?",
            (work_id, expected_revision))
        if changed.rowcount != 1:
            raise StaleWorkRecordError("This job changed. Reload before correcting materials.")
        db.execute("""UPDATE hq_work_material_entries
            SET voided_at=?,voided_by_fingerprint=? WHERE entry_id=? AND voided_at IS NULL""",
            (_now(), actor, entry_id))
        _audit_work(db, work_id, expected_revision + 1, "material_usage_voided",
                    {"entry_id": entry_id, "voided": False},
                    {"entry_id": entry_id, "voided": True})
    return expected_revision + 1
