"""Only synthetic provider evidence: owner-only read, no writes or false readiness."""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from test_sheet_link_preflight import fixture


class SheetsLinkInspectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": temp.name,
            "HQ_SESSION_SECRET": "fixture-only-job-link-inspection",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
            "GOOGLE_CLIENT_ID": "fixture-id",
            "GOOGLE_CLIENT_SECRET": "fixture-secret",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()

    def sign_in(self):
        with self.client.session_transaction() as sess:
            with auth_db() as db:
                sess["hq_sid"] = issue_session(db, "fixture-owner")
            sess["csrf"] = "fixture-csrf"

    def post(self, body=None, csrf="fixture-csrf"):
        return self.client.post(
            "/api/hq/sheets/job-link-preflight",
            json=body or {"job_ref": "J0083", "target_row": 6, "mode": "new"},
            headers={"X-HQ-CSRF": csrf},
        )

    def test_owner_only_and_explicit_post(self):
        unauthorised = self.post()
        self.assertNotEqual(unauthorised.status_code, 200)
        self.sign_in()
        page = self.client.get("/hq/sheets/job-link-preflight")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"read-only linking inspection", page.data)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertEqual(self.post(csrf="wrong").status_code, 403)
        self.assertEqual(self.client.get("/api/hq/sheets/job-link-preflight").status_code, 405)

    def test_reject_invalid_candidate_before_any_provider_call(self):
        self.sign_in()
        cases = [
            {"job_ref": "J0083", "target_row": True, "mode": "new"},
            {"job_ref": "J0083", "target_row": 2001, "mode": "new"},
            {"job_ref": "J0083", "target_row": 6, "mode": "write"},
            {"job_ref": "=IMPORTXML()", "target_row": 6, "mode": "new"},
        ]
        with patch("app._google_connection", side_effect=AssertionError("provider call")):
            for values in cases:
                with self.subTest(values=values):
                    self.assertEqual(self.post(values).status_code, 400)

    def test_missing_owner_google_consent_never_reads(self):
        self.sign_in()
        with patch("hq_sheets_live.requests.get", side_effect=AssertionError("no provider read")):
            response = self.post()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json["connect_url"], "/auth/sheets/connect")

    def test_synthetic_success_stays_snapshot_only(self):
        self.sign_in()
        with patch("app._google_connection", return_value=("synthetic-token", time.time() + 300)), \
             patch("hq_sheets_link_inspection.read_job_link_snapshot", return_value=fixture()) as read, \
             patch("hq_sheets_link_inspection.record_check", return_value=True) as observed:
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["complete_reference_scan"])
        for name in ("live_sync", "write_authorised", "historical_mapping_verified", "reconciled"):
            self.assertFalse(response.json[name])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("J0077", response.get_data(as_text=True))
        read.assert_called_once_with("synthetic-token")
        observed.assert_called_once_with("sheets_job_preflight")

    def test_conflicting_sheet_never_returns_private_rows(self):
        self.sign_in()
        snap = fixture()
        with patch("app._google_connection", return_value=("synthetic-token", time.time() + 300)), \
             patch("hq_sheets_link_inspection.read_job_link_snapshot", return_value=snap), \
             patch("hq_sheets_link_inspection.record_check", return_value=True):
            response = self.post({"job_ref":"J0077", "target_row":6, "mode":"new"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json["live_sync"])
        self.assertNotIn("J0077", response.get_data(as_text=True))

    def test_read_error_and_observation_failure_never_claim_success(self):
        self.sign_in()
        with patch("app._google_connection", return_value=("synthetic-token", time.time() + 300)), \
             patch("hq_sheets_link_inspection.read_job_link_snapshot",
                   side_effect=RuntimeError("private source material")), \
             patch("hq_sheets_link_inspection.record_check", return_value=True):
            failed = self.post()
        self.assertEqual(failed.status_code, 502)
        self.assertNotIn(b"private source material", failed.data)
        with patch("app._google_connection", return_value=("synthetic-token", time.time() + 300)), \
             patch("hq_sheets_link_inspection.read_job_link_snapshot", return_value=fixture()), \
             patch("hq_sheets_link_inspection.record_check", return_value=False):
            missing_audit = self.post()
        self.assertEqual(missing_audit.status_code, 503)
        self.assertIn(b"observation storage failed", missing_audit.data)


if __name__ == "__main__":
    unittest.main()
