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
import re
from pathlib import Path
import sqlite3

from hq_reconciliation_contracts import ContractError, _ref, _timestamp
from hq_transaction_classification import CATEGORIES, preview_manual_classifications


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
                signature_version INTEGER NOT NULL DEFAULT 1,
                event_key TEXT,
                UNIQUE(evidence_key, revision)
            )""")
            # Legacy fixture databases remain readable; only new events use v2
            # HMAC seals. Never silently re-sign old evidence as newly trusted.
            columns = {row[1] for row in db.execute(
                "PRAGMA table_info(offline_review_events)")}
            if "signature_version" not in columns:
                db.execute("""ALTER TABLE offline_review_events ADD COLUMN
                    signature_version INTEGER NOT NULL DEFAULT 1""")
            if "event_key" not in columns:
                db.execute("ALTER TABLE offline_review_events ADD COLUMN event_key TEXT")
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

    def _verify(self, db):
        """Verify existing synthetic review rows before trusting their history.

        Detects changed signed fields, revision gaps, malformed timestamps and
        SQLite damage. It cannot prove source authenticity or detect deletion
        of a final revision without an external independently stored anchor.
        Legacy v1 events do not authenticate request key/timestamp. New v2
        events separately HMAC-seal both; no in-database seal detects a
        complete rollback or deletion of the most recent event.
        """
        try:
            check = db.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise ContractError("SQLite integrity check failed")
            rows = db.execute("""SELECT request_key, payload_key, scope_key,
                evidence_key, source_key, revision, category, reason,
                actor_key, recorded_at, signature_version, event_key
                FROM offline_review_events ORDER BY evidence_key, revision""").fetchall()
            last = {}
            for (request_key, payload_key, scope_key, evidence_key, source_key,
                 revision, category, reason, actor_key, recorded_at,
                 signature_version, event_key) in rows:
                keys = (request_key, payload_key, scope_key, evidence_key,
                        source_key, actor_key)
                if any(not isinstance(key, str) or not re.fullmatch(
                        r"[0-9a-f]{64}", key) for key in keys):
                    raise ContractError("Malformed keyed review evidence")
                if (type(revision) is not int or not 1 <= revision <= 100000
                        or category not in CATEGORIES
                        or (reason != "initial_review" if revision == 1
                            else reason not in ("operator_correction", "new_evidence"))
                        or not isinstance(recorded_at, str)
                        or _timestamp(recorded_at) != recorded_at):
                    raise ContractError("Malformed review revision or timestamp")
                if type(signature_version) is not int or signature_version not in (1, 2):
                    raise ContractError("Unsupported review signature version")
                previous = last.get(evidence_key)
                if (previous is None and revision != 1
                        or previous is not None
                        and (revision != previous[0] + 1
                             or scope_key != previous[1]
                             or source_key != previous[2])):
                    raise ContractError("Review revision sequence has changed")
                intent = (scope_key, evidence_key, source_key, category,
                          actor_key, revision - 1, reason)
                expected = self._digest(
                    "payload-v2" if signature_version == 2 else "payload", *intent)
                if not hmac.compare_digest(payload_key, expected):
                    raise ContractError("Signed review fields have changed")
                if signature_version == 1:
                    if event_key is not None:
                        raise ContractError("Unexpected legacy review seal")
                else:
                    if not isinstance(event_key, str) or not re.fullmatch(
                            r"[0-9a-f]{64}", event_key):
                        raise ContractError("Missing review metadata seal")
                    expected_event = self._digest(
                        "event-v2", request_key, payload_key, scope_key,
                        evidence_key, source_key, revision, category, reason,
                        actor_key, recorded_at)
                    if not hmac.compare_digest(event_key, expected_event):
                        raise ContractError("Review metadata integrity check failed")
                last[evidence_key] = (revision, scope_key, source_key)
        except (sqlite3.Error, ValueError, TypeError):
            raise ContractError(
                "Offline review audit integrity could not be verified") from None
        return {"events_checked": len(rows), "evidence_chains_checked": len(last),
                "audit_structure_valid": True, "offline_fixture_only": True,
                **self._flags()}

    def verify_integrity(self):
        """Read-only fixture audit. No live-provider or finance access."""
        try:
            with sqlite3.connect(self._path, timeout=5) as db:
                return self._verify(db)
        except sqlite3.Error:
            raise ContractError(
                "Offline review audit integrity could not be verified") from None

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
        intent = (scope_key, evidence_key, source_key, category,
                  actor_key, expected_revision, reason)
        payload_key = self._digest("payload-v2", *intent)
        with sqlite3.connect(self._path, timeout=5) as db:
            db.execute("BEGIN IMMEDIATE")
            self._verify(db)
            existing = db.execute(
                "SELECT payload_key, revision, signature_version FROM offline_review_events WHERE request_key=?",
                (request_key,)).fetchone()
            if existing is not None:
                expected_key = (payload_key if existing[2] == 2 else
                                self._digest("payload", *intent))
                if existing[0] != expected_key:
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
            recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            event_key = self._digest(
                "event-v2", request_key, payload_key, scope_key, evidence_key,
                source_key, revision, category, reason, actor_key, recorded_at)
            db.execute("""INSERT INTO offline_review_events
                (request_key, payload_key, scope_key, evidence_key, source_key,
                 revision, category, reason, actor_key, recorded_at,
                 signature_version, event_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_key, payload_key, scope_key, evidence_key, source_key,
                 revision, category, reason, actor_key, recorded_at, 2, event_key))
        return {"revision": revision, "idempotent_replay": False, **self._flags()}

    def history(self, bank_preview, *, transaction_id):
        """Return review decisions only, without bank identifiers or amounts."""
        _ref(transaction_id, "transaction ID")
        _, evidence_key, source_key = self._source(
            bank_preview, transaction_id, "unresolved")
        with sqlite3.connect(self._path, timeout=5) as db:
            self._verify(db)
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
