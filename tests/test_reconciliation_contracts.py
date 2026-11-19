"""Synthetic contract tests. No bank exports, credentials or customer data."""
import unittest

from hq_reconciliation_contracts import (
    ContractError, normalise_dna_rows, normalise_monzo_rows,
    preview_bank_rollforward, preview_processor_batches,
)


def bank(**changes):
    row = {
        "transaction_id": "tx-001", "amount_pence": 12530,
        "currency": "GBP", "booked_at": "2026-11-18T10:30:00Z",
    }
    row.update(changes)
    return row


def card(**changes):
    row = {
        "event_id": "evt-001", "payment_id": "payment-001",
        "kind": "payment", "gross_pence": 10000, "fee_pence": 120,
        "net_pence": 9880, "currency": "GBP",
        "occurred_at": "2026-11-18T10:30:00+00:00", "batch_id": "batch-A",
    }
    row.update(changes)
    return row


class MonzoContractTests(unittest.TestCase):
    def test_exact_ids_signed_pence_and_timezone_are_preserved(self):
        preview = normalise_monzo_rows([
            bank(), bank(transaction_id="tx-002", amount_pence=-790,
                         booked_at="2026-11-18T11:30:00+01:00"),
        ], account_ref="monzo-business")
        self.assertEqual(len(preview["events"]), 2)
        self.assertEqual({e["amount_pence"] for e in preview["events"]}, {12530, -790})
        self.assertEqual({e["occurred_at"] for e in preview["events"]},
                         {"2026-11-18T10:30:00+00:00"})
        self.assertTrue(all(e["classification"] is None for e in preview["events"]))
        self.assertTrue(all(not e["bank_receipt_verified"] for e in preview["events"]))
        self.assertIsNone(preview["verified_trading_receipts_pence"])
        self.assertFalse(preview["reconciled"])

    def test_identical_replay_dedupes_but_changed_history_aborts(self):
        preview = normalise_monzo_rows([bank(), bank()], account_ref="account-A")
        self.assertEqual(len(preview["events"]), 1)
        self.assertEqual(preview["duplicate_rows"], 1)
        with self.assertRaises(ContractError):
            normalise_monzo_rows([bank(), bank(amount_pence=14530)], account_ref="account-A")

    def test_cross_account_scoping_prevents_id_collision(self):
        a = normalise_monzo_rows([bank()], account_ref="account-A")
        b = normalise_monzo_rows([bank()], account_ref="account-B")
        self.assertNotEqual(a["events"][0]["evidence_id"], b["events"][0]["evidence_id"])

    def test_rejects_float_boolean_invalid_currency_and_naive_datetime(self):
        for bad in (
            {"amount_pence": 4.99}, {"amount_pence": True},
            {"amount_pence": "499"}, {"currency": "USD"},
            {"booked_at": "2026-11-18T10:30:00"}, {"transaction_id": "../bad"},
            {"transaction_id": ""}, {"booked_at": 42},
        ):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                normalise_monzo_rows([bank(**bad)], account_ref="business")

    def test_rejects_invalid_account_and_unbounded_batches(self):
        for account in ("", "business:account", "../test"):
            with self.subTest(account=account), self.assertRaises(ContractError):
                normalise_monzo_rows([bank()], account_ref=account)
        with self.assertRaises(ContractError):
            normalise_monzo_rows([bank()] * 5001, account_ref="business")
        with self.assertRaises(ContractError):
            normalise_monzo_rows({"transaction_id": "x"}, account_ref="business")

    def test_rollforward_matches_are_arithmetic_only(self):
        preview = normalise_monzo_rows([
            bank(amount_pence=10000),
            bank(transaction_id="tx-002", amount_pence=-500),
        ], account_ref="business")
        result = preview_bank_rollforward(preview, opening_pence=20000, closing_pence=29500)
        self.assertTrue(result["arithmetically_consistent"])
        self.assertEqual(result["difference_pence"], 0)
        self.assertFalse(result["bank_balance_verified"])
        self.assertFalse(result["reconciled"])
        bad = preview_bank_rollforward(preview, opening_pence=20000, closing_pence=29499)
        self.assertEqual(bad["difference_pence"], -1)
        self.assertFalse(bad["arithmetically_consistent"])
        unknown = preview_bank_rollforward(preview)
        self.assertIsNone(unknown["expected_closing_pence"])
        self.assertIsNone(unknown["arithmetically_consistent"])

    def test_arithmetic_rejects_pseudonumeric_balances(self):
        preview = normalise_monzo_rows([], account_ref="business")
        with self.assertRaises(ContractError):
            preview_bank_rollforward(preview, opening_pence=True, closing_pence=100)
        with self.assertRaises(ContractError):
            preview_bank_rollforward({}, opening_pence=0, closing_pence=0)

    def test_rollforward_revalidates_mutable_canonical_bank_preview(self):
        original = normalise_monzo_rows([
            bank(), bank(transaction_id="tx-002", amount_pence=-700),
        ], account_ref="account-A")
        first, second = original["events"]
        alterations = (
            dict(original, source_mapping_verified=True),
            dict(original, bank_balance_verified=True),
            dict(original, verified_trading_receipts_pence=11830),
            dict(original, events=(first, first)),
            dict(original, events=({**first, "account_ref": "account-B"}, second)),
            dict(original, events=({**first, "amount_pence": 12531}, second)),
            dict(original, events=({**first, "classification": "customer_receipt_candidate"}, second)),
            dict(original, events=({**first, "currency": "EUR"}, second)),
            dict(original, events=[first, second]),
            dict(original, events=(first, {"provider": "monzo"})),
            dict(original, events=(first,) * 5001),
        )
        for modified in alterations:
            with self.subTest(modification=str(modified)[:80]), self.assertRaises(ContractError):
                preview_bank_rollforward(modified, opening_pence=100, closing_pence=11930)



class DnaContractTests(unittest.TestCase):
    def test_refunds_chargebacks_and_fees_remain_signed_provider_events(self):
        preview = normalise_dna_rows([
            card(),
            card(event_id="evt-refund", kind="refund", gross_pence=-2500,
                 fee_pence=0, net_pence=-2500, payment_id="payment-001"),
            card(event_id="evt-chargeback", kind="chargeback", gross_pence=-3000,
                 fee_pence=100, net_pence=-3100, payment_id="payment-002"),
            card(event_id="evt-fee", kind="fee", gross_pence=0,
                 fee_pence=75, net_pence=-75, payment_id="payment-003"),
        ], merchant_ref="merchant-A")
        summary = preview_processor_batches(preview)
        self.assertEqual(summary["batches"]["batch-A"]["event_count"], 4)
        self.assertEqual(summary["batches"]["batch-A"]["net_pence"], 4205)
        self.assertFalse(summary["bank_settlement_verified"])
        self.assertFalse(summary["job_receipts_verified"])
        self.assertIsNone(preview["verified_job_receipts_pence"])

    def test_identical_replay_and_changed_same_event(self):
        preview = normalise_dna_rows([card(), card()], merchant_ref="merchant-A")
        self.assertEqual(preview["duplicate_rows"], 1)
        self.assertEqual(len(preview["events"]), 1)
        with self.assertRaises(ContractError):
            normalise_dna_rows([card(), card(fee_pence=125, net_pence=9875)],
                               merchant_ref="merchant-A")

    def test_rejects_inconsistent_money_and_kind_signs(self):
        for bad in (
            {"net_pence": 10000}, {"gross_pence": 99.0},
            {"fee_pence": -10}, {"fee_pence": True}, {"kind": "settlement"},
            {"kind": "refund"}, {"kind": "fee"}, {"kind": []},
            {"kind": "payment", "gross_pence": -100, "net_pence": -220},
            {"currency": "EUR"}, {"batch_id": "bad/ref"},
            {"occurred_at": "2026-11-18T10:30:00"}, {"payment_id": ""},
        ):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                normalise_dna_rows([card(**bad)], merchant_ref="merchant-A")

    def test_unbatched_events_cannot_be_silently_applied_to_settlement(self):
        preview = normalise_dna_rows([card(batch_id=None)], merchant_ref="merchant-A")
        self.assertEqual(preview_processor_batches(preview)["batches"], {})
        self.assertFalse(preview["source_mapping_verified"])
        self.assertFalse(preview["bank_settlement_verified"])

    def test_cross_merchant_ids_remain_distinct(self):
        a = normalise_dna_rows([card()], merchant_ref="merchant-A")
        b = normalise_dna_rows([card()], merchant_ref="merchant-B")
        self.assertNotEqual(a["events"][0]["evidence_id"], b["events"][0]["evidence_id"])

    def test_processor_batch_summary_revalidates_mutable_canonical_preview(self):
        original = normalise_dna_rows([card()], merchant_ref="merchant-A")
        event = original["events"][0]
        alterations = (
            dict(original, source_mapping_verified=True),
            dict(original, bank_settlement_verified=True),
            dict(original, verified_job_receipts_pence=9880),
            dict(original, events=({**event, "merchant_ref": "merchant-B"},)),
            dict(original, events=({**event, "gross_pence": 20000},)),
            dict(original, events=({**event, "fee_pence": 121},)),
            dict(original, events=({**event, "net_pence": 9881},)),
            dict(original, events=({**event, "bank_settlement_verified": True},)),
            dict(original, events=(event, event)),
            dict(original, events=[event]),
            dict(original, events=({**event, "event_id": "evt/invalid"},)),
            dict(original, events=(event,) * 5001),
        )
        for modified in alterations:
            with self.subTest(modification=str(modified)[:80]), self.assertRaises(ContractError):
                preview_processor_batches(modified)



if __name__ == "__main__":
    unittest.main()
