"""Owner-only native workshop time entries linked to canonical HQ job IDs.

Amounts are in integer pence. Missing hourly costs are UNKNOWN, not zero.
No Sheets, payment, appointment, customer-message or external side effects.
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
from hq_work_progress import WORK_STAGES, _actor_fingerprint

_WORK_ID = re.compile(r"[0-9a-f]{32}\Z")
_ALLOWED = ("approved", "in_progress")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _validate_id(value):
    if not isinstance(value, str) or not _WORK_ID.fullmatch(value):
        raise ValueError("Invalid HQ work reference")


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_work_time_entries (
        entry_id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL REFERENCES hq_work_records(id),
        operation_id TEXT NOT NULL UNIQUE,
        payload_fingerprint TEXT NOT NULL,
        minutes INTEGER NOT NULL CHECK(minutes BETWEEN 1 AND 1440),
        stage TEXT NOT NULL,
        note TEXT NOT NULL,
        hourly_cost_pence INTEGER CHECK(hourly_cost_pence BETWEEN 0 AND 1000000),
        actor_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        voided_at TEXT,
        voided_by_fingerprint TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_hq_work_time_work ON hq_work_time_entries(work_id,created_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_work_time_rate_corrections (
        replacement_entry_id TEXT PRIMARY KEY REFERENCES hq_work_time_entries(entry_id),
        original_entry_id TEXT NOT NULL UNIQUE REFERENCES hq_work_time_entries(entry_id),
        reason TEXT NOT NULL, created_at TEXT NOT NULL)""")


def _validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid time entry")
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str) or not _WORK_ID.fullmatch(operation_id):
        raise ValueError("Invalid time entry operation")
    raw_minutes = payload.get("minutes", "")
    if not isinstance(raw_minutes, str) or not re.fullmatch(r"[0-9]{1,4}", raw_minutes):
        raise ValueError("Enter actual minutes as a whole number")
    minutes = int(raw_minutes)
    if not 1 <= minutes <= 1440:
        raise ValueError("Minutes must be between 1 and 1440")
    stage = payload.get("stage")
    if stage not in WORK_STAGES:
        raise ValueError("Select a supported workshop activity")
    note = payload.get("note", "")
    if not isinstance(note, str) or len(note.strip()) > 160:
        raise ValueError("Work note must be at most 160 characters")
    # Rate is an ACTUAL internal labour cost per hour, never an assumed sale rate.
    raw_rate = payload.get("hourly_cost_pence", "")
    if raw_rate is None:
        raw_rate = ""
    if not isinstance(raw_rate, str) or (raw_rate and not re.fullmatch(r"[0-9]{1,7}", raw_rate)):
        raise ValueError("Enter internal hourly cost in pence or leave unknown")
    # The owner-facing form accepts pounds; retain old pence submissions and
    # normalize both to the same integer-pence replay fingerprint.
    pounds = payload.get("hourly_cost_pounds", "")
    if not isinstance(pounds, str):
        raise ValueError("Enter internal hourly cost in pounds or leave unknown")
    if pounds:
        if raw_rate or not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,2})?", pounds):
            raise ValueError("Enter one internal hourly cost in pounds with up to two decimal places")
        whole, _, fraction = pounds.partition(".")
        rate = int(whole) * 100 + int(fraction.ljust(2, "0"))
    else:
        rate = None if raw_rate == "" else int(raw_rate)
    if rate is not None and not 0 <= rate <= 1000000:
        raise ValueError("Hourly cost must be between 0 and 1000000 pence")
    correction = payload.get("timer_correction_reason", "")
    if not isinstance(correction, str) or len(correction.strip()) > 160:
        raise ValueError("Timer correction reason must be at most 160 characters")
    fields = {
        "operation_id": operation_id, "minutes": minutes,
        "stage": stage, "note": note.strip(), "hourly_cost_pence": rate,
    }
    # Preserve fingerprints of existing exact and manual entry submissions.
    if correction.strip():
        fields["timer_correction_reason"] = correction.strip()
    return fields


def _fingerprint(fields):
    secret = current_app.secret_key
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("HQ session secret unavailable")
    raw = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _validate_revision(revision):
    if type(revision) is not int or not 0 <= revision < 2**63 - 1:
        raise ValueError("Invalid expected work revision")


def _read_work(db, work_id):
    work = db.execute("""SELECT w.id,w.kind,w.status,w.revision,w.service,
        w.price_pence,c.name AS customer_name,v.make,v.model,v.registration,
        COALESCE(a.is_archived,0) AS is_archived
        FROM hq_work_records w
        JOIN hq_customers c ON c.id=w.customer_id
        JOIN hq_vehicles v ON v.id=w.vehicle_id
        LEFT JOIN hq_work_archive_state a ON a.work_id=w.id WHERE w.id=?""", (work_id,)).fetchone()
    if work is None:
        raise LookupError("HQ work not found")
    return dict(work)


def time_ledger_snapshot(work_id):
    """Read all active entries for totals; show only latest 100 in history."""
    _validate_id(work_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        work = _read_work(db, work_id)
        totals = db.execute("""SELECT COUNT(*) AS entry_count,
            COALESCE(SUM(minutes),0) AS minutes_total,
            SUM(CASE WHEN hourly_cost_pence IS NULL THEN 1 ELSE 0 END) AS unknown_rates,
            SUM(CASE WHEN hourly_cost_pence IS NOT NULL
                THEN (minutes * hourly_cost_pence + 30)/60 END) AS known_cost
            FROM hq_work_time_entries WHERE work_id=? AND voided_at IS NULL""",
            (work_id,)).fetchone()
        stage_totals = [dict(row) for row in db.execute("""SELECT stage,
            COUNT(*) AS entry_count, SUM(minutes) AS minutes_total,
            SUM(CASE WHEN hourly_cost_pence IS NULL THEN 1 ELSE 0 END) AS unknown_rate_count,
            SUM(CASE WHEN hourly_cost_pence IS NOT NULL
                THEN (minutes * hourly_cost_pence + 30)/60 END) AS known_cost_subtotal_pence
            FROM hq_work_time_entries WHERE work_id=? AND voided_at IS NULL
            GROUP BY stage ORDER BY stage""", (work_id,))]
        for stage in stage_totals:
            stage["actual_labour_cost_pence"] = (
                stage["known_cost_subtotal_pence"] if not stage["unknown_rate_count"] else None)
        entries = [dict(row) for row in db.execute("""SELECT
            e.entry_id,e.minutes,e.stage,e.note,e.hourly_cost_pence,e.created_at,e.voided_at,
            r.original_entry_id AS corrected_from, r.reason AS rate_correction_reason,
            n.replacement_entry_id AS replaced_by
            FROM hq_work_time_entries e
            LEFT JOIN hq_work_time_rate_corrections r ON r.replacement_entry_id=e.entry_id
            LEFT JOIN hq_work_time_rate_corrections n ON n.original_entry_id=e.entry_id
            WHERE e.work_id=?
            ORDER BY e.created_at DESC,e.entry_id DESC LIMIT 101""", (work_id,))]
    for entry in entries:
        entry["rate_operation"] = uuid.uuid4().hex
    unknown = totals["unknown_rates"] or 0
    work.update({
        "entry_count": totals["entry_count"],
        "minutes_total": totals["minutes_total"],
        "unknown_rate_count": unknown,
        "known_cost_subtotal_pence": totals["known_cost"],
        # Do not convert absent evidence into a "£0 total".
        "actual_labour_cost_pence": (totals["known_cost"] or 0)
            if totals["entry_count"] and unknown == 0 else None,
        "entries": entries[:100],
        "stage_totals": stage_totals,
        "history_truncated": len(entries) > 100,
        "can_edit_time": work["kind"] == "job" and work["status"] in _ALLOWED and not work["is_archived"],
        "payment_verified": False,
    })
    return work


def append_work_time(work_id, payload, expected_revision, owner_identity):
    """Append an immutable actual-time entry, with replay and revision checks."""
    _validate_id(work_id)
    _validate_revision(expected_revision)
    fields = _validate_payload(payload)
    actor = _actor_fingerprint(owner_identity)
    fingerprint = _fingerprint(fields)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        work = _read_work(db, work_id)
        prior = db.execute("""SELECT entry_id,work_id,payload_fingerprint
            FROM hq_work_time_entries WHERE operation_id=?""",
            (fields["operation_id"],)).fetchone()
        if prior:
            if prior["work_id"] != work_id or not hmac.compare_digest(
                prior["payload_fingerprint"], fingerprint
            ):
                raise StaleWorkRecordError("This time entry was submitted with different details.")
            return prior["entry_id"]
        from hq_work_timer import tables as timer_tables
        timer_tables(db)
        timer = db.execute("SELECT * FROM hq_timer_sessions WHERE id=?", (fields["operation_id"],)).fetchone()
        if not timer and db.execute("""SELECT 1 FROM hq_timer_sessions WHERE work_id=?
            AND state IN ('running','paused','review')""", (work_id,)).fetchone():
            raise ValueError("Finish or correct the persistent timer before entering manual time")
        correction = fields.get("timer_correction_reason")
        if correction and not timer:
            raise ValueError("Timer correction requires a finished timer")
        if timer:
            original_minutes = timer["active_seconds"] // 60
            if timer["work_id"] != work_id or timer["state"] != "review":
                raise ValueError("Review the exact finished timer minutes before saving")
            if fields["minutes"] > original_minutes or (fields["minutes"] != original_minutes and not correction):
                raise ValueError("Review the exact finished timer minutes before saving, or reduce them with a correction reason")
            if correction and fields["minutes"] == original_minutes:
                raise ValueError("A correction reason requires a reduction in reviewed minutes")
        archived = db.execute("SELECT is_archived FROM hq_work_archive_state WHERE work_id=?", (work_id,)).fetchone()
        if work["kind"] != "job" or work["status"] not in _ALLOWED or (archived and archived["is_archived"]):
            raise ValueError("Actual time can be recorded only for active approved or in-progress jobs")
        if work["revision"] != expected_revision:
            raise StaleWorkRecordError("This job changed. Reload before recording time.")
        updated = db.execute("""UPDATE hq_work_records SET revision=revision+1
            WHERE id=? AND revision=?""", (work_id, expected_revision))
        if updated.rowcount != 1:
            raise StaleWorkRecordError("This job changed. Reload before recording time.")
        entry_id = uuid.uuid4().hex
        db.execute("""INSERT INTO hq_work_time_entries
            (entry_id,work_id,operation_id,payload_fingerprint,minutes,stage,note,
            hourly_cost_pence,actor_fingerprint,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (entry_id, work_id, fields["operation_id"], fingerprint,
             fields["minutes"], fields["stage"], fields["note"],
             fields["hourly_cost_pence"], actor, _now()))
        if timer:
            if correction:
                # Retain original measured seconds; only the reviewed ledger posting changes.
                detail = f'{timer["active_seconds"]} measured seconds; {fields["minutes"]} approved minutes; reason: {correction}'
                db.execute("INSERT INTO hq_timer_events VALUES (?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex, timer["id"], work_id, "review_correction",
                            timer["revision"], int(datetime.now(timezone.utc).timestamp()), detail))
            db.execute("UPDATE hq_timer_sessions SET state='saved',entry_id=?,revision=revision+1 WHERE id=?",
                       (entry_id, timer["id"]))
        _audit_work(db, work_id, expected_revision + 1, "actual_time_added",
                    {"entry_count_change": 0},
                    {"entry_count_change": 1, "entry_id": entry_id, "cost_known": fields["hourly_cost_pence"] is not None})
    return entry_id


def void_work_time(work_id, entry_id, expected_revision, owner_identity):
    """Correct mistakes by a versioned reversal, never delete source evidence."""
    _validate_id(work_id)
    _validate_id(entry_id)
    _validate_revision(expected_revision)
    actor = _actor_fingerprint(owner_identity)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        work = _read_work(db, work_id)
        row = db.execute("""SELECT voided_at FROM hq_work_time_entries
            WHERE work_id=? AND entry_id=?""", (work_id, entry_id)).fetchone()
        if row is None:
            raise LookupError("Time entry not found")
        if row["voided_at"] is not None:
            return work["revision"]  # Exact replay, no duplicate reversal.
        if work["kind"] != "job" or work["status"] not in _ALLOWED or work["is_archived"]:
            raise ValueError("Closed or archived work time is locked")
        if work["revision"] != expected_revision:
            raise StaleWorkRecordError("This job changed. Reload before correcting time.")
        updated = db.execute("UPDATE hq_work_records SET revision=revision+1 WHERE id=? AND revision=?",
                             (work_id, expected_revision))
        if updated.rowcount != 1:
            raise StaleWorkRecordError("This job changed. Reload before correcting time.")
        db.execute("""UPDATE hq_work_time_entries SET voided_at=?,voided_by_fingerprint=?
            WHERE entry_id=? AND voided_at IS NULL""", (_now(), actor, entry_id))
        _audit_work(db, work_id, expected_revision + 1, "actual_time_voided",
                    {"entry_id": entry_id, "voided": False},
                    {"entry_id": entry_id, "voided": True})
    return expected_revision + 1


def correct_work_time_rate(work_id, entry_id, payload, expected_revision, owner_identity):
    """Atomically reverse and replace only the rate; retain original timer evidence."""
    _validate_id(work_id)
    _validate_id(entry_id)
    _validate_revision(expected_revision)
    actor = _actor_fingerprint(owner_identity)
    reason = payload.get("reason", "")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 160:
        raise ValueError("Enter a rate correction reason (1–160 characters)")
    reason = reason.strip()
    # Require the field even when intentionally clearing a rate to unknown.
    if "hourly_cost_pounds" not in payload:
        raise ValueError("Enter a rate or explicitly leave it unknown")
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        work = _read_work(db, work_id)
        old = db.execute("""SELECT * FROM hq_work_time_entries
            WHERE work_id=? AND entry_id=?""", (work_id, entry_id)).fetchone()
        if old is None:
            raise LookupError("Time entry not found")
        fields = _validate_payload({
            "operation_id": payload.get("operation_id"),
            "minutes": str(old["minutes"]), "stage": old["stage"], "note": old["note"],
            "hourly_cost_pounds": payload["hourly_cost_pounds"],
        })
        rate = fields["hourly_cost_pence"]
        fingerprint = _fingerprint({"rate_correction": entry_id, "reason": reason,
            "revision": expected_revision, "fields": fields})
        prior = db.execute("""SELECT entry_id,work_id,payload_fingerprint
            FROM hq_work_time_entries WHERE operation_id=?""",
            (fields["operation_id"],)).fetchone()
        if prior:
            if prior["work_id"] != work_id or not hmac.compare_digest(
                prior["payload_fingerprint"], fingerprint
            ):
                raise StaleWorkRecordError("This correction operation was already used")
            return prior["entry_id"]
        if work["kind"] != "job" or work["status"] not in _ALLOWED or work["is_archived"]:
            raise ValueError("Closed or archived work time is locked")
        if old["voided_at"] is not None or work["revision"] != expected_revision:
            raise StaleWorkRecordError("This entry or job changed. Reload before correcting the rate.")
        if old["hourly_cost_pence"] == rate:
            raise ValueError("The rate has not changed")
        changed = db.execute("""UPDATE hq_work_records SET revision=revision+1
            WHERE id=? AND revision=?""", (work_id, expected_revision))
        if changed.rowcount != 1:
            raise StaleWorkRecordError("This job changed. Reload before correcting the rate.")
        timestamp = _now()
        replacement = uuid.uuid4().hex
        db.execute("""UPDATE hq_work_time_entries SET voided_at=?,voided_by_fingerprint=?
            WHERE entry_id=? AND voided_at IS NULL""", (timestamp, actor, entry_id))
        db.execute("""INSERT INTO hq_work_time_entries
            (entry_id,work_id,operation_id,payload_fingerprint,minutes,stage,note,
            hourly_cost_pence,actor_fingerprint,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (replacement, work_id, fields["operation_id"],
            fingerprint, old["minutes"], old["stage"], old["note"], rate, actor, timestamp))
        db.execute("INSERT INTO hq_work_time_rate_corrections VALUES (?,?,?,?)",
            (replacement, entry_id, reason, timestamp))
        _audit_work(db, work_id, expected_revision+1, "actual_time_rate_corrected",
            {"entry_id": entry_id, "hourly_cost_pence": old["hourly_cost_pence"]},
            {"entry_id": replacement, "hourly_cost_pence": rate})
        # The original timer session remains linked to its original posting.
        # The correction table explicitly links that posting to its successor.
    return replacement
