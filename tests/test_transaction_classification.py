"""Offline classification tests: synthetic IDs and no real financial data."""
import unittest

from hq_reconciliation_contracts import ContractError, normalise_monzo_rows, normalise_dna_rows
from hq_transaction_classification import CATEGORIES, preview_manual_classifications


def rows():
    return normalise_monzo_rows([
        {"transaction_id": "tx001", "amount_pence": 10000,
         "currency": "GBP", "booked_at": "2026-11-18T11:00:00Z"},
        {"transaction_id": "tx002", "amount_pence": -2500,
         "currency": "GBP", "booked_at": "2026-11-18T11:15:00Z"},
        {"transaction_id": "tx003", "amount_pence": 9000,
         "currency": "GBP", "booked_at": "2026-11-18T11:20:00Z"},
    ], account_ref="synthetic-business")


class ClassificationReviewTests(unittest.TestCase):
    def test_manual_categories_never_verify_revenue_or_balance(self):
        preview = preview_manual_classifications(rows(), {
            "tx001": "customer_receipt_candidate",
            "tx002": "supplier_expense",
            "tx003": "director_funding",
        })
        self.assertEqual(preview["proposal_counts"], {
            "customer_receipt_candidate": 1, "director_funding": 1,
            "supplier_expense": 1,
        })
        self.assertEqual(preview["reviewed_proposals"], 3)
        self.assertEqual(preview["pending_review"], 0)
        self.assertEqual(preview["customer_receipt_candidates"], 1)
        for key in ("source_mapping_verified", "bank_receipt_verified",
                    "trading_revenue_verified", "job_payment_verified",
                    "bank_balance_verified", "reconciled"):
            self.assertFalse(preview[key])
        self.assertEqual(preview["records_imported"], 0)
        self.assertEqual(preview["classification_changes_saved"], 0)
        for private in ("tx001", "tx002", "tx003", "synthetic-business",
                        "10000", "2500"):
            self.assertNotIn(private, str(preview))

    def test_unreviewed_and_explicit_unresolved_remain_pending(self):
        preview = preview_manual_classifications(rows(), {
            "tx001": "unresolved", "tx002": "transfer",
        })
        self.assertEqual(preview["pending_review"], 2)
        self.assertEqual(preview["reviewed_proposals"], 1)
        self.assertEqual(preview["proposal_counts"]["transfer"], 1)
        self.assertNotIn("verified_revenue", CATEGORIES)

    def test_unknown_ids_or_labels_fail_closed(self):
        for proposals in (
            {"txXXX": "transfer"}, {"tx001": "paid"},
            {"tx001": "verified_revenue"}, {"tx001": 2},
            {"tx001": []}, ["transfer"], "transfer",
        ):
            with self.subTest(proposals=proposals), self.assertRaises(ContractError):
                preview_manual_classifications(rows(), proposals)

    def test_dna_events_cannot_be_misclassified_as_bank_credits(self):
        dna = normalise_dna_rows([{
            "event_id": "ev001", "payment_id": "payment001",
            "kind": "payment", "gross_pence": 10000,
            "fee_pence": 200, "net_pence": 9800,
            "currency": "GBP", "occurred_at": "2026-11-18T10:30:00Z",
        }], merchant_ref="test-merchant")
        with self.assertRaises(ContractError):
            preview_manual_classifications(dna, {})

    def test_repeated_transaction_ids_in_forged_preview_rejected(self):
        bank = rows()
        forged = {**bank, "events": (bank["events"][0], bank["events"][0])}
        with self.assertRaises(ContractError):
            preview_manual_classifications(forged, {})


if __name__ == "__main__":
    unittest.main()
