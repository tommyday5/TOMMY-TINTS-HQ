"""Fixture-only historical HQ-to-J candidate ledger; NOT a live link adapter.

No Flask route imports or calls this module. Never open the production HQ DB
with it or treat a stored proposal as an approved link, Sheets write baseline,
provider authorisation, job payment or bank reconciliation.

Only deliberately owner-selected identifiers and a local event digest are
stored. No customer, vehicle, spreadsheet cell or amount values are stored.
The digest is NOT a signed/externally anchored audit trail.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import sqlite3
import uuid

from hq_sheet_link_preflight import JOBS_SHEET_ID, MAX_ROWS
from hq_job_sync import WORKBOOK

_HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
_JOB = re.compile(r"J[0-9]{4,8}\Z")


class LinkProposalError(ValueError):
    """Unverified candidate cannot be staged without a collision."""


def _id(value):
    return type(value) is str and bool(_HEX_ID.fullmatch(value))


def _schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS offline_hq_sheet_link_proposals (
        id TEXT PRIMARY KEY, owner_scope TEXT NOT NULL,
        operation_id TEXT NOT NULL, work_id TEXT NOT NULL,
        work_revision INTEGER NOT NULL, workbook_id TEXT NOT NULL,
        sheet_id INTEGER NOT NULL, job_ref TEXT NOT NULL, sheet_row INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status = 'unverified_candidate'),
        created_at TEXT NOT NULL,
        UNIQUE(owner_scope, operation_id), UNIQUE(owner_scope, work_id),
        UNIQUE(owner_scope, workbook_id, sheet_id, job_ref COLLATE NOCASE),
        UNIQUE(owner_scope, workbook_id, sheet_id, sheet_row)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS offline_hq_sheet_link_events (
        id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, owner_scope TEXT NOT NULL,
        event_type TEXT NOT NULL, payload_digest TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""")



def _candidate_digest(payload):
    """Local checksum of selected identifiers, NOT a signed source attestation."""
    return hashlib.sha256(json.dumps(
        payload, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")).hexdigest()


def inspect_offline_candidate_ledger(db, *, owner_scope):
    """Read-only, owner-scoped consistency check of a fixture-only ledger.

    Every candidate must have exactly one matching local event. This detects
    accidental edits, partial audit writes and missing events before replay.
    An attacker changing both the row and unkeyed digest, or removing both
    records, is NOT detectable; there is no trusted external anchor.
    """
    if not isinstance(db, sqlite3.Connection) or not _id(owner_scope):
        raise LinkProposalError("Invalid offline owner scope or database")
    try:
        tables = {row[0] for row in db.execute(
            """SELECT name FROM sqlite_master WHERE type='table'
               AND name IN ('offline_hq_sheet_link_proposals',
                            'offline_hq_sheet_link_events')"""
        )}
        expected = {"offline_hq_sheet_link_proposals", "offline_hq_sheet_link_events"}
        if not tables:
            return {"candidate_count": 0, "event_count": 0,
                    "integrity_consistent": True, "source_authenticated": False,
                    "approved": False, "live_sync": False, "reconciled": False}
        if tables != expected:
            raise LinkProposalError("Offline candidate ledger has incomplete tables")
        proposals = db.execute(
            """SELECT id,owner_scope,work_id,work_revision,workbook_id,
                      sheet_id,job_ref,sheet_row,status,created_at
               FROM offline_hq_sheet_link_proposals WHERE owner_scope=?""",
            (owner_scope,)
        ).fetchall()
        events = db.execute(
            """SELECT id,proposal_id,owner_scope,event_type,payload_digest,recorded_at
               FROM offline_hq_sheet_link_events WHERE owner_scope=?""",
            (owner_scope,)
        ).fetchall()
    except sqlite3.Error:
        raise LinkProposalError("Offline candidate ledger could not be read") from None
    if len(proposals) != len(events) or len(proposals) > MAX_ROWS:
        raise LinkProposalError("Offline candidate ledger event coverage is incomplete")
    event_by_proposal = {}
    for event_id, proposal_id, scope, kind, digest, recorded_at in events:
        if (not _id(event_id) or not _id(proposal_id) or scope != owner_scope
                or kind != "candidate_staged" or type(digest) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or type(recorded_at) is not str or not recorded_at
                or proposal_id in event_by_proposal):
            raise LinkProposalError("Offline candidate event integrity failed")
        event_by_proposal[proposal_id] = (digest, recorded_at)
    for (proposal_id, scope, work_id, work_revision, workbook_id,
         sheet_id, job_ref, sheet_row, status, created_at) in proposals:
        if (not _id(proposal_id) or scope != owner_scope or not _id(work_id)
                or type(work_revision) is not int
                or not 0 <= work_revision < 2**63 - 1
                or workbook_id != WORKBOOK or type(sheet_id) is not int
                or sheet_id != JOBS_SHEET_ID or type(job_ref) is not str
                or not _JOB.fullmatch(job_ref) or type(sheet_row) is not int
                or not 5 <= sheet_row <= MAX_ROWS
                or status != "unverified_candidate"
                or type(created_at) is not str or not created_at):
            raise LinkProposalError("Offline candidate record integrity failed")
        event = event_by_proposal.pop(proposal_id, None)
        payload = (scope, work_id, work_revision, workbook_id,
                   sheet_id, job_ref, sheet_row)
        if (event is None or event[1] != created_at or
                not hmac.compare_digest(event[0], _candidate_digest(payload))):
            raise LinkProposalError("Offline candidate event does not match its record")
    if event_by_proposal:
        raise LinkProposalError("Offline candidate event has no matching record")
    return {"candidate_count": len(proposals), "event_count": len(events),
            "integrity_consistent": True, "source_authenticated": False,
            "approved": False, "live_sync": False, "reconciled": False}


def stage_unverified_candidate(db, *, owner_scope, operation_id, work_id,
                               work_revision, job_ref, sheet_row, inspection):
    """Store a NON-ACTIVE review candidate in a caller-owned isolated SQLite DB.

    The inspection is just a caller-provided shape: this rehearsal does NOT
    authenticate a Google read, internal record revision or owner identity.
    A production adapter must independently recheck both source versions,
    enforce the owner session and obtain separate approval before any link.
    """
    if (not isinstance(db, sqlite3.Connection)
            or not all(_id(value) for value in (owner_scope, operation_id, work_id))
            or type(work_revision) is not int
            or not 0 <= work_revision < 2**63 - 1
            or type(job_ref) is not str or not _JOB.fullmatch(job_ref)
            or type(sheet_row) is not int or not 5 <= sheet_row <= MAX_ROWS):
        raise LinkProposalError("Invalid owner-scoped candidate identifiers or revision")
    if (not isinstance(inspection, dict)
            or inspection.get("preflight") != "sheet_snapshot_only"
            or inspection.get("candidate_kind") != "existing"
            or inspection.get("complete_reference_scan") is not True
            or inspection.get("workbook_identity_checked") is not True
            or inspection.get("selected_row_formula_safe") is not True
            or any(inspection.get(flag) is not False for flag in (
                "historical_mapping_verified", "baseline_verified",
                "write_authorised", "live_sync", "reconciled"))):
        raise LinkProposalError("An existing read-only, unverified candidate is required")

    # Never let our context manager silently roll back a caller-owned transaction.
    if db.in_transaction:
        raise LinkProposalError("Finish the caller transaction before staging a candidate")

    payload = (owner_scope, work_id, work_revision, WORKBOOK,
               JOBS_SHEET_ID, job_ref, sheet_row)
    now = datetime.now(timezone.utc).isoformat()
    # SQLite unique constraints enforce one-to-one candidate allocation even
    # if two synthetic clients submit the same operation concurrently.
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            _schema(db)
            # A replay is not successful evidence if its local event was lost
            # or changed. This check stays in the same writer transaction.
            inspect_offline_candidate_ledger(db, owner_scope=owner_scope)
            previous = db.execute(
                """SELECT work_id, work_revision, workbook_id, sheet_id, job_ref,
                          sheet_row FROM offline_hq_sheet_link_proposals
                   WHERE owner_scope=? AND operation_id=?""",
                (owner_scope, operation_id)).fetchone()
            if previous is not None:
                if tuple(previous) != payload[1:]:
                    raise LinkProposalError("Operation replay conflicts with saved candidate")
                return {"candidate_state": "unverified_candidate", "created": False,
                        "approved": False, "live_sync": False, "reconciled": False}
            proposal_id = uuid.uuid4().hex
            db.execute(
                """INSERT INTO offline_hq_sheet_link_proposals
                   (id,owner_scope,operation_id,work_id,work_revision,workbook_id,
                    sheet_id,job_ref,sheet_row,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'unverified_candidate',?)""",
                (proposal_id, owner_scope, operation_id, work_id, work_revision,
                 WORKBOOK, JOBS_SHEET_ID, job_ref, sheet_row, now))
            digest = _candidate_digest(payload)
            db.execute(
                """INSERT INTO offline_hq_sheet_link_events
                   (id,proposal_id,owner_scope,event_type,payload_digest,recorded_at)
                   VALUES(?,?,?,'candidate_staged',?,?)""",
                (uuid.uuid4().hex, proposal_id, owner_scope, digest, now))
    except sqlite3.Error:
        raise LinkProposalError(
            "Offline candidate ledger unavailable or candidate is already reserved") from None
    return {"candidate_state": "unverified_candidate", "created": True,
            "approved": False, "live_sync": False, "reconciled": False}
