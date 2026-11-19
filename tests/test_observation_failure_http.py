"""Owner-only refreshes must report observation failures, never false sync success.

Every provider adapter in these tests is synthetic. No real provider calls,
customer records, payment settlement or finance writes are performed.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db


class ObservationFailureHttpTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name,
            "HQ_SESSION_SECRET": "fixture-observation-failure-secret",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
            "GOOGLE_CLIENT_ID": "fixture-id",
            "GOOGLE_CLIENT_SECRET": "fixture-secret",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            with auth_db() as db:
                sess["hq_sid"] = issue_session(db, "fixture-owner")
            sess["csrf"] = "fixture-csrf"

    def test_gmail_import_committed_but_observation_failed_returns_503(self):
        with patch("app._gmail_token", return_value="fixture-token"), \
             patch("app.gmail.import_page", return_value=None) as importer, \
             patch("app.record_committed_sync", return_value=False) as observer, \
             patch("app.gmail.queue", side_effect=AssertionError("do not report success")):
            response = self.client.post(
                "/hq/gmail-enquiries", data={"csrf": "fixture-csrf"})
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"stored, but integration read/sync evidence", response.data)
        self.assertIn(b"do not treat this import as a verified sync", response.data)
        importer.assert_called_once_with("fixture-token", None)
        observer.assert_called_once_with("gmail")

    def test_square_bookings_committed_but_observation_failed_returns_503(self):
        with patch("app.square.configured", return_value=True), \
             patch("app.square.refresh_bookings", return_value=2) as refresh, \
             patch("app.record_committed_sync", return_value=False) as observer, \
             patch("app.square.upcoming_bookings",
                   side_effect=AssertionError("do not report success")):
            response = self.client.post(
                "/hq/square-bookings", data={"csrf": "fixture-csrf"})
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"stored, but integration read/sync evidence", response.data)
        self.assertIn(b"do not treat this refresh as a verified sync", response.data)
        refresh.assert_called_once_with()
        observer.assert_called_once_with("square")

    def test_square_payment_snapshot_observation_error_is_visible(self):
        fixture_snapshot = {
            "configured": True, "snapshot_available": False,
            "stale": True, "payments": [],
        }
        with patch("hq_square_payments.configured", return_value=True), \
             patch("hq_square_payments.refresh_payments") as refresh, \
             patch("hq_square_payments.record_committed_sync",
                   return_value=False) as observer, \
             patch("hq_square_payments.read_snapshot",
                   return_value=fixture_snapshot):
            response = self.client.post(
                "/hq/square-payment-evidence", data={"csrf": "fixture-csrf"})
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"snapshot was stored, but integration", response.data)
        self.assertIn(b"sync remains unverified", response.data)
        refresh.assert_called_once_with()
        observer.assert_called_once_with("square_payments")


    def test_read_only_provider_success_without_recorded_evidence_is_503(self):
        # Synthetic provider results. If the readiness write fails after the
        # upstream read, never return a normal success with no audit trace.
        import time
        cases = (
            ("calendar", "/api/hq/maintenance/calendar-live-check",
             "app.live_conflicts", "post"),
            ("maintenance_sheets", "/api/hq/maintenance",
             "app.maintenance_data", "get"),
            ("finance", "/api/hq/finance",
             "hq_sheets_live.read_finance", "get"),
            ("pricing_sheets", "/api/hq/pricing",
             "app.read_workbook_with_access_token", "get"),
        )
        for key, endpoint, adapter, method in cases:
            with self.subTest(provider=key), \
                 patch("app._google_connection",
                       return_value=("fixture-token", time.time() + 3600)), \
                 patch(adapter, return_value={"private_fixture": "synthetic-only"}) as provider, \
                 patch("app.record_check", return_value=False) as observer:
                if method == "post":
                    response = self.client.post(endpoint, json={
                        "start": "2026-11-20T10:00:00Z",
                        "end": "2026-11-20T11:00:00Z",
                    })
                else:
                    response = self.client.get(endpoint)
                self.assertEqual(response.status_code, 503)
                self.assertIn(b"could not record", response.data)
                self.assertIn(b"do not treat this as a verified read", response.data)
                self.assertNotIn(b"private_fixture", response.data)
                self.assertEqual(response.headers.get("Cache-Control"), "no-store")
                provider.assert_called_once()
                observer.assert_called_once_with(key)

    def test_read_observation_returns_commit_result(self):
        import sqlite3
        from hq_integration_health import record_check
        self.assertTrue(record_check("finance"))
        with patch("hq_integration_health.sqlite3.connect",
                   side_effect=sqlite3.OperationalError("synthetic fixture storage failure")):
            self.assertFalse(record_check("finance"))


if __name__ == "__main__":
    unittest.main()
