"""Non-provider workflow readiness must never claim a live transaction or booking."""
import os
import tempfile
import unittest
from unittest.mock import patch

from hq_workflow_readiness import WORKFLOWS, build_workflow_readiness


class WorkflowReadinessTests(unittest.TestCase):
    def test_all_workstreams_covered_with_no_live_or_payment_claims(self):
        rows = build_workflow_readiness(persistent_storage_configured=None)
        self.assertEqual({r["key"] for r in rows}, {p["key"] for p in WORKFLOWS})
        self.assertEqual(len(rows), len(set(r["key"] for r in rows)))
        self.assertGreaterEqual(len(rows), 9)
        for row in rows:
            self.assertTrue(row["owner_only"])
            self.assertIsNone(row["last_success"])
            self.assertIsNone(row["read_tested"])
            self.assertIsNone(row["sync_tested"])
            self.assertIsNone(row["authorised"])
            self.assertFalse(row["reconciled"])
            self.assertTrue(row["blocker"])
            self.assertTrue(row["next_safe_action"])
            self.assertNotIn("verified receipt", str(row).lower())
        self.assertFalse(next(r for r in rows if r["key"] == "basket_checkout")["implemented"])
        self.assertFalse(next(r for r in rows if r["key"] == "customer_accounts")["implemented"])

    def test_storage_path_presence_is_not_database_or_backup_verification(self):
        missing = {r["key"]: r for r in build_workflow_readiness(
            persistent_storage_configured=False)}
        declared = {r["key"]: r for r in build_workflow_readiness(
            persistent_storage_configured=True)}
        for key in ("maintenance_packages", "public_requests", "basket_checkout",
                    "customer_accounts", "job_approval_costing", "device_security"):
            self.assertFalse(missing[key]["configured"])
            self.assertIn("HQ_DATA_DIR", missing[key]["blocker"])
            self.assertTrue(declared[key]["configured"])
            self.assertEqual(declared[key]["stages"]["configured"], "Path declared, not verified")
            self.assertIsNone(declared[key]["last_success"])
        self.assertIsNone(declared["finance_centre"]["configured"])

    def test_unknown_signal_validation(self):
        with self.assertRaises(ValueError):
            build_workflow_readiness(persistent_storage_configured="yes")


class WorkflowReadinessHTTPTests(unittest.TestCase):
    def setUp(self):
        from app import app
        from hq_auth import issue_session
        from hq_auth_store import auth_db
        tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.addCleanup(tmp.cleanup)
        environment = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name, "HQ_SESSION_SECRET": "unit-test-secret",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
            "GOOGLE_CLIENT_ID": "fixture-google-id",
            "GOOGLE_CLIENT_SECRET": "fixture-google-secret",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.client = app.test_client()
        self.assertEqual(self.client.get("/api/hq/integrations").status_code, 302)
        self.assertEqual(self.client.get("/hq/integrations").status_code, 302)
        with self.client.session_transaction() as flask_session:
            with auth_db() as db:
                flask_session["hq_sid"] = issue_session(db, "test-owner")

    def test_owner_only_api_and_page_show_conservative_workflow_readiness(self):
        api = self.client.get("/api/hq/integrations")
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.headers["Cache-Control"], "no-store")
        workflows = {x["key"]: x for x in api.json["workflow_readiness"]}
        self.assertIn("maintenance_packages", workflows)
        self.assertIn("basket_checkout", workflows)
        self.assertIn("finance_centre", workflows)
        self.assertIsNone(workflows["finance_centre"]["last_success"])
        self.assertFalse(workflows["finance_centre"]["reconciled"])
        self.assertFalse(workflows["basket_checkout"]["implemented"])
        self.assertTrue(workflows["public_requests"]["configured"])
        self.assertFalse(workflows["public_requests"]["reconciled"])
        self.assertNotIn("customer_name", str(api.json))
        page = self.client.get("/hq/integrations")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(b"Internal workflow readiness", page.data)
        self.assertIn(b"Maintenance customers and visit packages", page.data)
        self.assertIn(b"Product basket and checkout", page.data)
        self.assertIn(b"End-to-end incomplete", page.data)


if __name__ == "__main__":
    unittest.main()
