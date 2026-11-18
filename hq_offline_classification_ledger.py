"""Offline, fixture-only classification review audit ledger.

NOT imported by the Flask application. This deliberately accepts no live
provider credentials, CSVs, payment allocations, or bank balances. Production
wiring requires independently verified mappings, owner auth, encrypted storage
and a tested restore plan. A review is NEVER proof of trading revenue.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3

from hq_reconciliation_contracts import ContractError, _ref, _timestamp
from hq_transaction_classification import preview_manual_classifications


_REASONS = frozenset({"initial_review", "operator_correction", "new_evidence"})


class OfflineClassificationLedger:
    """Append-only SQLite review events, explicitly located by the caller.

    Keep the HMAC key outside SQLite. Reference HMACs avoid persisting bank
    account and transaction IDs; source amounts/timestamps are not stored.
    This is only an engineering fixture, not an authorised HQ finance ledger.
    """

    def __init__(self, path, *, hmac_key):
        if not isinstance(hmac_key, bytes) or len(hmac_key) < 32:
            raise ContractError("A separate 32-byte-or-longer audit HMAC key is required")
        if not isinstance(path, (str, Path)) or not str(path) or str(path) == ":memory:":
            raise ContractError("An explicit persistent test database path is required")
        self._path = str(path)
        self._key = hmac_key
        with sqlite3.connect(self._path, timeout=5) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS offline_review_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                request_key TEXT NOT NULL UNIQUE,
                payload_key TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                evidence_key TEXT NOT NULL,
                source_key TEXT NOT NULL,
                revision INTEGER NOT NULL,
                category TEXT NOT NULL,
                reason TEXT NOT NULL,
                actor_key TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(evidence_key, revision)
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS offline_review_by_evidence
                ON offline_review_events(evidence_key, revision)""")

    def _digest(self, domain, *parts):
        raw = json.dumps([domain, *parts], separators=(",", ":"), ensure_ascii=True)
        return hmac.new(self._key, raw.encode("ascii"), hashlib.sha256).hexdigest()

    def _source(self, bank_preview, transaction_id, category):
        preview_manual_classifications(bank_preview, {transaction_id: category})
        event = next(event for event in bank_preview["events"]
                     if event["transaction_id"] == transaction_id)
        when = event.get("occurred_at")
        if not isinstance(when, str) or _timestamp(when) != when:
            raise ContractError("Canonical UTC source timestamp is required")
        scope = event["account_ref"]
        evidence_key = self._digest("evidence", "monzo", scope, transaction_id)
        return (
            self._digest("scope", "monzo", scope),
            evidence_key,
            self._digest("source", "monzo", scope, transaction_id,
                         event["amount_pence"], when, "GBP"),
        )

    @staticmethod
    def _flags():
        return {
            "source_mapping_verified": False,
            "bank_receipt_verified": False,
            "trading_revenue_verified": False,
            "job_payment_verified": False,
            "bank_balance_verified": False,
            "reconciled": False,
        }

    def record(self, bank_preview, *, transaction_id, category, actor_ref,
               request_id, expected_revision, reason):
        """Record one explicitly proposed classification, atomically.

        First review expects revision 0 and reason initial_review. Corrections
        require the current revision and a correction reason. Identical retries
        of the same request are idempotent; changed reuse fails closed.
        """
        _ref(transaction_id, "transaction ID")
        _ref(actor_ref, "actor reference")
        _ref(request_id, "idempotency reference")
        if type(expected_revision) is not int or not 0 <= expected_revision < 100000:
            raise ContractError("Explicit nonnegative expected revision is required")
        if reason not in _REASONS:
            raise ContractError("Unsupported review reason")
        if (expected_revision == 0) != (reason == "initial_review"):
            raise ContractError("Initial review and correction reasons must be distinct")
        scope_key, evidence_key, source_key = self._source(
            bank_preview, transaction_id, category)
        actor_key = self._digest("actor", actor_ref)
        request_key = self._digest("request", request_id)
        payload_key = self._digest("payload", scope_key, evidence_key, source_key,
                                   category, actor_key, expected_revision, reason)
        with sqlite3.connect(self._path, timeout=5) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT payload_key, revision FROM offline_review_events WHERE request_key=?",
                (request_key,)).fetchone()
            if existing is not None:
                if existing[0] != payload_key:
                    raise ContractError("Idempotency reference was reused for different input")
                return {"revision": existing[1], "idempotent_replay": True, **self._flags()}
            head = db.execute("""SELECT revision, source_key FROM offline_review_events
                WHERE evidence_key=? ORDER BY revision DESC LIMIT 1""",
                (evidence_key,)).fetchone()
            current = head[0] if head else 0
            if head and head[1] != source_key:
                raise ContractError("Conflicting source evidence requires separate investigation")
            if current != expected_revision:
                raise ContractError("Review revision is stale; reload before correcting")
            revision = current + 1
            db.execute("""INSERT INTO offline_review_events
                (request_key, payload_key, scope_key, evidence_key, source_key,
                 revision, category, reason, actor_key, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_key, payload_key, scope_key, evidence_key, source_key,
                 revision, category, reason, actor_key,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
        return {"revision": revision, "idempotent_replay": False, **self._flags()}

    def history(self, bank_preview, *, transaction_id):
        """Return review decisions only, without bank identifiers or amounts."""
        _ref(transaction_id, "transaction ID")
        _, evidence_key, source_key = self._source(
            bank_preview, transaction_id, "unresolved")
        with sqlite3.connect(self._path, timeout=5) as db:
            rows = db.execute("""SELECT revision, category, reason, recorded_at, source_key
                FROM offline_review_events WHERE evidence_key=?
                ORDER BY revision ASC""", (evidence_key,)).fetchall()
        if any(row[4] != source_key for row in rows):
            raise ContractError("Source evidence has changed since review")
        return {
            "review_events": [{"revision": row[0], "category": row[1],
                               "reason": row[2], "recorded_at": row[3]}
                              for row in rows],
            "reviewed": bool(rows),
            **self._flags(),
        }
