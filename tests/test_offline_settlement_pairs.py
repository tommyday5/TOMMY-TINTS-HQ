"""Synthetic-only settlement comparison tests; no financial records or keys."""
import unittest

from hq_offline_settlement_pairs import preview_explicit_settlement_pairs
from hq_reconciliation_contracts import (
    ContractError, normalise_dna_rows, normalise_monzo_rows,
)


def bank(*rows, account="fixture-bank"):
    return normalise_monzo_rows([
        {"transaction_id": ref, "amount_pence": amount, "currency": "GBP",
         "booked_at": "2026-11-18T10:00:00Z"}
        for ref, amount in rows
    ], account_ref=account)


def dna(*rows, merchant="fixture-merchant"):
    return normalise_dna_rows([
        {"event_id": ref, "payment_id": payment, "kind": kind,
         "gross_pence": gross, "fee_pence": fee, "net_pence": gross - fee,
         "currency": "GBP", "occurred_at": "2026-11-18T09:00:00Z",
         "batch_id": batch}
        for ref, payment, kind, gross, fee, batch in rows
    ], merchant_ref=merchant)


PAYMENT = ("evt-1", "payment-1", "payment", 10000, 120, "batch-A")


class OfflineSettlementPairTests(unittest.TestCase):
    def test_exact_payout_is_only_an_amount_candidate(self):
        review = preview_explicit_settlement_pairs(
            bank(("bank-1", 9880)), dna(PAYMENT), {"batch-A": "bank-1"})
        self.assertEqual(review["amount_equal_candidates"], 1)
        self.assertEqual(review["credit_amount_equal_candidates"], 1)
        self.assertEqual(review["amount_mismatches"], 0)
        self.assertTrue(review["manual_proposals_only"])
        self.assertEqual(review["changes_saved"], 0)
        for flag in ("provider_authorised", "source_mapping_verified",
                     "bank_receipt_verified", "bank_balance_verified",
                     "bank_settlement_verified", "trading_revenue_verified",
                     "job_payment_verified", "reconciled"):
            self.assertIs(review[flag], False)
        self.assertNotIn("batch-A", str(review))
        self.assertNotIn("bank-1", str(review))
        self.assertNotIn("9880", str(review))

    def test_refunds_and_fees_reduce_net_but_never_double_count_bank_cash(self):
        processor = dna(
            PAYMENT,
            ("evt-refund", "payment-1", "refund", -2000, 0, "batch-A"),
            ("evt-fee", "payment-1", "fee", 0, 30, "batch-A"),
            ("evt-loose", "payment-2", "payment", 700, 10, None),
        )
        review = preview_explicit_settlement_pairs(
            bank(("bank-1", 7850)), processor, {"batch-A": "bank-1"})
        self.assertEqual(review["amount_equal_candidates"], 1)
        self.assertEqual(review["unbatched_processor_events"], 1)
        self.assertEqual(review["processor_batches_in_preview"], 1)
        self.assertEqual(review["proposed_one_to_one_links"], 1)
        self.assertFalse(review["bank_settlement_verified"])

    def test_amount_mismatch_is_not_a_success(self):
        review = preview_explicit_settlement_pairs(
            bank(("bank-1", 9900)), dna(PAYMENT), {"batch-A": "bank-1"})
        self.assertEqual(review["amount_equal_candidates"], 0)
        self.assertEqual(review["amount_mismatches"], 1)
        self.assertFalse(review["reconciled"])

    def test_negative_batch_and_bank_debit_are_not_revenue(self):
        review = preview_explicit_settlement_pairs(
            bank(("bank-debit", -2030)),
            dna(("evt-chargeback", "payment-3", "chargeback", -2000, 30, "batch-B")),
            {"batch-B": "bank-debit"})
        self.assertEqual(review["debit_amount_equal_candidates"], 1)
        self.assertEqual(review["credit_amount_equal_candidates"], 0)
        self.assertFalse(review["trading_revenue_verified"])

    def test_unknown_duplicate_and_zero_movements_fail_closed(self):
        original = dna(PAYMENT)
        b = bank(("bank-1", 9880), ("bank-2", 9880))
        with self.assertRaises(ContractError):
            preview_explicit_settlement_pairs(b, original, {"unknown": "bank-1"})
        with self.assertRaises(ContractError):
            preview_explicit_settlement_pairs(b, original, {"batch-A": "unknown"})
        two_batches = dna(
            PAYMENT, ("evt-2", "payment-2", "payment", 9880, 0, "batch-B"))
        with self.assertRaisesRegex(ContractError, "assigned twice"):
            preview_explicit_settlement_pairs(
                b, two_batches, {"batch-A": "bank-1", "batch-B": "bank-1"})
        with self.assertRaisesRegex(ContractError, "Zero"):
            preview_explicit_settlement_pairs(
                bank(("bank-1", 0)), original, {"batch-A": "bank-1"})
        with self.assertRaisesRegex(ContractError, "Zero"):
            preview_explicit_settlement_pairs(
                bank(("bank-1", 5)),
                dna(("evt-zero", "payment-1", "fee", 0, 5, "batch-Z"),
                    ("evt-five", "payment-2", "payment", 5, 0, "batch-Z")),
                {"batch-Z": "bank-1"})

    def test_unpaired_counts_never_imply_missing_bank_deposit(self):
        review = preview_explicit_settlement_pairs(
            bank(("bank-1", 9880), ("bank-2", -500)),
            dna(PAYMENT, ("evt-2", "payment-2", "payment", 2000, 20, "batch-B")),
            {})
        self.assertEqual(review["unpaired_bank_entries"], 2)
        self.assertEqual(review["unpaired_processor_batches"], 2)
        self.assertEqual(review["amount_equal_candidates"], 0)
        self.assertFalse(review["bank_settlement_verified"])

    def test_forged_preview_mixed_scope_and_unbounded_proposals_rejected(self):
        b = bank(("bank-1", 9880))
        p = dna(PAYMENT)
        forged = dict(b, events=({**b["events"][0], "account_ref": "other-bank"},))
        with self.assertRaises(ContractError):
            preview_explicit_settlement_pairs(forged, p, {"batch-A": "bank-1"})
        forged_p = dict(p, events=({**p["events"][0], "net_pence": 9881},))
        with self.assertRaises(ContractError):
            preview_explicit_settlement_pairs(b, forged_p, {"batch-A": "bank-1"})
        with self.assertRaises(ContractError):
            preview_explicit_settlement_pairs(dict(b, source_mapping_verified=True), p, {})
        with self.assertRaises(ContractError):
            preview_explicit_settlement_pairs(b, p, [("batch-A", "bank-1")])
        with self.assertRaises(ContractError):
            preview_explicit_settlement_pairs(b, p, {str(i): "bank-1" for i in range(501)})

    def test_cross_scope_cannot_accidentally_reuse_provider_evidence(self):
        review = preview_explicit_settlement_pairs(
            bank(("bank-1", 9880), account="different-bank"),
            dna(PAYMENT, merchant="different-merchant"),
            {"batch-A": "bank-1"})
        self.assertEqual(review["amount_equal_candidates"], 1)
        self.assertFalse(review["provider_authorised"])
        self.assertFalse(review["bank_settlement_verified"])

    def test_invalid_processor_events_never_gain_trust_from_pairing(self):
        b = bank(("bank-1", 9880))
        p = dna(PAYMENT)
        malformed = dict(p, events=({**p["events"][0], "merchant_ref": "other-merchant"},))
        with self.assertRaises(ContractError):
            preview_explicit_settlement_pairs(b, malformed, {"batch-A": "bank-1"})
        with self.assertRaises(ContractError):
            preview_explicit_settlement_pairs(b, dict(p, source_mapping_verified=True), {})


if __name__ == "__main__":
    unittest.main()
