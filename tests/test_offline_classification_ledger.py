"""Synthetic-only regression tests for the isolated review ledger."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from hq_offline_classification_ledger import OfflineClassificationLedger
from hq_reconciliation_contracts import ContractError, normalise_monzo_rows, normalise_dna_rows


def bank(account="sample-account", *, amount=10250, transaction="fixture-txn"):
    return normalise_monzo_rows([{
        "transaction_id": transaction, "amount_pence": amount,
        "currency": "GBP", "booked_at": "2026-11-18T12:00:00Z",
    }], account_ref=account)


class OfflineClassificationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "isolated-review.sqlite3"
        self.key = b"fixture-secret-for-hmac-audit-ledger" * 2
        self.ledger = OfflineClassificationLedger(self.db_path, hmac_key=self.key)

    def submit(self, evidence=None, **changes):
        args = dict(transaction_id="fixture-txn", category="customer_receipt_candidate",
                    actor_ref="fixture-owner", request_id="fixture-operation-one",
                    expected_revision=0, reason="initial_review")
        args.update(changes)
        return self.ledger.record(evidence or bank(), **args)

    def test_initial_review_is_pending_evidence_not_verified_money(self):
        result = self.submit()
        self.assertEqual(result["revision"], 1)
        self.assertFalse(result["idempotent_replay"])
        for field in ("source_mapping_verified", "bank_receipt_verified",
                      "trading_revenue_verified", "job_payment_verified",
                      "bank_balance_verified", "reconciled"):
            self.assertFalse(result[field])
        history = self.ledger.history(bank(), transaction_id="fixture-txn")
        self.assertEqual([(r["revision"], r["category"]) for r in history["review_events"]],
                         [(1, "customer_receipt_candidate")])
        self.assertFalse(history["job_payment_verified"])

    def test_retry_is_idempotent_after_reopening_database(self):
        self.submit()
        reopened = OfflineClassificationLedger(self.db_path, hmac_key=self.key)
        result = reopened.record(bank(), transaction_id="fixture-txn",
                                 category="customer_receipt_candidate",
                                 actor_ref="fixture-owner", request_id="fixture-operation-one",
                                 expected_revision=0, reason="initial_review")
        self.assertTrue(result["idempotent_replay"])
        self.assertEqual(len(self.ledger.history(bank(), transaction_id="fixture-txn")
                             ["review_events"]), 1)

    def test_correction_appends_and_stale_revision_is_rejected(self):
        self.submit()
        corrected = self.submit(category="director_funding",
                                request_id="fixture-operation-two", expected_revision=1,
                                reason="operator_correction")
        self.assertEqual(corrected["revision"], 2)
        events = self.ledger.history(bank(), transaction_id="fixture-txn")["review_events"]
        self.assertEqual([e["category"] for e in events],
                         ["customer_receipt_candidate", "director_funding"])
        with self.assertRaisesRegex(ContractError, "stale"):
            self.submit(category="transfer", request_id="fixture-operation-three",
                        expected_revision=1, reason="operator_correction")

    def test_request_key_reuse_with_changed_payload_is_blocked(self):
        self.submit()
        for changed in (
            {"category": "transfer"},
            {"actor_ref": "different-owner"},
            {"expected_revision": 1, "reason": "operator_correction"},
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                    ContractError, "Idempotency"):
                self.submit(**changed)

    def test_changed_source_evidence_is_never_silently_reclassified(self):
        self.submit()
        changed_source = bank(amount=10500)
        with self.assertRaisesRegex(ContractError, "Conflicting source"):
            self.submit(changed_source, category="transfer",
                        request_id="fixture-operation-two", expected_revision=1,
                        reason="new_evidence")
        with self.assertRaisesRegex(ContractError, "Source evidence"):
            self.ledger.history(changed_source, transaction_id="fixture-txn")

    def test_different_account_same_provider_id_is_isolated(self):
        self.submit()
        second = bank("different-account")
        another = self.submit(second, request_id="second-account-operation")
        self.assertEqual(another["revision"], 1)
        self.assertEqual(len(self.ledger.history(bank(), transaction_id="fixture-txn")
                             ["review_events"]), 1)
        self.assertEqual(len(self.ledger.history(second, transaction_id="fixture-txn")
                             ["review_events"]), 1)

    def test_debits_dna_unknown_transactions_and_invalid_reasons_fail(self):
        with self.assertRaises(ContractError):
            self.submit(bank(amount=-100))
        with self.assertRaises(ContractError):
            self.submit(bank(), transaction_id="not-in-evidence")
        dna = normalise_dna_rows([{
            "event_id": "event-1", "payment_id": "payment-1",
            "kind": "payment", "gross_pence": 1000, "fee_pence": 20,
            "net_pence": 980, "currency": "GBP",
            "occurred_at": "2026-11-18T12:00:00Z",
        }], merchant_ref="fixture-merchant")
        with self.assertRaises(ContractError):
            self.submit(dna)
        with self.assertRaises(ContractError):
            self.submit(reason="operator_correction")
        with self.assertRaises(ContractError):
            self.submit(expected_revision=True)
        with self.assertRaises(ContractError):
            self.submit(reason="paid")

    def test_sqlite_contains_only_keyed_refs_not_raw_bank_data(self):
        self.submit()
        self.ledger.history(bank(), transaction_id="fixture-txn")
        data = self.db_path.read_bytes()
        for raw in (b"fixture-txn", b"sample-account", b"fixture-owner",
                    b"fixture-operation-one", b"10250"):
            self.assertNotIn(raw, data)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM offline_review_events").fetchone()[0], 1)

    def test_explicit_key_and_file_are_required(self):
        with self.assertRaises(ContractError):
            OfflineClassificationLedger(self.db_path, hmac_key=b"short")
        with self.assertRaises(ContractError):
            OfflineClassificationLedger(":memory:", hmac_key=self.key)


if __name__ == "__main__":
    unittest.main()
