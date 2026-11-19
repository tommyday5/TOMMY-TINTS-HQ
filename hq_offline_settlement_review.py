"""Synthetic-only audit of explicit DNA-to-Monzo payout review decisions.

This module is NEVER registered with Flask. No real export parser, credentials,
customer data, settlement verification or live finance writes are supported.
Even a matching payout is a candidate, never a verified bank receipt.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3

from hq_offline_settlement_pairs import preview_explicit_settlement_pairs
from hq_reconciliation_contracts import (
    ContractError, _ref, _validated_monzo_events, _validated_dna_events,
    preview_processor_batches,
)

_DECISIONS = frozenset({"candidate", "investigate", "rejected"})
_REASONS = frozenset({"initial_review", "operator_correction", "new_evidence"})
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _unverified():
    return {
        "offline_fixture_only": True, "source_mapping_verified": False,
        "provider_authorised": False, "bank_receipt_verified": False,
        "bank_balance_verified": False, "bank_settlement_verified": False,
        "trading_revenue_verified": False, "job_payment_verified": False,
        "reconciled": False,
    }


class OfflineSettlementReviewLedger:
    """Append-only logical SQLite revisions; physical DB is NOT immutable.

    References, account scopes and underlying amounts are HMAC-bound, not
    stored raw. The database itself is not encrypted. HMAC key must be kept
    outside this fixture and the detached head retained independently.
    """

    def __init__(self, path, *, hmac_key):
        if not isinstance(hmac_key, bytes) or len(hmac_key) < 32:
            raise ContractError("Independent 32-byte audit HMAC key required")
        if not isinstance(path, (str, Path)) or str(path) in ("", ":memory:"):
            raise ContractError("Explicit fixture SQLite path required")
        self._path, self._key = str(path), hmac_key
        try:
            with sqlite3.connect(self._path, timeout=5) as db:
                db.execute("""CREATE TABLE IF NOT EXISTS offline_settlement_reviews (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_key TEXT NOT NULL UNIQUE,
                    pair_key TEXT NOT NULL,
                    bank_claim_key TEXT NOT NULL,
                    batch_claim_key TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    actor_key TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    UNIQUE(pair_key, revision)
                )""")
        except sqlite3.Error:
            raise ContractError("Fixture settlement review database unavailable") from None

    def _h(self, domain, *parts):
        payload = json.dumps([domain, *parts], sort_keys=True,
                             separators=(",", ":"), ensure_ascii=True)
        return hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).hexdigest()

    def _source(self, bank_preview, dna_preview, batch_id, transaction_id):
        _ref(batch_id, "processor batch ID")
        _ref(transaction_id, "bank transaction ID")
        result = preview_explicit_settlement_pairs(
            bank_preview, dna_preview, {batch_id: transaction_id})
        bank = _validated_monzo_events(bank_preview)
        processor = _validated_dna_events(dna_preview)
        bank_row = next(row for row in bank if row["transaction_id"] == transaction_id)
        batch_rows = [row for row in processor if row["batch_id"] == batch_id]
        if not batch_rows:
            raise ContractError("Unknown processor batch")
        bank_scope = bank_row["account_ref"]
        merchant_scope = batch_rows[0]["merchant_ref"]
        pair = self._h("pair", merchant_scope, batch_id, bank_scope, transaction_id)
        bank_claim = self._h("bank-claim", bank_scope, transaction_id)
        batch_claim = self._h("batch-claim", merchant_scope, batch_id)
        source = self._h("source-v1", pair, bank_row, batch_rows)
        return pair, bank_claim, batch_claim, source, bool(
            result["amount_equal_candidates"])

    def _verify(self, db):
        try:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ContractError("Review SQLite integrity failed")
            rows = db.execute("""SELECT seq, request_key, pair_key, bank_claim_key,
                batch_claim_key, source_key, revision, decision, reason, actor_key,
                recorded_at, event_key FROM offline_settlement_reviews
                ORDER BY seq""").fetchall()
            latest = {}
            claimed_banks, claimed_batches = {}, {}
            for expected_seq, row in enumerate(rows, start=1):
                (seq, request, pair, bank, batch, source, rev, decision,
                 reason, actor, stamp, seal) = row
                if (seq != expected_seq or type(rev) is not int or rev < 1
                        or rev > 100000 or decision not in _DECISIONS
                        or (reason != "initial_review" if rev == 1
                            else reason not in ("operator_correction", "new_evidence"))
                        or any(not isinstance(v, str) or not _HASH.fullmatch(v)
                               for v in (request, pair, bank, batch, source, actor, seal))
                        or not isinstance(stamp, str)
                        or len(stamp) > 40):
                    raise ContractError("Settlement audit metadata invalid")
                try:
                    parsed = datetime.fromisoformat(stamp)
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        raise ValueError("timezone missing")
                except ValueError:
                    raise ContractError("Settlement audit timestamp invalid") from None
                if pair in latest:
                    previous_rev, previous_bank, previous_batch, previous_source = latest[pair]
                    if (rev != previous_rev + 1 or bank != previous_bank
                            or batch != previous_batch or source != previous_source):
                        raise ContractError("Settlement audit revisions altered")
                elif rev != 1:
                    raise ContractError("Settlement initial revision missing")
                if (bank in claimed_banks and claimed_banks[bank] != pair
                        or batch in claimed_batches and claimed_batches[batch] != pair):
                    raise ContractError("Settlement evidence assigned to multiple pairs")
                claimed_banks[bank], claimed_batches[batch] = pair, pair
                expected = self._h("event-v1", *row[:-1])
                if not hmac.compare_digest(expected, seal):
                    raise ContractError("Settlement audit event integrity failed")
                latest[pair] = rev, bank, batch, source
            return rows, latest, claimed_banks, claimed_batches
        except sqlite3.Error:
            raise ContractError("Settlement audit could not be checked") from None

    def verify_integrity(self):
        with sqlite3.connect(self._path, timeout=5) as db:
            rows, latest, _, _ = self._verify(db)
        return {"events_checked": len(rows), "pairs_checked": len(latest),
                "audit_structure_valid": True, **_unverified()}

    def record(self, bank_preview, dna_preview, *, batch_id, transaction_id,
               decision, reason, actor_ref, request_id, expected_revision):
        """Record an owner-*proposed* offline status; no approval/funds action."""
        if decision not in _DECISIONS or reason not in _REASONS:
            raise ContractError("Unsupported offline review decision or reason")
        if type(expected_revision) is not int or not 0 <= expected_revision < 100000:
            raise ContractError("Explicit expected revision required")
        if (expected_revision == 0) != (reason == "initial_review"):
            raise ContractError("Review reason is inconsistent with revision")
        _ref(actor_ref, "reviewer reference")
        _ref(request_id, "idempotency reference")
        pair, bank_claim, batch_claim, source, equal = self._source(
            bank_preview, dna_preview, batch_id, transaction_id)
        if decision == "candidate" and not equal:
            raise ContractError("A mismatched amount cannot be labelled a candidate")
        request = self._h("request", request_id)
        actor = self._h("actor", actor_ref)
        with sqlite3.connect(self._path, timeout=5) as db:
            db.execute("BEGIN IMMEDIATE")
            rows, latest, claimed_banks, claimed_batches = self._verify(db)
            prior_request = db.execute("""SELECT pair_key, bank_claim_key,
                batch_claim_key, source_key, revision, decision, reason, actor_key
                FROM offline_settlement_reviews WHERE request_key=?""", (request,)).fetchone()
            expected_request = (pair, bank_claim, batch_claim, source,
                                expected_revision + 1, decision, reason, actor)
            if prior_request:
                if prior_request != expected_request:
                    raise ContractError("Idempotency reference reused for different review")
                return {"revision": prior_request[4], "idempotent_replay": True, **_unverified()}
            if (bank_claim in claimed_banks and claimed_banks[bank_claim] != pair
                    or batch_claim in claimed_batches and claimed_batches[batch_claim] != pair):
                raise ContractError("Another pair already claims this payout evidence")
            current = latest.get(pair)
            if current and current[3] != source:
                raise ContractError("Source evidence changed; investigate separately")
            if (current[0] if current else 0) != expected_revision:
                raise ContractError("Settlement review revision stale")
            seq = len(rows) + 1
            revision = expected_revision + 1
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            fields = (seq, request, pair, bank_claim, batch_claim, source,
                      revision, decision, reason, actor, stamp)
            db.execute("""INSERT INTO offline_settlement_reviews
                (seq, request_key, pair_key, bank_claim_key, batch_claim_key,
                 source_key, revision, decision, reason, actor_key, recorded_at, event_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*fields, self._h("event-v1", *fields)))
        return {"revision": revision, "idempotent_replay": False, **_unverified()}

    def history(self, bank_preview, dna_preview, *, batch_id, transaction_id):
        pair, _, _, source, _ = self._source(
            bank_preview, dna_preview, batch_id, transaction_id)
        with sqlite3.connect(self._path, timeout=5) as db:
            self._verify(db)
            rows = db.execute("""SELECT revision, decision, reason, recorded_at, source_key
                FROM offline_settlement_reviews WHERE pair_key=?
                ORDER BY revision""", (pair,)).fetchall()
        if any(row[4] != source for row in rows):
            raise ContractError("Review source changed")
        return {"reviews": [{"revision": rev, "decision": decision,
                              "reason": reason, "recorded_at": stamp}
                             for rev, decision, reason, stamp, _ in rows],
                **_unverified()}

    def export_anchor(self):
        """Export only: caller must store prior head separately from SQLite."""
        with sqlite3.connect(self._path, timeout=5) as db:
            db.execute("BEGIN")
            rows, _, _, _ = self._verify(db)
        return {"format": "offline-settlement-head-v1", "events_checked": len(rows),
                "head_hmac": self._h("detached-head-v1", rows)}

    def verify_anchor(self, previous):
        if (not isinstance(previous, dict)
                or set(previous) != {"format", "events_checked", "head_hmac"}
                or previous["format"] != "offline-settlement-head-v1"
                or type(previous["events_checked"]) is not int
                or previous["events_checked"] < 0
                or not isinstance(previous["head_hmac"], str)
                or not _HASH.fullmatch(previous["head_hmac"])):
            raise ContractError("Independently retained settlement audit head required")
        current = self.export_anchor()
        if (current["events_checked"] != previous["events_checked"]
                or not hmac.compare_digest(current["head_hmac"], previous["head_hmac"])):
            raise ContractError("Settlement audit rollback or modification detected")
        return {"anchor_matches": True, **_unverified()}
