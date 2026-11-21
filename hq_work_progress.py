"""Owner-only, durable work-stage milestones for canonical HQ jobs.

Stage observations do not mark a job completed, booked, invoiced or paid. They
never write to Sheets, calendar, Square, customer messaging or banking.
"""
import hashlib
import hmac
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from flask import current_app
from customer_request_store import connect
from hq_core_records import _schema, _audit_work, StaleWorkRecordError

WORK_STAGES = {
    "checked_in": "Vehicle checked in",
    "preparation": "Preparation / masking",
    "repair": "Repair / bodywork",
    "primer": "Primer / surface preparation",
    "paint": "Paint / finish application",
    "installation": "Tint / wrap / parts installation",
    "finishing": "Finishing / polishing",
    "quality_check": "Quality check",
    "ready_for_collection": "Ready for collection (internal only)",
}
_WORK_ID = re.compile(r"[0-9a-f]{32}\Z")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _validate(work_id):
    if not isinstance(work_id, str) or not _WORK_ID.fullmatch(work_id):
        raise ValueError("Invalid HQ work reference")


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_work_stage_events(
        event_id TEXT PRIMARY KEY, work_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision >= 1),
        stage TEXT NOT NULL, actor_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(work_id,revision),
        FOREIGN KEY(work_id) REFERENCES hq_work_records(id))""")


def _actor_fingerprint(actor):
    # No raw owner identifier is duplicated into the stage ledger.
    if not isinstance(actor, str) or not 1 <= len(actor) <= 256:
        raise ValueError("Verified owner identity required")
    secret = current_app.secret_key
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("HQ session secret unavailable")
    return hmac.new(secret.encode(), actor.encode(), hashlib.sha256).hexdigest()


def work_progress_snapshot(work_id):
    """Owner-facing snapshot across all canonical HQ jobs, including public conversions."""
    _validate(work_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        row = db.execute("""SELECT w.id,w.kind,w.service,w.status,w.revision,
            w.source_request_id,c.name AS customer_name,v.make,v.model,v.registration
            FROM hq_work_records w
            JOIN hq_customers c ON c.id=w.customer_id
            JOIN hq_vehicles v ON v.id=w.vehicle_id WHERE w.id=?""", (work_id,)).fetchone()
        if row is None:
            raise LookupError("HQ job not found")
        events = db.execute("""SELECT revision,stage,created_at FROM hq_work_stage_events
            WHERE work_id=? ORDER BY revision DESC LIMIT 50""", (work_id,)).fetchall()
    info = dict(row)
    info["events"] = [dict(event) for event in reversed(events)]
    info["current_stage"] = events[0]["stage"] if events else None
    info["can_record_progress"] = info["kind"] == "job" and info["status"] in ("approved", "in_progress")
    return info


def record_work_progress(work_id, stage, expected_revision, owner_identity):
    """Atomic, optimistic stage append with an audit; same-stage retries are no-ops.

    Requires an approved or in-progress job. This is *not* a status transition.
    Stale competing changes fail rather than overwriting audit evidence.
    """
    _validate(work_id)
    if not isinstance(stage, str) or stage not in WORK_STAGES:
        raise ValueError("Select a supported work stage")
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1:
        raise ValueError("Invalid expected work revision")
    actor_ref = _actor_fingerprint(owner_identity)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT kind,status,revision FROM hq_work_records WHERE id=?",
                         (work_id,)).fetchone()
        if row is None:
            raise LookupError("HQ job not found")
        if row["kind"] != "job" or row["status"] not in ("approved", "in_progress"):
            raise ValueError("Only approved or in-progress jobs can record work stages")
        previous = db.execute("""SELECT stage FROM hq_work_stage_events
            WHERE work_id=? ORDER BY revision DESC LIMIT 1""", (work_id,)).fetchone()
        old_stage = previous["stage"] if previous else None
        if old_stage == stage:
            return row["revision"]  # Identical repeated action, no duplicate history.
        if expected_revision != row["revision"]:
            raise StaleWorkRecordError("The job changed. Reload before recording a new work stage.")
        updated = db.execute("""UPDATE hq_work_records SET revision=revision+1
            WHERE id=? AND revision=? AND status IN ('approved','in_progress') AND kind='job'""",
            (work_id, expected_revision))
        if updated.rowcount != 1:
            raise StaleWorkRecordError("The job changed. Reload before recording a new work stage.")
        new_revision = expected_revision + 1
        db.execute("""INSERT INTO hq_work_stage_events
            (event_id,work_id,revision,stage,actor_fingerprint,created_at)
            VALUES (?,?,?,?,?,?)""",
            (uuid.uuid4().hex, work_id, new_revision, stage, actor_ref, _now()))
        _audit_work(db, work_id, new_revision, "progress_stage",
                    {"stage": old_stage}, {"stage": stage})
    return new_revision
