import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hq_auth import issue_session
from hq_auth_store import auth_db
from app import app


class IntegrationHealthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": directory.name, "HQ_SESSION_SECRET": "test",
            "GOOGLE_CLIENT_ID": "test", "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()

    def sign_in(self):
        with self.client.session_transaction() as session:
            with auth_db() as db:
                session["hq_sid"] = issue_session(db, "test-owner")
            session.update(sheets_token_key="sheet", calendar_token_key="calendar")

    def providers(self):
        response = self.client.get("/api/hq/integrations")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return {item["name"]: item for item in response.json["integrations"]}

    def test_dashboard_owner_only_and_first_view_does_not_create_database(self):
        for path in ("/api/hq/integrations", "/hq/integrations"):
            self.assertEqual(self.client.get(path).status_code, 302)
        self.sign_in()
        providers = self.providers()
        self.assertEqual(providers["Google Sheets"]["status"], "Consent required in this session")
        self.assertEqual(providers["Square"]["status"], "Configuration required")
        self.assertIsNone(providers["Square"]["pending_records"])
        dashboard = self.client.get("/hq/integrations")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Unavailable", dashboard.data)
        self.assertEqual({p.name for p in self.data_dir.iterdir()}, {"owner_security.sqlite3"})

    def test_sheets_read_observations_preserve_history_and_do_not_touch_existing_database(self):
        self.sign_in()
        existing = self.data_dir / "customer_requests.sqlite3"
        with sqlite3.connect(existing) as db:
            db.execute("CREATE TABLE prior_records (name TEXT)")
            db.execute("INSERT INTO prior_records VALUES ('existing')")
        original = existing.read_bytes()
        with patch("app._google_tokens", {"sheet": ("private-token", time.time() + 60)}):
            self.assertEqual(self.providers()["Google Sheets"]["status"], "Consent present, read unverified")
            with patch("app.read_workbook_with_access_token", side_effect=[
                {"services": {}}, RuntimeError("private-token"), {"services": {}},
            ]):
                self.assertEqual(self.client.get("/api/hq/pricing").status_code, 200)
                verified = self.providers()["Google Sheets"]
                self.assertEqual(verified["status"], "Last read succeeded")
                self.assertTrue(verified["last_success"])
                self.assertEqual(self.client.get("/api/hq/pricing").status_code, 502)
                failed = self.providers()["Google Sheets"]
                self.assertEqual(failed["status"], "Last read failed")
                self.assertEqual(failed["last_success"], verified["last_success"])
                self.assertEqual(failed["last_error"], "Pricing workbook read failed")
                self.assertTrue(failed["intervention_required"])
                self.assertEqual(self.client.get("/api/hq/pricing").status_code, 200)
                self.assertFalse(self.providers()["Google Sheets"]["intervention_required"])
        self.assertEqual(existing.read_bytes(), original)
        self.assertEqual(self.providers()["Google Sheets"]["status"], "Consent required in this session")

    def test_calendar_success_requires_upstream_read(self):
        self.sign_in()
        with patch("app._calendar_tokens", {"calendar": ("token", time.time() + 60)}):
            with patch("app.live_conflicts", side_effect=[ValueError("bad time"), {"conflicts": []}]):
                path = "/api/hq/maintenance/calendar-live-check"
                self.assertEqual(self.client.post(path, json={}).status_code, 400)
                self.assertIsNone(self.providers()["Google Calendar"]["last_success"])
                self.assertEqual(self.client.post(path, json={
                    "start": "2026-11-18T09:00:00Z", "end": "2026-11-18T10:00:00Z",
                }).status_code, 200)
                self.assertEqual(self.providers()["Google Calendar"]["status"], "Last read succeeded")

    def test_committed_provider_reads_and_snapshots_record_atomically(self):
        from hq_integration_health import record_check, record_committed_sync, integration_status
        for provider in ("gmail", "square", "square_payments"):
            record_check(provider, "Earlier upstream failure")
            record_check(provider + "_sync", "Earlier storage failure")
            self.assertTrue(record_committed_sync(provider))
            observed = integration_status([(provider, provider, True)])
            self.assertTrue(observed["history_available"])
            self.assertEqual(observed["integrations"][0]["status"], "Last read succeeded")
            self.assertEqual(observed["sync_successes"][provider],
                             observed["integrations"][0]["last_success"])
            self.assertFalse(observed["sync_failures"][provider]["needs_attention"])
            # Preserve the historical error for audit, but clear current attention.
            self.assertEqual(observed["sync_failures"][provider]["last_error"],
                             "Earlier storage failure")
        with self.assertRaises(ValueError):
            record_committed_sync("finance")

    def test_failed_second_observation_rolls_back_first_success(self):
        from hq_integration_health import record_check, record_committed_sync, integration_status
        record_check("gmail", "Previous upstream failure")
        record_check("gmail_sync", "Previous snapshot failure")
        path = self.data_dir / "integration_health.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TRIGGER fail_fixture_sync
                BEFORE INSERT ON integration_checks
                WHEN NEW.provider = 'gmail_sync'
                BEGIN SELECT RAISE(FAIL, 'synthetic second write failure'); END""")
        self.assertFalse(record_committed_sync("gmail"))
        with sqlite3.connect(path) as db:
            states = {r[0]: (r[1], r[2], r[3]) for r in db.execute(
                "SELECT provider, last_success, last_error, needs_attention "
                "FROM integration_checks")}
        for key in ("gmail", "gmail_sync"):
            self.assertIsNone(states[key][0])
            self.assertEqual(states[key][2], 1)
        self.assertEqual(states["gmail"][1], "Previous upstream failure")
        self.assertEqual(states["gmail_sync"][1], "Previous snapshot failure")
        health = integration_status([("gmail", "Gmail", True)])
        self.assertIsNone(health["integrations"][0]["last_success"])
        self.assertIsNone(health["sync_successes"]["gmail"])
        self.assertTrue(health["sync_failures"]["gmail"]["needs_attention"])

        # Removing the synthetic fault allows the legitimate subsequent
        # post-commit observation to recover both stages without erasing data.
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER fail_fixture_sync")
        self.assertTrue(record_committed_sync("gmail"))
        health = integration_status([("gmail", "Gmail", True)])
        self.assertTrue(health["integrations"][0]["last_success"])
        self.assertEqual(health["sync_successes"]["gmail"],
                         health["integrations"][0]["last_success"])
        self.assertFalse(health["sync_failures"]["gmail"]["needs_attention"])
