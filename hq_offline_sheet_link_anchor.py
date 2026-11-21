"""Detached, fixture-only audit head for historical Sheets link proposals.

This module has no Flask routes, no production storage, no Google access and no
source authentication. The caller must retain the returned head independently,
protect the caller-supplied key, and compare it before trusting an offline copy.
Never treat a matching head as approval to link, write, or reconcile anything.
"""
import hashlib
import hmac
import json
import re
import sqlite3

from hq_sheet_link_preflight import MAX_ROWS
from hq_offline_sheet_link_proposals import (
    LinkProposalError, _id, inspect_offline_candidate_ledger,
)

_VERSION = 1
_CONTEXT = b"tommy-tints/offline-sheet-link-head/v1\x00"
_HEX_SHA256 = re.compile("[0-9a-f]{64}")


def _validate_key(key):
    if type(key) is not bytes or len(key) < 32:
        raise LinkProposalError("A distinct offline audit key of at least 32 bytes is required")


def export_detached_candidate_head(db, *, owner_scope, key):
    """Produce a PII-free HMAC of a consistent, committed, owner-scoped snapshot.

    A SQLite read transaction keeps the existing local consistency inspection
    and the record read on the same snapshot. It rolls back only the read
    transaction this function opened. Caller-owned transactions are rejected.
    No schema, event, key or head is stored by this module.
    """
    if not isinstance(db, sqlite3.Connection) or not _id(owner_scope):
        raise LinkProposalError("Invalid offline candidate ledger or owner scope")
    _validate_key(key)
    if db.in_transaction:
        raise LinkProposalError("Finish the caller transaction before exporting an audit head")

    try:
        db.execute("BEGIN")
        try:
            inspection = inspect_offline_candidate_ledger(db, owner_scope=owner_scope)
            records = []
            if inspection["candidate_count"]:
                records = db.execute(
                    """SELECT p.id, p.owner_scope, p.operation_id, p.work_id,
                              p.work_revision, p.workbook_id, p.sheet_id, p.job_ref,
                              p.sheet_row, p.status, p.created_at,
                              e.id, e.proposal_id, e.owner_scope, e.event_type,
                              e.payload_digest, e.recorded_at
                       FROM offline_hq_sheet_link_proposals AS p
                       JOIN offline_hq_sheet_link_events AS e
                         ON e.proposal_id = p.id AND e.owner_scope = p.owner_scope
                       WHERE p.owner_scope = ?
                       ORDER BY p.id, e.id""",
                    (owner_scope,),
                ).fetchall()
            if len(records) != inspection["candidate_count"]:
                raise LinkProposalError("Offline candidate audit snapshot is incomplete")
            # The original local checksum does not cover operation_id. The
            # detached HMAC authenticates it, alongside all other stored fields.
            if any(not _id(record[2]) for record in records):
                raise LinkProposalError("Offline candidate operation identity is invalid")
            canonical = json.dumps(
                {"owner_scope": owner_scope, "records": records},
                separators=(",", ":"), ensure_ascii=True,
            ).encode("utf-8")
            digest = hmac.new(key, _CONTEXT + canonical, hashlib.sha256).hexdigest()
            count = len(records)
        finally:
            db.rollback()
    except sqlite3.Error:
        raise LinkProposalError("Offline candidate audit snapshot could not be read") from None

    return {
        "version": _VERSION, "candidate_count": count, "event_count": count,
        "digest": digest, "source_authenticated": False,
        "approved": False, "live_sync": False, "reconciled": False,
    }


def verify_detached_candidate_head(db, *, owner_scope, key, head):
    """Check against a previously retained independent head, never a DB copy."""
    if (type(head) is not dict or set(head) != {
            "version", "candidate_count", "event_count", "digest",
            "source_authenticated", "approved", "live_sync", "reconciled",
        } or type(head["version"]) is not int or head["version"] != _VERSION
            or type(head["candidate_count"]) is not int
            or not 0 <= head["candidate_count"] <= MAX_ROWS
            or type(head["event_count"]) is not int
            or head["event_count"] != head["candidate_count"]
            or type(head["digest"]) is not str
            or not _HEX_SHA256.fullmatch(head["digest"])
            or any(head[name] is not False for name in (
                "source_authenticated", "approved", "live_sync", "reconciled",
            ))):
        raise LinkProposalError("Invalid offline detached audit head")
    actual = export_detached_candidate_head(db, owner_scope=owner_scope, key=key)
    if (actual["candidate_count"] != head["candidate_count"]
            or not hmac.compare_digest(actual["digest"], head["digest"])):
        raise LinkProposalError("Detached offline audit head does not match the ledger")
    return {
        "anchor_matches": True, "source_authenticated": False,
        "approved": False, "live_sync": False, "reconciled": False,
    }
