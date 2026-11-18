"""Customer work summaries must use the approved HQ price, not editable costing sale."""
import os
import tempfile
import unittest
from unittest.mock import patch

from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import create_test_job, set_draft_financials, change_work_status
from job_material_store import write_log, read_log


class CustomerBreakdownPricingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": temp.name,
            "HQ_SESSION_SECRET": "customer-summary-tests",
            "GOOGLE_CLIENT_ID": "test", "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()

    def sign_in(self, subject="cost-owner"):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, subject), google_sub=subject, csrf="csrf")

    @staticmethod
    def log(sale="1.00"):
        return {
            "sale": sale,
            "description": "Hyundai bonnet repaint",
            "stages": "Surface preparation\nBasecoat and clearcoat",
            "lines": [{"name": "PRIVATE MATERIAL", "cost": "95.00"}],
            "labour": "2", "actual_labour_rate": "30",
            "overhead": "20", "other_costs": "5",
        }

    def test_linked_work_requires_approval_and_prints_authoritative_price(self):
        work = create_test_job()
        set_draft_financials(work, 35000, 7600, expected_revision=0)
        write_log("cost-owner", work, self.log("1.00"), expected_revision=0)
        url = "/customer-breakdown/" + work
        self.assertEqual(self.client.get(url).status_code, 302)
        self.sign_in()
        self.assertEqual(self.client.get(url).status_code, 409)
        change_work_status(work, "ready_for_review", expected_revision=1)
        self.assertEqual(self.client.get(url).status_code, 409)
        change_work_status(work, "approved", expected_revision=2)
        before_revision = read_log("cost-owner", work)["revision"]
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        html = response.get_data(as_text=True)
        self.assertIn("£350.00", html)
        self.assertNotIn("£1.00", html)
        self.assertNotIn("£76.00", html)
        self.assertIn("Hyundai bonnet repaint", html)
        self.assertIn("Surface preparation", html)
        self.assertNotIn("PRIVATE MATERIAL", html)
        self.assertNotIn("£95.00", html)
        self.assertNotIn("Actual labour", html)
        self.assertNotIn("margin_percent", html)
        self.assertEqual(read_log("cost-owner", work)["revision"], before_revision)

        # A stale or independently changed costing sale must not change
        # what a customer sees after owner approval.
        altered = self.log("9999.00")
        write_log("cost-owner", work, altered, expected_revision=before_revision)
        html = self.client.get(url).get_data(as_text=True)
        self.assertIn("£350.00", html)
        self.assertNotIn("£9,999.00", html)
        change_work_status(work, "cancelled", expected_revision=3)
        self.assertEqual(self.client.get(url).status_code, 409)

    def test_legacy_manual_reference_retains_printing_behaviour(self):
        write_log("cost-owner", "J0057", self.log("270"), expected_revision=0)
        self.sign_in()
        response = self.client.get("/customer-breakdown/J0057")
        self.assertEqual(response.status_code, 200)
        self.assertIn("£270.00", response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.get("/customer-breakdown/invalid/part").status_code, 400)

    def test_unknown_internal_id_and_cross_owner_are_not_printed(self):
        work = create_test_job()
        set_draft_financials(work, 10000, 0, expected_revision=0)
        change_work_status(work, "ready_for_review", expected_revision=1)
        change_work_status(work, "approved", expected_revision=2)
        write_log("cost-owner", work, self.log("200"), expected_revision=0)
        self.sign_in("another-owner")
        self.assertEqual(self.client.get("/customer-breakdown/" + work).status_code, 404)
        self.sign_in()
        unknown = "a" * 32
        write_log("cost-owner", unknown, self.log("200"), expected_revision=0)
        self.assertEqual(self.client.get("/customer-breakdown/" + unknown).status_code, 404)

    def test_unapproved_price_is_never_labelled_agreed_and_markup_is_escaped(self):
        self.sign_in()
        work = create_test_job()
        write_log("cost-owner", work, self.log("350"), expected_revision=0)
        self.assertEqual(self.client.get("/customer-breakdown/" + work).status_code, 409)
        write_log("cost-owner", "J0001", {
            **self.log("20"),
            "description": "<script>malicious()</script>",
            "stages": "<img src=x onerror=alert(1)>",
        }, expected_revision=0)
        html = self.client.get("/customer-breakdown/J0001").get_data(as_text=True)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img", html)
        self.assertNotIn("<script>malicious()", html)


if __name__ == "__main__":
    unittest.main()
