import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
            session.update(hq_auth=True, sheets_token_key="sheet", calendar_token_key="calendar")

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
        self.assertEqual(providers["Square"]["status"], "Not implemented")
        self.assertIsNone(providers["Square"]["pending_records"])
        dashboard = self.client.get("/hq/integrations")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Unavailable", dashboard.data)
        self.assertFalse(list(self.data_dir.iterdir()))

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
