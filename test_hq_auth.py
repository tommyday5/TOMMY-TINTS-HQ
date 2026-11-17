"""Authentication regression tests: no credentials must fail closed."""
import os
import unittest
from unittest.mock import patch
from app import app

class AuthTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
    def test_no_credentials_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(b"Command centre", response.data)
    def test_health_does_not_expose_secrets(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"GOOGLE_CLIENT_SECRET", response.data)
    def test_login_requires_config(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.client.get("/login").status_code, 503)
    def test_unauthenticated_landing_offers_login(self):
        with patch.dict(os.environ, {
            "HQ_SESSION_SECRET": "testing-not-production",
            "GOOGLE_CLIENT_ID": "test-client",
            "GOOGLE_CLIENT_SECRET": "test-secret",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.com",
        }, clear=True):
            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"/login", response.data)
            self.assertIn(b"apple-touch-icon", response.data)
            self.assertNotIn(b"customerRequestsList", response.data)
    def test_logout_rejects_missing_csrf(self):
        with patch.dict(os.environ, {
            "HQ_SESSION_SECRET": "testing-not-production",
            "GOOGLE_CLIENT_ID": "test-client",
            "GOOGLE_CLIENT_SECRET": "test-secret",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.com",
        }, clear=True):
            with self.client.session_transaction() as session:
                session["hq_auth"] = True
                session["csrf"] = "known-token"
            self.assertEqual(self.client.post("/logout").status_code, 400)

if __name__ == "__main__":
    unittest.main()
