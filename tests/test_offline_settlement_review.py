"""Synthetic payout review audit tests; never use real account data."""
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hq_offline_settlement_review import OfflineSettlementReviewLedger
from hq_reconciliation_contracts import (
    ContractError, normalise_dna_rows, normalise_monzo_rows,
)


def bank(amount=9880, account="fixture-bank", ref="bank-1"):
    return normalise_monzo_rows([{
        "transaction_id": ref, "amount_pence": amount,
        "currency": "GBP", "booked_at": "2026-11-18T10:00:00Z",
    }], account_ref=account)


def dna(gross=10000, fee=120, merchant="fixture-merchant", batch="batch-A"):
    return normalise_dna_rows([{
        "event_id": "evt-1", "payment_id": "payment-1",
        "kind": "payment", "gross_pence": gross, "fee_pence": fee,
        "net_pence": gross - fee, "currency": "GBP",
        "occurred_at": "2026-11-18T09:00:00Z", "batch_id": batch,
    }], merchant_ref=merchant)


class OfflineSettlementReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.sqlite3"
        self.key = b"synthetic-secret-for-audit-tests-only-32bytes"
        self.ledger = OfflineSettlementReviewLedger(self.path, hmac_key=self.key)
        self.b = bank()
        self.d = dna()

    def record(self, **changes):
        args = {
            "batch_id": "batch-A", "transaction_id": "bank-1",
            "decision": "candidate", "reason": "initial_review",
            "actor_ref": "owner-1", "request_id": "request-1",
            "expected_revision": 0,
        }
        args.update(changes)
        return self.ledger.record(self.b, self.d, **args)

    def test_persists_only_pseudonymous_review_and_false_financial_flags(self):
        event = self.record()
        self.assertEqual(event["revision"], 1)
        self.assertFalse(event["idempotent_replay"])
        self.assertFalse(event["bank_settlement_verified"])
        self.assertFalse(event["job_payment_verified"])
        self.assertFalse(event["trading_revenue_verified"])
        self.assertFalse(event["reconciled"])
        with sqlite3.connect(self.path) as db:
            stored = repr(db.execute("SELECT * FROM offline_settlement_reviews").fetchall())
        for forbidden in ("bank-1", "batch-A", "fixture-bank", "fixture-merchant",
                          "9880", "10000", "owner-1", "request-1", "payment-1"):
            self.assertNotIn(forbidden, stored)
        self.assertEqual(self.ledger.verify_integrity()["events_checked"], 1)
        history = self.ledger.history(self.b, self.d, batch_id="batch-A",
                                     transaction_id="bank-1")
        self.assertEqual(history["reviews"][0]["decision"], "candidate")
        self.assertNotIn("9880", repr(history))

    def test_exact_retry_is_idempotent_and_corrections_append(self):
        self.record()
        self.assertTrue(self.record()["idempotent_replay"])
        result = self.record(decision="investigate", reason="operator_correction",
                             request_id="request-2", expected_revision=1)
        self.assertEqual(result["revision"], 2)
        self.assertEqual(self.ledger.verify_integrity()["events_checked"], 2)
        self.assertEqual([r["decision"] for r in self.ledger.history(
            self.b, self.d, batch_id="batch-A",
            transaction_id="bank-1")["reviews"]], ["candidate", "investigate"])
        with self.assertRaisesRegex(ContractError, "stale"):
            self.record(request_id="request-3", expected_revision=1,
                        reason="new_evidence")

    def test_changed_request_mismatch_and_missing_evidence_fail_closed(self):
        self.record()
        with self.assertRaisesRegex(ContractError, "Idempotency"):
            self.record(decision="rejected")
        with self.assertRaises(ContractError):
            self.record(request_id="nope", decision="verified")
        with self.assertRaises(ContractError):
            self.record(batch_id="unknown", request_id="unknown")
        with self.assertRaises(ContractError):
            self.record(transaction_id="unknown", request_id="unknown")

    def test_mismatching_amount_can_only_be_investigated_or_rejected(self):
        self.b = bank(9000)
        with self.assertRaisesRegex(ContractError, "mismatched"):
            self.record()
        self.assertEqual(self.record(decision="investigate")["revision"], 1)

    def test_changed_normalised_evidence_rejected(self):
        self.record()
        self.b = bank(9881)
        with self.assertRaisesRegex(ContractError, "Source evidence changed"):
            self.record(reason="new_evidence", decision="investigate",
                        expected_revision=1, request_id="request-2")

    def test_cross_scope_and_double_claim_prevented(self):
        self.record()
        alt_bank = normalise_monzo_rows([
            {"transaction_id": "bank-1", "amount_pence": 9880, "currency": "GBP",
             "booked_at": "2026-11-18T10:00:00Z"},
            {"transaction_id": "bank-2", "amount_pence": 9880, "currency": "GBP",
             "booked_at": "2026-11-18T10:01:00Z"},
        ], account_ref="fixture-bank")
        with self.assertRaisesRegex(ContractError, "claims"):
            self.ledger.record(alt_bank, self.d, batch_id="batch-A",
                               transaction_id="bank-2", decision="candidate",
                               reason="initial_review", actor_ref="owner-1",
                               request_id="request-2", expected_revision=0)
        # Same provider reference under a different merchant/account has
        # distinct scoped HMAC keys and cannot overwrite the first review.
        other = self.ledger.record(
            bank(account="other-bank"), dna(merchant="other-merchant"),
            batch_id="batch-A", transaction_id="bank-1", decision="candidate",
            reason="initial_review", actor_ref="owner-1",
            request_id="request-3", expected_revision=0)
        self.assertEqual(other["revision"], 1)
        self.assertEqual(self.ledger.verify_integrity()["pairs_checked"], 2)

    def test_detects_metadata_tampering_wrong_key_and_missing_middle_event(self):
        self.record()
        self.record(decision="investigate", reason="operator_correction",
                    request_id="request-2", expected_revision=1)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE offline_settlement_reviews SET decision='rejected' WHERE seq=2")
        with self.assertRaisesRegex(ContractError, "integrity"):
            self.ledger.verify_integrity()
        with self.assertRaises(ContractError):
            self.record(reason="new_evidence", decision="investigate",
                        request_id="request-3", expected_revision=2)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE offline_settlement_reviews SET decision='investigate' WHERE seq=2")
            db.execute("DELETE FROM offline_settlement_reviews WHERE seq=1")
        with self.assertRaises(ContractError):
            self.ledger.verify_integrity()
        wrong = OfflineSettlementReviewLedger(self.path, hmac_key=b"another-audit-key-with-over-32-bytes")
        with self.assertRaises(ContractError):
            wrong.verify_integrity()

    def test_independently_retained_anchor_detects_tail_deletion_and_rollback(self):
        self.record()
        first = self.ledger.export_anchor()
        self.assertTrue(self.ledger.verify_anchor(first)["anchor_matches"])
        backup = Path(self.tmp.name) / "old.sqlite3"
        with sqlite3.connect(self.path) as source, sqlite3.connect(backup) as dest:
            source.backup(dest)
        self.record(decision="investigate", reason="operator_correction",
                    request_id="request-2", expected_revision=1)
        current = self.ledger.export_anchor()
        with self.assertRaisesRegex(ContractError, "rollback"):
            self.ledger.verify_anchor(first)
        self.assertTrue(self.ledger.verify_anchor(current)["anchor_matches"])
        shutil.copyfile(backup, self.path)
        self.assertEqual(self.ledger.verify_integrity()["events_checked"], 1)
        with self.assertRaisesRegex(ContractError, "rollback"):
            self.ledger.verify_anchor(current)

    def test_prior_anchor_verifies_append_only_prefix_but_rejects_rollback(self):
        self.record()
        original_head = self.ledger.export_anchor()
        old_backup = Path(self.tmp.name) / "before-review.sqlite3"
        with sqlite3.connect(self.path) as source, sqlite3.connect(old_backup) as dest:
            source.backup(dest)

        self.record(decision="investigate", reason="operator_correction",
                    request_id="request-2", expected_revision=1)
        # Exact verification intentionally rejects a newer ledger, while
        # prefix verification accepts additional *signed* append-only events.
        with self.assertRaisesRegex(ContractError, "rollback"):
            self.ledger.verify_anchor(original_head)
        prefix = self.ledger.verify_anchor_prefix(original_head)
        self.assertTrue(prefix["anchor_prefix_matches"])
        self.assertEqual(prefix["new_events_since_anchor"], 1)
        self.assertFalse(prefix["reconciled"])
        current_head = self.ledger.export_anchor()
        self.assertEqual(
            self.ledger.verify_anchor_prefix(current_head)["new_events_since_anchor"], 0)
        with self.assertRaises(ContractError):
            self.ledger.verify_anchor_prefix({**original_head, "events_checked": True})

        # A later review cannot conceal modifications to the signed prefix.
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE offline_settlement_reviews SET decision='rejected' WHERE seq=1")
        with self.assertRaises(ContractError):
            self.ledger.verify_anchor_prefix(original_head)

        # Even a structurally valid older SQLite copy cannot satisfy an
        # independently retained head from a later review.
        shutil.copyfile(old_backup, self.path)
        self.assertEqual(self.ledger.verify_integrity()["events_checked"], 1)
        with self.assertRaisesRegex(ContractError, "rollback"):
            self.ledger.verify_anchor_prefix(current_head)

    def test_anchor_input_and_forged_canonical_records_fail_closed(self):
        with self.assertRaises(ContractError):
            self.ledger.verify_anchor({"format": "offline-settlement-head-v1"})
        forged = dict(self.b, events=({**self.b["events"][0],
                                      "amount_pence": 9881},))
        with self.assertRaises(ContractError):
            self.ledger.record(forged, self.d, batch_id="batch-A",
                               transaction_id="bank-1", decision="investigate",
                               reason="initial_review", actor_ref="owner-1",
                               request_id="request-1", expected_revision=0)
        with self.assertRaises(ContractError):
            OfflineSettlementReviewLedger(self.path, hmac_key=b"short")


    def test_candidate_rejects_impossible_payout_posting_but_allows_investigation(self):
        self.b = normalise_monzo_rows([{
            "transaction_id": "bank-1", "amount_pence": 9880,
            "currency": "GBP", "booked_at": "2026-11-18T08:00:00Z",
        }], account_ref="fixture-bank")
        with self.assertRaisesRegex(ContractError, "pre-event bank posting"):
            self.record()
        self.assertEqual(self.ledger.verify_integrity()["events_checked"], 0)
        result = self.record(decision="investigate")
        self.assertEqual(result["revision"], 1)
        self.assertFalse(result["bank_settlement_verified"])
        self.assertTrue(self.record(decision="investigate")["idempotent_replay"])
        with self.assertRaisesRegex(ContractError, "pre-event bank posting"):
            self.record(decision="candidate", reason="operator_correction",
                        expected_revision=1, request_id="request-2")



if __name__ == "__main__":
    unittest.main()
