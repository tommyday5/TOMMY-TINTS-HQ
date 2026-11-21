"""Square payout arithmetic tests use synthetic IDs and amounts only."""
import unittest

from hq_offline_square_payouts import (
    normalise_square_payout_rows, preview_explicit_square_payout_pairs,
)
from hq_reconciliation_contracts import ContractError, normalise_monzo_rows


def entry(**overrides):
    row = {
        "event_id": "event-1", "payout_id": "payout-1",
        "payment_id": "payment-1", "kind": "payment",
        "gross_pence": 10000, "fee_pence": 200, "net_pence": 9800,
        "currency": "GBP", "occurred_at": "2026-11-20T10:00:00Z",
    }
    row.update(overrides)
    return row


def bank(amount=9800, when="2026-11-21T10:00:00Z"):
    return normalise_monzo_rows([{
        "transaction_id": "bank-1", "amount_pence": amount,
        "booked_at": when, "currency": "GBP",
    }], account_ref="fixture-business")


class OfflineSquarePayoutTests(unittest.TestCase):
    def test_signed_refunds_fees_and_exact_payout_arithmetic_only(self):
        preview = normalise_square_payout_rows([
            entry(), entry(event_id="refund-1", kind="refund",
                           gross_pence=-1000, fee_pence=0, net_pence=-1000),
            entry(event_id="fee-1", kind="fee", payment_id=None,
                  gross_pence=0, fee_pence=50, net_pence=-50),
            entry(),
        ], location_ref="fixture-square-location")
        self.assertEqual(preview["duplicate_rows"], 1)
        self.assertEqual(len(preview["events"]), 3)
        review = preview_explicit_square_payout_pairs(
            bank(8750), preview, {"payout-1": "bank-1"})
        self.assertEqual(review["amount_equal_candidates"], 1)
        self.assertEqual(review["amount_mismatches"], 0)
        self.assertEqual(review["changes_saved"], 0)
        for flag in ("source_mapping_verified", "provider_authorised",
                     "bank_receipt_verified", "bank_settlement_verified",
                     "job_payment_verified", "reconciled"):
            self.assertFalse(review[flag])
        self.assertNotIn("8750", str(review))
        self.assertNotIn("payment-1", str(review))
        self.assertNotIn("payout-1", str(review))

    def test_conflicting_event_replays_and_malformed_money_fail(self):
        for rows in (
            [entry(), entry(net_pence=9999)],
            [entry(fee_pence=True)],
            [entry(gross_pence=10000.0)],
            [entry(currency="USD")],
            [entry(kind="refund")],
            [entry(kind="fee")],
            [entry(kind="adjustment", gross_pence=0, fee_pence=0, net_pence=0)],
            [entry(payment_id=None)],
            [entry(payment_id="bad/id")],
            [entry(event_id="bad/id")],
            [entry(occurred_at="2026-11-20T10:00:00")],
            [entry(payout_id="")],
        ):
            with self.subTest(rows=rows), self.assertRaises(ContractError):
                normalise_square_payout_rows(rows, location_ref="location-1")

    def test_bounded_data_and_owner_exact_references(self):
        with self.assertRaises(ContractError):
            normalise_square_payout_rows([entry()] * 1001, location_ref="location-1")
        with self.assertRaises(ContractError):
            normalise_square_payout_rows([], location_ref="../location")
        with self.assertRaises(ContractError):
            normalise_square_payout_rows("not rows", location_ref="location-1")
        with self.assertRaises(ContractError):
            normalise_square_payout_rows([entry(), None], location_ref="location-1")
        source = normalise_square_payout_rows([entry()], location_ref="location-1")
        with self.assertRaises(ContractError):
            preview_explicit_square_payout_pairs(bank(), source,
                                                 {"unknown-payout": "bank-1"})
        with self.assertRaises(ContractError):
            preview_explicit_square_payout_pairs(bank(), source,
                                                 {"payout-1": "unknown-bank"})

    def test_mutated_events_or_fake_verification_cannot_gain_candidate(self):
        source = normalise_square_payout_rows([entry()], location_ref="location-1")
        event = source["events"][0]
        alterations = (
            dict(source, bank_settlement_verified=True),
            dict(source, provider_authorised=True),
            dict(source, source_mapping_verified=True),
            dict(source, events=({**event, "net_pence": 9900},)),
            dict(source, events=({**event, "location_ref": "another-location"},)),
            dict(source, events=({**event, "bank_settlement_verified": True},)),
            dict(source, events=(event, event)),
            dict(source, events=[event]),
        )
        for altered in alterations:
            with self.subTest(altered=str(altered)[:40]), self.assertRaises(ContractError):
                preview_explicit_square_payout_pairs(bank(), altered,
                                                     {"payout-1": "bank-1"})

    def test_early_bank_chronology_blocks_equal_amount_candidates(self):
        source = normalise_square_payout_rows([entry(
            occurred_at="2026-11-20T10:00:00.900Z")], location_ref="location-1")
        early = preview_explicit_square_payout_pairs(
            bank(9800, "2026-11-20T10:00:00.100Z"),
            source, {"payout-1": "bank-1"})
        self.assertEqual(early["chronology_conflicts"], 1)
        self.assertEqual(early["amount_equal_candidates"], 0)
        late = preview_explicit_square_payout_pairs(
            bank(9800, "2026-11-20T10:00:00.950Z"),
            source, {"payout-1": "bank-1"})
        self.assertEqual(late["amount_equal_candidates"], 1)

    def test_negative_payout_must_remain_a_debit_candidate(self):
        source = normalise_square_payout_rows([
            entry(event_id="refund", kind="refund",
                  gross_pence=-1000, fee_pence=0, net_pence=-1000)
        ], location_ref="location-1")
        result = preview_explicit_square_payout_pairs(
            bank(-1000), source, {"payout-1": "bank-1"})
        self.assertEqual(result["negative_amount_equal_candidates"], 1)
        self.assertFalse(result["job_payment_verified"])
        self.assertFalse(result["bank_receipt_verified"])


if __name__ == "__main__":
    unittest.main()
