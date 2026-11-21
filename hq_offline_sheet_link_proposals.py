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

    payload = (owner_scope, work_id, work_revision, WORKBOOK,
               JOBS_SHEET_ID, job_ref, sheet_row)
    now = datetime.now(timezone.utc).isoformat()
    # SQLite unique constraints enforce one-to-one candidate allocation even
    # if two synthetic clients submit the same operation concurrently.
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            _schema(db)
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
            digest = hashlib.sha256(json.dumps(payload, separators=(",", ":"),
                                               ensure_ascii=True).encode("utf-8")).hexdigest()
            db.execute(
                """INSERT INTO offline_hq_sheet_link_events
                   (id,proposal_id,owner_scope,event_type,payload_digest,recorded_at)
                   VALUES(?,?,?,'candidate_staged',?,?)""",
                (uuid.uuid4().hex, proposal_id, owner_scope, digest, now))
    except sqlite3.IntegrityError:
        raise LinkProposalError(
            "Candidate row, J reference or HQ work is already reserved for review") from None
    return {"candidate_state": "unverified_candidate", "created": True,
            "approved": False, "live_sync": False, "reconciled": False}
