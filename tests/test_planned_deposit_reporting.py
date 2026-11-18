"""Approval pipeline figures must never be mistaken for received cash."""
import os
import tempfile
import unittest
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import (
    change_work_status, create_test_job, read_work_record,
    set_draft_financials, work_summary,
)


class PlannedDepositReportingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {
            "HQ_DATA_DIR": temporary.name,
            "HQ_SESSION_SECRET": "planned-deposit-test-secret",
            "GOOGLE_CLIENT_ID": "test", "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.client = app.test_client()

    def sign_in(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "test-owner"), csrf="csrf")

    def priced_job(self, price, deposit, status):
        record = create_test_job()
        set_draft_financials(record, price, deposit, expected_revision=0)
        if status != "draft":
            change_work_status(record, "ready_for_review", expected_revision=1)
            change_work_status(record, "approved", expected_revision=2)
            if status in ("in_progress", "completed"):
                change_work_status(record, "in_progress", expected_revision=3)
            if status == "completed":
                change_work_status(record, "completed", expected_revision=4)
            if status == "cancelled":
                change_work_status(record, "cancelled", expected_revision=3)
        return record

    def test_approved_pipeline_excludes_drafts_cancellations_and_corrupt_legacy(self):
        draft = self.priced_job(20000, 5000, "draft")
        approved = self.priced_job(35000, 7600, "approved")
        self.priced_job(10000, 1000, "in_progress")
        self.priced_job(50000, 10000, "completed")
        self.priced_job(12000, 3000, "cancelled")
        missing_price = create_test_job()
        invalid_deposit = self.priced_job(9000, 2000, "approved")
        # Old or externally corrupted data must not silently become revenue.
        with connect() as db:
            db.execute("UPDATE hq_work_records SET status='approved' WHERE id=?",
                       (missing_price,))
            db.execute("UPDATE hq_work_records SET deposit_pence=10000 WHERE id=?",
                       (invalid_deposit,))

        summary = work_summary()
        self.assertEqual(summary["record_count"], 7)
        self.assertEqual(summary["priced_draft_value_pence"], 20000)
        self.assertEqual(summary["approved_priced_record_count"], 3)
        self.assertEqual(summary["approved_amounts_incomplete_count"], 2)
        self.assertEqual(summary["approved_price_total_pence"], 95000)
        self.assertEqual(summary["planned_deposit_total_pence"], 18600)
        self.assertEqual(summary["conditional_remainder_pence"], 76400)
        self.assertEqual(summary["payment_reconciliation_status"], "not_connected")
        self.assertFalse(summary["payment_verified"])
        self.assertIsNone(summary["verified_cash_received_pence"])
        self.assertIsNone(summary["verified_cash_outstanding_pence"])
        self.assertIn("unverified", summary["outstanding_recorded_basis"])
        # Existing stored prices/deposits and historical compatibility remain intact.
        self.assertEqual(read_work_record(draft)["deposit_pence"], 5000)
        self.assertEqual(read_work_record(approved)["deposit_pence"], 7600)
        self.assertGreater(summary["outstanding_recorded_pence"],
                           summary["conditional_remainder_pence"])

    def test_empty_records_mean_no_plans_but_unknown_verified_receipts(self):
        summary = work_summary()
        self.assertEqual(summary["approved_price_total_pence"], 0)
        self.assertEqual(summary["planned_deposit_total_pence"], 0)
        self.assertEqual(summary["approved_priced_record_count"], 0)
        self.assertIsNone(summary["verified_cash_received_pence"])
        self.assertIsNone(summary["verified_cash_outstanding_pence"])

    def test_owner_only_endpoint_and_job_cards_use_explicit_unverified_labels(self):
        self.priced_job(35000, 7600, "approved")
        url = "/api/hq/work-summary"
        self.assertEqual(self.client.get(url).status_code, 302)
        self.sign_in()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["approved_price_total_pence"], 35000)
        self.assertEqual(response.json["planned_deposit_total_pence"], 7600)
        self.assertIsNone(response.json["verified_cash_received_pence"])
        self.assertNotIn("customer", response.get_data(as_text=True).lower())

        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="jobCardPaymentSummary"', html)
        self.assertIn('await assistantTaskApi("/api/hq/work-summary")', html)
        self.assertIn("Approved job prices (not collected)", html)
        self.assertIn("Planned deposits (not confirmed received)", html)
        self.assertIn("Verified receipts and actual outstanding amounts: unavailable", html)
        self.assertIn('deposit.placeholder="Planned deposit £ (NOT received)"', html)
        self.assertIn("Price less planned deposit", html)
        self.assertIn("Conditional remainder after planned deposit", html)
        self.assertNotIn('deposit.placeholder="Deposit received £"', html)
        self.assertNotIn("Draft saved. Outstanding £", html)
        self.assertNotIn("Balance after recorded deposit", html)


if __name__ == "__main__":
    unittest.main()
