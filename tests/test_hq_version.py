import os
import unittest
from unittest.mock import patch

from hq_auth import issue_session
from hq_auth_store import auth_db
from app import app
from hq_version import LOCAL_VERSION, UPDATE_SCRIPT_VERSION


class HQVersionTests(unittest.TestCase):
    def setUp(self):
        self.version = "a" * 40
        env = patch.dict(os.environ, RAILWAY_GIT_COMMIT_SHA=self.version,
                         HQ_SESSION_SECRET="test", GOOGLE_CLIENT_ID="test",
                         GOOGLE_CLIENT_SECRET="test", HQ_ALLOWED_GOOGLE_EMAIL="owner@example.invalid")
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()

    def test_public_version_only_and_no_cache(self):
        response = self.client.get("/api/hq/version")
        self.assertEqual(response.json, {"version": self.version})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        with patch.dict(os.environ, RAILWAY_GIT_COMMIT_SHA="invalid"):
            self.assertEqual(self.client.get("/api/hq/version").json, {"version": LOCAL_VERSION})

    def test_html_and_owner_routes_are_not_stored(self):
        for path in ("/", "/install-hq", "/static/timer.html", "/static/manifest.webmanifest",
                     "/api/hq/work-records", "/hq/integrations"):
            with self.client.get(path) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        for path in ("/", "/install-hq"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn(f'data-build="{self.version}"', html)
            self.assertIn('data-automatic="true"', html)
            self.assertNotIn('id="hq-latest-version"', html)
        with self.client.session_transaction() as session:
            with auth_db() as db:
                session["hq_sid"] = issue_session(db, "test-owner")
            session.update(csrf="test")
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-automatic="false"', html)
        self.assertIn('id="hq-latest-version"', html)
        self.assertIn(f'<code style="overflow-wrap:anywhere">{self.version}</code>', html)
        self.assertIn(f'/static/hq-updates.js?v={UPDATE_SCRIPT_VERSION}', html)

    def test_only_matching_versioned_script_and_icon_are_immutable(self):
        for path in (f"/static/hq-updates.js?v={UPDATE_SCRIPT_VERSION}", "/hq-icon-180.png?v=20261117d"):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
                self.assertIn("immutable", response.headers["Cache-Control"])
                self.assertIn("public", response.headers["Cache-Control"])
        with self.client.get("/static/hq-updates.js?v=old") as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        with self.client.get("/hq-icon-180.png?v=old") as response:
            self.assertNotIn("immutable", response.headers["Cache-Control"])
