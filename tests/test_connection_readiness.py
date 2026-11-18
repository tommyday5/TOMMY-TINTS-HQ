"""Conservative readiness checks: no invented consent, receipts or live data."""
import os
import tempfile
import unittest
from unittest.mock import patch

from hq_connection_readiness import build_readiness, PROFILES
from hq_integration_health import PersistedSyncError, integration_status, record_check


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
        self.assertFalse(rows["square_payments"]["sync_tested"])
        self.assertIsNone(rows["square_payments"]["sync_last_success"])
        self.assertIsNone(rows["square_payments"]["authorised"])
        self.assertFalse(rows["square_payments"]["reconciled"])

    def test_last_read_failure_preserves_historical_success_not_current_health(self):
        record_check("square_payments")
        record_check("square_payments_sync")
        record_check("square_payments", "Square payment read failed")
        row = self.build({"square_payments": {"configured": True, "consent": None}})["square_payments"]
        self.assertTrue(row["read_tested"])
        self.assertTrue(row["sync_tested"])
        self.assertEqual(row["blocker"], "Square payment read failed")
        self.assertFalse(row["reconciled"])

    def test_explicit_post_commit_evidence_is_required_for_each_sync(self):
        # A sync marker without a successful upstream read is insufficient.
        record_check("gmail_sync")
        self.assertFalse(self.build()["gmail"]["sync_tested"])
        for provider in ("gmail", "square", "square_payments"):
            record_check(provider)
            row = self.build({provider: {"configured": True, "consent": None}})[provider]
            if provider != "gmail":
                self.assertFalse(row["sync_tested"])
                self.assertIsNone(row["sync_last_success"])
            else:
                self.assertTrue(row["sync_tested"])
                self.assertTrue(row["sync_last_success"])
            record_check(provider + "_sync")
            row = self.build({provider: {"configured": True, "consent": None}})[provider]
            self.assertTrue(row["read_tested"])
            self.assertTrue(row["sync_tested"])
            self.assertTrue(row["sync_last_success"])
            self.assertFalse(row["reconciled"])

    def test_source_reads_not_committed_syncs(self):
        for provider in ("sheets", "finance", "calendar", "square_payments"):
            record_check(provider)
        rows = self.build({key: {"configured": True, "consent": True}
                           for key in ("sheets", "finance", "calendar")})
        for key in ("sheets", "finance", "calendar", "square_payments"):
            self.assertTrue(rows[key]["read_tested"])
            self.assertFalse(rows[key]["sync_tested"])

    def test_failed_persisted_sync_is_visible_despite_historical_success(self):
        record_check("square_payments")
        record_check("square_payments_sync")
        record_check("square_payments_sync", "Fixture persistence failure")
        row = self.build({
            "square_payments": {"configured": True, "consent": None},
        })["square_payments"]
        self.assertTrue(row["read_tested"])
        self.assertTrue(row["sync_tested"])  # historical successful write
        self.assertTrue(row["sync_needs_attention"])
        self.assertEqual(row["sync_last_error"], "Fixture persistence failure")
        self.assertTrue(row["sync_last_error_at"])
        self.assertEqual(row["blocker"], "Fixture persistence failure")
        self.assertIn("refresh", row["next_safe_action"].lower())
        self.assertFalse(row["reconciled"])

    def test_recovered_sync_clears_attention_without_erasing_success(self):
        record_check("gmail")
        record_check("gmail_sync")
        record_check("gmail_sync", "Fixture database unavailable")
        self.assertTrue(self.build({"gmail": {"configured": True, "consent": True}})
                        ["gmail"]["sync_needs_attention"])
        record_check("gmail_sync")
        row = self.build({"gmail": {"configured": True, "consent": True}})["gmail"]
        self.assertTrue(row["sync_tested"])
        self.assertFalse(row["sync_needs_attention"])
        self.assertIsNone(row["sync_last_error"])
        self.assertNotIn("Fixture database unavailable", row["blocker"])
        self.assertFalse(row["reconciled"])

    def test_read_only_workflows_do_not_require_nonexistent_sync(self):
        for key in ("sheets", "finance", "calendar"):
            record_check(key)
        rows = self.build({key: {"configured": True, "consent": True}
                           for key in ("sheets", "finance", "calendar")})
        for profile in PROFILES:
            if profile["key"] not in ("sheets", "finance", "calendar"):
                continue
            row = rows[profile["key"]]
            self.assertTrue(row["read_tested"])
            self.assertFalse(row["sync_applicable"])
            self.assertFalse(row["sync_tested"])
            self.assertEqual(row["stages"]["sync_tested"], "Not applicable (read-only)")
            self.assertEqual(row["blocker"], profile["reconcile"])
            self.assertEqual(row["next_safe_action"], profile["reconcile"])

    def test_missing_configuration_requests_setup_before_provider_reads(self):
        row = self.build()["square_payments"]
        profile = next(p for p in PROFILES if p["key"] == "square_payments")
        self.assertEqual(row["next_safe_action"], profile["setup"])
        self.assertFalse(row["configured"])

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

    def test_dashboard_uses_local_encrypted_grants_without_refresh_or_remote_calls(self):
        from hq_google_connections import save_connection
        with self.client.session_transaction() as session:
            session["google_sub"] = "test-owner"
        for provider in ("sheets", "calendar", "gmail"):
            self.assertTrue(save_connection(provider, "test-owner", {
                "access_token": "expired-private-token",
                "refresh_token": "owner-private-refresh",
                "expires_in": 0,
            }))
        with patch("hq_google_connections.requests.post",
                   side_effect=AssertionError("Status page tried to refresh OAuth")) as remote, \
             patch("app._google_connection",
                   side_effect=AssertionError("Status page tried provider token lookup")), \
             patch("app._gmail_token",
                   side_effect=AssertionError("Status page tried Gmail token lookup")):
            for path in ("/api/hq/integrations", "/hq/integrations"):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
            rows = {r["key"]: r for r in self.client.get(
                "/api/hq/integrations").json["readiness"]}
            remote.assert_not_called()
        for provider in ("sheets", "finance", "calendar", "gmail"):
            row = rows[provider]
            self.assertTrue(row["authorised"])  # saved grant only; not live permission
            self.assertFalse(row["read_tested"])
            self.assertFalse(row["reconciled"])

    def test_stored_consent_is_owner_scoped_and_corruption_fails_closed(self):
        from hq_google_connections import save_connection, stored_grant_present
        from hq_auth_store import auth_db
        self.assertFalse(stored_grant_present("gmail", "test-owner"))
        self.assertTrue(save_connection("gmail", "test-owner", {
            "access_token": "test", "refresh_token": "private-refresh", "expires_in": 0,
        }))
        self.assertFalse(stored_grant_present("gmail", "another-owner"))
        self.assertTrue(stored_grant_present("gmail", "test-owner"))
        with auth_db() as db:
            db.execute("UPDATE google_connections SET token=? WHERE provider='gmail'",
                       (b"corrupted-ciphertext",))
        self.assertFalse(stored_grant_present("gmail", "test-owner"))
        with self.client.session_transaction() as session:
            session["google_sub"] = "test-owner"
        row = next(r for r in self.client.get("/api/hq/integrations").json["readiness"]
                   if r["key"] == "gmail")
        self.assertFalse(row["authorised"])
        self.assertFalse(row["read_tested"])

    def test_gmail_storage_failure_is_recorded_as_sync_not_provider_read(self):
        with self.client.session_transaction() as session:
            session["csrf"] = "fixture-csrf"
        record_check("gmail")
        record_check("gmail_sync")
        with patch("app._gmail_token", return_value="fixture-token"), patch(
                "hq_gmail_enquiries.import_page",
                side_effect=PersistedSyncError("Fixture storage failed")):
            response = self.client.post("/hq/gmail-enquiries",
                                        data={"csrf": "fixture-csrf"})
            row = next(x for x in self.client.get("/api/hq/integrations")
                       .json["readiness"] if x["key"] == "gmail")
        self.assertEqual(response.status_code, 503)
        self.assertTrue(row["read_tested"])
        self.assertTrue(row["sync_tested"])
        self.assertTrue(row["sync_needs_attention"])
        self.assertEqual(row["blocker"], "Gmail enquiry storage commit failed")
        self.assertFalse(row["reconciled"])

    def test_square_booking_storage_failure_is_recorded_as_sync(self):
        with self.client.session_transaction() as session:
            session["csrf"] = "fixture-csrf"
        record_check("square")
        record_check("square_sync")
        with patch.dict(os.environ, {
                "HQ_SQUARE_READ_TOKEN": "fixture-read-only",
                "HQ_SQUARE_LOCATION_ID": "fixture-location"}), patch(
                    "hq_square_bookings.refresh_bookings",
                    side_effect=PersistedSyncError("Fixture disk full")):
            response = self.client.post("/hq/square-bookings",
                                        data={"csrf": "fixture-csrf"})
            row = next(x for x in self.client.get("/api/hq/integrations")
                       .json["readiness"] if x["key"] == "square")
        self.assertEqual(response.status_code, 503)
        self.assertTrue(row["sync_needs_attention"])
        self.assertTrue(row["sync_tested"])
        self.assertEqual(row["blocker"], "Square booking snapshot commit failed")
        self.assertFalse(row["reconciled"])

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
