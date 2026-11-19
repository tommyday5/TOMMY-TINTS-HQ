"""Offline classification tests: synthetic IDs and no real financial data."""
import unittest

from hq_reconciliation_contracts import (
    ContractError, _event_digest, normalise_monzo_rows, normalise_dna_rows,
)
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

    def test_direction_of_receipts_funding_and_drawings_is_checked(self):
        bank = rows()
        for proposals in (
            {"tx002": "customer_receipt_candidate"},
            {"tx002": "director_funding"},
            {"tx001": "director_drawings"},
        ):
            with self.subTest(proposals=proposals), self.assertRaises(ContractError):
                preview_manual_classifications(bank, proposals)
        # A refund, loan or transfer can legitimately have either sign.
        result = preview_manual_classifications(bank, {
            "tx001": "refund", "tx002": "transfer",
            "tx003": "customer_receipt_candidate",
        })
        self.assertEqual(result["customer_receipt_candidates"], 1)
        self.assertFalse(result["bank_receipt_verified"])
        zero = normalise_monzo_rows([{
            "transaction_id": "zero", "amount_pence": 0,
            "currency": "GBP", "booked_at": "2026-11-18T11:00:00Z",
        }], account_ref="synthetic-business")
        with self.assertRaises(ContractError):
            preview_manual_classifications(zero, {"zero": "customer_receipt_candidate"})

    def test_forged_or_mixed_account_bank_evidence_fails_closed(self):
        bank = rows()
        first = bank["events"][0]
        second = bank["events"][1]
        for changed in (
            {**first, "amount_pence": "10000"},
            {**first, "currency": "EUR"},
            {**first, "account_ref": "other-account"},
            {**first, "evidence_id": "monzo:wrong:tx001"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ContractError):
                preview_manual_classifications({**bank, "events": (changed,)}, {})
        mixed = {**second, "account_ref": "other-account",
                 "evidence_id": "monzo:other-account:tx002"}
        with self.assertRaises(ContractError):
            preview_manual_classifications({**bank, "events": (first, mixed)}, {})

    def test_valid_looking_mutation_cannot_be_classified_as_bank_credit(self):
        bank = rows()
        # The debit now looks like a credit, but its original canonical
        # fingerprint must be checked BEFORE any classification decision.
        changed = {**bank["events"][1], "amount_pence": 2500}
        forged = {**bank, "events": (
            bank["events"][0], changed, bank["events"][2],
        )}
        with self.assertRaises(ContractError):
            preview_manual_classifications(
                forged, {"tx002": "customer_receipt_candidate"})

        # A different valid timestamp or sequence must not silently alter
        # which statement evidence is being reviewed.
        altered_time = {**bank["events"][0],
                        "occurred_at": "2026-11-18T11:05:00+00:00"}
        with self.assertRaises(ContractError):
            preview_manual_classifications({
                **bank, "events": (altered_time, *bank["events"][1:]),
            }, {"tx001": "customer_receipt_candidate"})
        with self.assertRaises(ContractError):
            preview_manual_classifications({
                **bank, "events": tuple(reversed(bank["events"])),
            }, {"tx001": "customer_receipt_candidate"})

    def test_review_rejects_forged_verification_flags_and_rewritten_digest(self):
        bank = rows()
        # The local digest alone is not a provider signature. Even if a
        # caller recomputes it, inconsistent canonical event fields fail.
        forged_events = ({**bank["events"][0], "bank_receipt_verified": True},
                         *bank["events"][1:])
        forged = {**bank, "events": forged_events,
                  "canonical_event_digest": _event_digest(forged_events)}
        with self.assertRaises(ContractError):
            preview_manual_classifications(
                forged, {"tx001": "customer_receipt_candidate"})
        for changed in (
            {**bank, "canonical_event_digest": None},
            {**bank, "source_mapping_verified": True},
            {**bank, "bank_balance_verified": True},
            {**bank, "verified_trading_receipts_pence": 10000},
            {**bank, "reconciled": True},
        ):
            with self.subTest(changed_fields=set(changed) - set(bank) or "flags"), self.assertRaises(ContractError):
                preview_manual_classifications(
                    changed, {"tx001": "customer_receipt_candidate"})

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
