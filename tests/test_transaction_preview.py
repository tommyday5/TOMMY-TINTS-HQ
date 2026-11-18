"""Tests for owner-only, non-importing CSV schema inspection, using fake data."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from hq_transaction_preview import PreviewError, preview_export


MONZO = "Transaction,Minor units,Currency,Booked,Private note\n"
MONZO_MAP = {
    "transaction_id": "Transaction", "amount_pence": "Minor units",
    "currency": "Currency", "booked_at": "Booked",
}
DNA = "Event,Payment,Kind,Gross,Fee,Net,Currency,Occurred,Batch,Customer name\n"
DNA_MAP = {
    "event_id": "Event", "payment_id": "Payment", "kind": "Kind",
    "gross_pence": "Gross", "fee_pence": "Fee", "net_pence": "Net",
    "currency": "Currency", "occurred_at": "Occurred", "batch_id": "Batch",
}


def monzo_row(tx="tx001", amount="12345", note="Private Customer"):
    return f"{tx},{amount},GBP,2026-11-18T10:00:00Z,{note}\n"


def dna_row(event="evt001", kind="payment", gross="10000", fee="200", net="9800"):
    return (f"{event},pmt001,{kind},{gross},{fee},{net},GBP,"
            "2026-11-18T10:00:00Z,batchA,Private Customer\n")


class CsvContractTests(unittest.TestCase):
    def test_headers_without_mapping_does_not_claim_ingestion(self):
        result = preview_export("monzo", MONZO + monzo_row())
        self.assertEqual(result["headers"][0], "Transaction")
        self.assertEqual(len(result["required_canonical_fields"]), 4)
        self.assertFalse(result["canonical_preview_valid"])
        self.assertFalse(result["source_mapping_verified"])
        self.assertFalse(result["provider_read_tested"])
        self.assertFalse(result["sync_tested"])
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["records_imported"], 0)
        self.assertNotIn("Private Customer", str(result))

    def test_exact_monzo_mapping_dedupes_without_receipts(self):
        result = preview_export("monzo", MONZO + monzo_row() + monzo_row(),
                                mapping=MONZO_MAP, scope_ref="business-one")
        self.assertTrue(result["canonical_preview_valid"])
        self.assertEqual((result["mapped_rows"], result["unique_events"],
                          result["exact_duplicate_rows"]), (2, 1, 1))
        self.assertEqual(result["records_imported"], 0)
        self.assertFalse(result["bank_settlement_verified"])
        self.assertFalse(result["job_payment_verified"])
        self.assertNotIn("tx001", str(result))
        self.assertNotIn("12345", str(result))

    def test_dna_batch_is_not_a_bank_settlement(self):
        result = preview_export("dna",
                                DNA + dna_row() +
                                dna_row(event="evtRefund", kind="refund",
                                        gross="-2000", fee="0", net="-2000"),
                                mapping=DNA_MAP, scope_ref="merchant-one")
        self.assertEqual(result["unique_events"], 2)
        self.assertFalse(result["bank_settlement_verified"])
        self.assertFalse(result["job_payment_verified"])
        self.assertFalse(result["source_mapping_verified"])

    def test_manual_classification_preview_counts_only_and_never_verifies_cash(self):
        text = MONZO + monzo_row("tx001", "10000") + monzo_row("tx002", "-3000")
        result = preview_export("monzo", text, mapping=MONZO_MAP,
                                scope_ref="business-one",
                                classifications={
                                    "tx001": "customer_receipt_candidate",
                                    "tx002": "transfer",
                                })
        review = result["classification_review"]
        self.assertEqual(review["reviewed_proposals"], 2)
        self.assertEqual(review["pending_review"], 0)
        self.assertEqual(review["customer_receipt_candidates"], 1)
        self.assertFalse(review["bank_receipt_verified"])
        self.assertFalse(review["trading_revenue_verified"])
        self.assertFalse(review["job_payment_verified"])
        self.assertEqual(review["records_imported"], 0)
        self.assertEqual(review["classification_changes_saved"], 0)
        for sensitive in ("tx001", "tx002", "10000", "-3000", "business-one"):
            self.assertNotIn(sensitive, str(result))

    def test_manual_review_requires_exact_known_ids_and_monzo_mapping(self):
        text = MONZO + monzo_row()
        for review in ({"unknown-id": "transfer"}, {"tx001": "verified_revenue"},
                       {"tx001": True}, ["transfer"], "revenue"):
            with self.subTest(review=review), self.assertRaises(PreviewError):
                preview_export("monzo", text, mapping=MONZO_MAP,
                               scope_ref="business-one", classifications=review)
        with self.assertRaises(PreviewError):
            preview_export("monzo", text, classifications={"tx001": "transfer"})
        with self.assertRaises(PreviewError):
            preview_export("dna", DNA + dna_row(), mapping=DNA_MAP,
                           scope_ref="merchant-one",
                           classifications={"evt001": "customer_receipt_candidate"})

    def test_negative_bank_entry_cannot_be_proposed_as_customer_receipt(self):
        text = MONZO + monzo_row("debit001", "-10000")
        with self.assertRaisesRegex(PreviewError, "positive bank credit"):
            preview_export("monzo", text, mapping=MONZO_MAP,
                           scope_ref="business-one",
                           classifications={"debit001": "customer_receipt_candidate"})

    def test_unclassified_and_unresolved_are_not_falsely_completed(self):
        text = MONZO + monzo_row("tx001") + monzo_row("tx002", "-1")
        result = preview_export("monzo", text, mapping=MONZO_MAP,
                                scope_ref="business-one",
                                classifications={"tx001": "unresolved"})
        self.assertEqual(result["classification_review"]["pending_review"], 2)
        self.assertEqual(result["classification_review"]["reviewed_proposals"], 0)

    def test_conflicting_replay_aborts_no_partial_result(self):
        with self.assertRaisesRegex(PreviewError, "Conflicting"):
            preview_export("monzo", MONZO + monzo_row() + monzo_row(amount="22222"),
                           mapping=MONZO_MAP, scope_ref="business-one")

    def test_mapping_must_be_explicit_nonambiguous_and_exact(self):
        bad_maps = [
            {"transaction_id": "Transaction"},
            {**MONZO_MAP, "amount_pence": "Transaction"},
            {**MONZO_MAP, "booked_at": "unknown"},
            {**MONZO_MAP, "unknown": "Private note"},
            {**MONZO_MAP, "amount_pence": 2},
        ]
        for mapping in bad_maps:
            with self.subTest(mapping=mapping), self.assertRaises(PreviewError):
                preview_export("monzo", MONZO + monzo_row(),
                               mapping=mapping, scope_ref="business-one")

    def test_no_pounds_float_silent_unit_conversion(self):
        for amount in ("12.34", "1,234", "1e3", "True", " 123", "01", "£123"):
            with self.subTest(amount=amount), self.assertRaises(PreviewError):
                # Quoted CSV is required for a comma-bearing field.
                text = MONZO + f'tx001,"{amount}",GBP,2026-11-18T10:00:00Z,Private\n'
                preview_export("monzo", text, mapping=MONZO_MAP,
                               scope_ref="business-one")
        with self.assertRaises(PreviewError):
            preview_export("dna", DNA + dna_row(net="9900"),
                           mapping=DNA_MAP, scope_ref="merchant-one")

    def test_malformed_csv_different_row_width_headers_and_bad_date(self):
        inputs = (
            MONZO + "tx001,12345,GBP\n",
            "A,A,C\n1,2,3\n",
            "X" * 81 + ",B\n1,2\n",
            MONZO + 'tx001,"unterminated\n',
        )
        for text in inputs:
            with self.subTest(text=text[:35]), self.assertRaises(PreviewError):
                preview_export("monzo", text, mapping=MONZO_MAP,
                               scope_ref="business-one")
        with self.assertRaises(PreviewError):
            preview_export("monzo", MONZO + "tx001,12345,GBP,2026-11-18T10:00:00,Private\n",
                           mapping=MONZO_MAP, scope_ref="business-one")

    def test_row_and_size_bounds(self):
        with self.assertRaises(PreviewError):
            preview_export("monzo", MONZO + monzo_row() * 501,
                           mapping=MONZO_MAP, scope_ref="business-one")
        with self.assertRaises(PreviewError):
            preview_export("monzo", "X" * 120001)
        with self.assertRaises(PreviewError):
            preview_export("monzo", MONZO + "a\x00b\n")


class OwnerPreviewHttpTests(unittest.TestCase):
    def setUp(self):
        from app import app
        from hq_auth import issue_session
        from hq_auth_store import auth_db
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.tmp.name, "HQ_SESSION_SECRET": "test-secret",
            "GOOGLE_CLIENT_ID": "test", "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.app = app
        self.client = app.test_client()
        self.issue_session, self.auth_db = issue_session, auth_db

    def owner(self):
        with self.client.session_transaction() as sess, self.auth_db() as db:
            sess.update(hq_sid=self.issue_session(db, "owner"), csrf="valid-csrf")

    def payload(self):
        return {
            "source": "monzo", "csv_text": MONZO + monzo_row(),
            "mapping": MONZO_MAP, "scope_ref": "business-one",
        }

    def test_owner_only_no_session_or_csrf_is_denied(self):
        self.assertEqual(self.client.get("/hq/transaction-import-preview").status_code, 302)
        self.assertEqual(self.client.post("/api/hq/transaction-import-preview",
                                          json=self.payload()).status_code, 302)
        self.owner()
        self.assertEqual(self.client.post("/api/hq/transaction-import-preview",
                                          json=self.payload()).status_code, 403)
        self.assertEqual(self.client.post("/hq/transaction-import-preview",
                                          data={"csrf": "wrong"}).status_code, 403)
        page = self.client.get("/hq/transaction-import-preview")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Owner-only", page.data)
        self.assertEqual(page.headers["Cache-Control"], "no-store")

    def test_success_does_not_persist_csv_pii_or_update_finances(self):
        self.owner()
        before = set(os.listdir(self.tmp.name))
        result = self.client.post("/api/hq/transaction-import-preview",
                                  json=self.payload(), headers={"X-HQ-CSRF": "valid-csrf"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.assertTrue(result.json["canonical_preview_valid"])
        self.assertFalse(result.json["reconciled"])
        self.assertEqual(result.json["records_imported"], 0)
        for sensitive in ("Private Customer", "12345", "business-one", "tx001"):
            self.assertNotIn(sensitive, result.get_data(as_text=True))
        self.assertEqual(set(os.listdir(self.tmp.name)), before)
        self.assertEqual(self.client.get("/hq/integrations").status_code, 200)

    def test_form_supports_schema_inspection_and_no_sensitive_echo(self):
        self.owner()
        result = self.client.post("/hq/transaction-import-preview",
                                  data={"csrf": "valid-csrf", "source": "monzo",
                                        "csv_text": MONZO + monzo_row()})
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"Required canonical fields", result.data)
        self.assertNotIn(b"Private Customer", result.data)
        self.assertNotIn(b"12345", result.data)
        self.assertNotIn(b"tx001", result.data)

    def test_owner_manual_classification_preview_never_echoes_ids_or_persists(self):
        self.owner()
        before = set(os.listdir(self.tmp.name))
        payload = self.payload()
        payload["classifications"] = {"tx001": "customer_receipt_candidate"}
        response = self.client.post("/api/hq/transaction-import-preview",
                                    json=payload, headers={"X-HQ-CSRF": "valid-csrf"})
        self.assertEqual(response.status_code, 200)
        review = response.json["classification_review"]
        self.assertEqual(review["customer_receipt_candidates"], 1)
        self.assertFalse(review["trading_revenue_verified"])
        self.assertEqual(review["records_imported"], 0)
        for secret in ("tx001", "business-one", "Private Customer", "12345"):
            self.assertNotIn(secret, response.get_data(as_text=True))
        self.assertEqual(set(os.listdir(self.tmp.name)), before)
        payload["classifications"] = '{"tx001": "paid"}'
        response = self.client.post("/api/hq/transaction-import-preview",
                                    json=payload, headers={"X-HQ-CSRF": "valid-csrf"})
        self.assertEqual(response.status_code, 400)

    def test_invalid_shape_mapping_and_oversize_fail_closed(self):
        self.owner()
        payload = self.payload()
        payload["mapping"] = json.dumps(MONZO_MAP)
        response = self.client.post("/api/hq/transaction-import-preview",
                                    json=payload, headers={"X-HQ-CSRF": "valid-csrf"})
        self.assertEqual(response.status_code, 200)
        payload["source"] = "unknown"
        response = self.client.post("/api/hq/transaction-import-preview",
                                    json=payload, headers={"X-HQ-CSRF": "valid-csrf"})
        self.assertEqual(response.status_code, 400)
        payload = self.payload()
        payload["csv_text"] = "X" * 130000
        response = self.client.post("/api/hq/transaction-import-preview",
                                    json=payload, headers={"X-HQ-CSRF": "valid-csrf"})
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
