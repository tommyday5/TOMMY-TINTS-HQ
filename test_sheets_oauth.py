"""Read-only Google Sheets OAuth access regression tests."""
import os
import unittest
from unittest.mock import patch
from app import app, _google_tokens, _google_tokens_lock

ENV={"HQ_SESSION_SECRET":"testing-only","GOOGLE_CLIENT_ID":"test-id",
     "GOOGLE_CLIENT_SECRET":"test-secret","HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.com"}

class SheetsOAuthTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"]=True
        self.client=app.test_client()
        with _google_tokens_lock:
            _google_tokens.clear()

    def test_unauthenticated_cannot_read(self):
        with patch.dict(os.environ,ENV,clear=True):
            result=self.client.get("/api/hq/pricing")
            self.assertEqual(result.status_code,302)

    def test_owner_without_consent_cannot_read(self):
        with patch.dict(os.environ,ENV,clear=True):
            with self.client.session_transaction() as s:
                s["hq_auth"]=True
            result=self.client.get("/api/hq/pricing")
            self.assertEqual(result.status_code,401)
            self.assertIn("connect_url",result.json)

    def test_expired_token_rejected(self):
        with patch.dict(os.environ,ENV,clear=True):
            with self.client.session_transaction() as s:
                s["hq_auth"]=True
                s["sheets_token_key"]="expired"
            with _google_tokens_lock:
                _google_tokens["expired"]=("never-use",0)
            self.assertEqual(self.client.get("/api/hq/pricing").status_code,401)

    def test_no_token_leaked_by_health(self):
        with patch.dict(os.environ,ENV,clear=True):
            self.assertNotIn(b"access_token",self.client.get("/health").data)

if __name__=="__main__":
    unittest.main()
