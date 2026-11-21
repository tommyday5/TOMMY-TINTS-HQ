"""Owner-only archive/restore for native HQ work, not a lifecycle transition.

Archives hide completed/cancelled/draft records from active owner search while
preserving canonical identifiers, work status, audit history and all finance data.
No Sheets/provider operation or external messaging is permitted here.
"""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from customer_request_store import connect
from hq_core_records import _schema, _audit_work, StaleWorkRecordError, search_work_records

_ID = re.compile(r"[0-9a-f]{32}\Z")
_ALLOWED_ARCHIVE_STATUSES = frozenset(("draft", "ready_for_review", "completed", "cancelled"))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _validate_work_id(work_id):
    if not isinstance(work_id, str) or _ID.fullmatch(work_id) is None:
        raise ValueError("Invalid native work reference")


def archived_work_snapshot(work_id):
    """Read owner-facing state and bounded metadata-only history."""
    _validate_work_id(work_id)
    with connect() as db:
        _schema(db)
        db.execute("BEGIN")
        row = db.execute("""SELECT w.id,w.kind,w.service,w.status,w.revision,
            w.source_request_id,c.name AS customer_name,v.make,v.model,v.registration,
            COALESCE(a.is_archived,0) AS is_archived,a.archived_at,a.restored_at
            FROM hq_work_records w
            JOIN hq_customers c ON c.id=w.customer_id
            JOIN hq_vehicles v ON v.id=w.vehicle_id
            LEFT JOIN hq_work_archive_state a ON a.work_id=w.id
            WHERE w.id=? AND w.source_request_id=?""", (work_id, "HQ-" + work_id)).fetchone()
        if row is None:
            raise LookupError("Native HQ record not found")
        events = db.execute("""SELECT action,revision,created_at
            FROM hq_work_audit WHERE work_id=? AND action IN ('archive','restore')
            ORDER BY revision DESC LIMIT 50""", (work_id,)).fetchall()
    return {**dict(row), "events": [dict(e) for e in events],
            "can_archive": (row["status"] in _ALLOWED_ARCHIVE_STATUSES
                            and not row["is_archived"])}


def set_work_archived(work_id, action, expected_revision, operation_id):
    """Version-checked, journalled owner action. Replay is safe even after restore.

    The action is visibility-only: it never changes job status, selling price,
    planned deposit, time/material ledgers, receipts or customer-facing state.
    """
    _validate_work_id(work_id)
    if action not in ("archive", "restore"):
        raise ValueError("Invalid archive action")
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1:
        raise ValueError("Invalid work revision")
    if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
        raise ValueError("Invalid operation reference")
    fingerprint = hashlib.sha256(json.dumps(
        [work_id, action, expected_revision], separators=(",", ":")).encode()).hexdigest()
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("""SELECT work_id,action,request_fingerprint,result_revision
            FROM hq_work_archive_operations WHERE operation_id=?""",
            (operation_id,)).fetchone()
        if prior:
            if prior["work_id"] != work_id or prior["action"] != action or (
                prior["request_fingerprint"] != fingerprint
            ):
                raise StaleWorkRecordError("Operation identifier already used with different details")
            return prior["result_revision"]
        row = db.execute("""SELECT revision,status FROM hq_work_records
            WHERE id=? AND source_request_id=?""",
            (work_id, "HQ-" + work_id)).fetchone()
        if row is None:
            raise LookupError("Native HQ record not found")
        state = db.execute("""SELECT is_archived FROM hq_work_archive_state
            WHERE work_id=?""", (work_id,)).fetchone()
        is_archived = bool(state and state["is_archived"])
        if row["revision"] != expected_revision:
            raise StaleWorkRecordError("Work changed since this screen loaded. Refresh and review.")
        if action == "archive":
            if is_archived:
                raise ValueError("This record is already archived")
            if row["status"] not in _ALLOWED_ARCHIVE_STATUSES:
                raise ValueError("Active approved work must be completed or cancelled before archiving")
        elif not is_archived:
            raise ValueError("This record is not archived")
        revision = expected_revision + 1
        changed = db.execute("""UPDATE hq_work_records SET revision=revision+1
            WHERE id=? AND revision=?""", (work_id, expected_revision))
        if changed.rowcount != 1:
            raise StaleWorkRecordError("Work changed since this screen loaded")
        now = _now()
        if action == "archive":
            db.execute("""INSERT INTO hq_work_archive_state
                (work_id,is_archived,archived_at,restored_at) VALUES (?,1,?,NULL)
                ON CONFLICT(work_id) DO UPDATE SET is_archived=1,
                archived_at=excluded.archived_at,restored_at=NULL""",
                (work_id, now))
        else:
            db.execute("""UPDATE hq_work_archive_state
                SET is_archived=0,restored_at=? WHERE work_id=?""",
                (now, work_id))
        _audit_work(db, work_id, revision, action,
                    {"archived": is_archived}, {"archived": not is_archived})
        db.execute("""INSERT INTO hq_work_archive_operations
            (operation_id,work_id,action,request_fingerprint,result_revision,created_at)
            VALUES (?,?,?,?,?,?)""",
            (operation_id, work_id, action, fingerprint, revision, now))
    return revision


def list_archived_work(offset=0):
    """List archived records only, with same bounded canonical owner search."""
    return search_work_records(archived=True, limit=25, offset=offset)
