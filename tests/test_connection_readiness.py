"""Conservative readiness checks: no invented consent, receipts or live data."""
import os
import tempfile
import unittest
from unittest.mock import patch

from hq_connection_readiness import build_readiness, PROFILES
from hq_integration_health import integration_status, record_check


class ReadinessUnitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"HQ_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def build(self, signals=None):
        return {r["key"]: r for r in build_readiness(integration_status([
            ("sheets", "Google Sheets", False), ("finance", "Sheets finance", False),
            ("calendar", "Google Calendar", False),
            ("gmail", "Gmail", False), ("square", "Square", False),
            ("square_payments", "Square payments", False),
        ]), signals or {})}

    def test_all_workstreams_accounted_for_without_external_calls(self):
        rows = self.build()
        self.assertEqual(set(rows), {profile["key"] for profile in PROFILES})
        for row in rows.values():
            self.assertFalse(row["reconciled"])
            self.assertIsNone(row["last_success"])
            self.assertTrue(row["blocker"])
            self.assertTrue(row["next_safe_action"])
            self.assertTrue(row["owner_only"])

    def test_env_presence_never_establishes_square_authorisation_or_a_receipt(self):
        rows = self.build({
            "square_payments": {"configured": True, "consent": None},
            "square": {"configured": True, "consent": None},
        })
        for key in ("square_payments", "square"):
            row = rows[key]
            self.assertTrue(row["configured"])
            self.assertIsNone(row["authorised"])
            self.assertFalse(row["read_tested"])
            self.assertFalse(row["sync_tested"])
            self.assertFalse(row["reconciled"])

    def test_separate_finance_read_evidence_and_persisted_snapshot_stages(self):
        record_check("sheets")
        record_check("square_payments")
        rows = self.build({
            "sheets": {"configured": True, "consent": True},
            "finance": {"configured": True, "consent": True},
            "square_payments": {"configured": True, "consent": None},
        })
        self.assertTrue(rows["sheets"]["read_tested"])
        self.assertFalse(rows["finance"]["read_tested"])
        self.assertFalse(rows["sheets"]["sync_tested"])
        self.assertTrue(rows["square_payments"]["read_tested"])
        self.assertTrue(rows["square_payments"]["sync_tested"])
        self.assertIsNone(rows["square_payments"]["authorised"])
        self.assertFalse(rows["square_payments"]["reconciled"])

    def test_last_read_failure_preserves_historical_success_not_current_health(self):
        record_check("square_payments")
        record_check("square_payments", "Square payment read failed")
        row = self.build({"square_payments": {"configured": True, "consent": None}})["square_payments"]
        self.assertTrue(row["read_tested"])
        self.assertTrue(row["sync_tested"])
        self.assertEqual(row["blocker"], "Square payment read failed")
        self.assertFalse(row["reconciled"])

    def test_failed_history_read_is_not_misrepresented_as_no_events(self):
        with patch("hq_integration_health._db_path", side_effect=OSError("unavailable")):
            health = integration_status([("sheets", "Google Sheets", True)])
        row = build_readiness(health, {"sheets": {"configured": True, "consent": True}})[0]
        self.assertIsNone(row["read_tested"])
        self.assertIsNone(row["sync_tested"])


class ReadinessHttpTests(unittest.TestCase):
    def setUp(self):
        from app import app
        from hq_auth import issue_session
        from hq_auth_store import auth_db
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name, "HQ_SESSION_SECRET": "unit-test-secret",
            "GOOGLE_CLIENT_ID": "test", "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()
        self.assertEqual(self.client.get("/api/hq/integrations").status_code, 302)
        self.assertEqual(self.client.get("/hq/integrations").status_code, 302)
        with self.client.session_transaction() as session:
            with auth_db() as db:
                session["hq_sid"] = issue_session(db, "test-owner")

    def test_owner_readiness_visible_and_no_financial_numbers_leak(self):
        response = self.client.get("/api/hq/integrations")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        rows = {x["key"]: x for x in response.json["readiness"]}
        self.assertIn("monzo", rows)
        self.assertIn("dna", rows)
        self.assertFalse(rows["monzo"]["implemented"])
        self.assertFalse(rows["square_payments"]["reconciled"])
        self.assertIsNone(rows["square_payments"]["last_success"])
        page = self.client.get("/hq/integrations")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Connection &amp; reconciliation readiness", page.data)
        self.assertIn(b"Google Sheets finance", page.data)

    def test_finance_read_is_independently_recorded_from_pricing(self):
        with patch.dict(os.environ, {"HQ_GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}):
            with patch("hq_sheets_live.read_finance", return_value={"source": "fixture"}):
                response = self.client.get("/api/hq/finance")
                self.assertEqual(response.status_code, 200)
            rows = {x["key"]: x for x in self.client.get("/api/hq/integrations").json["readiness"]}
            self.assertTrue(rows["finance"]["read_tested"])
            self.assertFalse(rows["sheets"]["read_tested"])


if __name__ == "__main__":
    unittest.main()
