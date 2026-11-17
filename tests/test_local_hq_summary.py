"""Owner-only local HQ work summaries: functional and privacy regressions."""
import os
import tempfile
import unittest
from unittest.mock import patch

from hq_auth import issue_session
from hq_auth_store import auth_db
from app import app
from hq_assistant_local import answer, LocalRecordsUnavailable
from hq_core_records import create_test_job, set_draft_financials


class LocalSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.temp.name,
            "HQ_SESSION_SECRET": "test",
            "GOOGLE_CLIENT_ID": "test",
            "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
            "OPENAI_API_KEY": "", "OPENAI_MODEL": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_local_summary_uses_actual_hq_records_with_unverified_financial_label(self):
        job = create_test_job()
        set_draft_financials(job, 25000, 7600)
        with (patch("hq_assistant_local.storage_ready", return_value=True),
              patch("app.openai_chat") as provider):
            with app.test_client() as client:
                self.assertEqual(client.post("/api/hq/assistant/chat", json={"message": "HQ work summary"}).status_code, 302)
                with client.session_transaction() as sess:
                    with auth_db() as db:
                        sess["hq_sid"] = issue_session(db, "test-owner")
                    sess.update(csrf="csrf")
                self.assertEqual(client.post("/api/hq/assistant/chat", json={"message": "HQ work summary"}).status_code, 403)
                result = client.post("/api/hq/assistant/chat",
                    headers={"X-HQ-CSRF": "csrf"}, json={"message": "HQ work summary"})
                self.assertEqual(result.status_code, 200)
                data = result.get_json()
                self.assertEqual(data["mode"], "hq_records_local")
                self.assertEqual(data["actions_executed"], 0)
                self.assertFalse(data["payment_verified"])
                self.assertIn("HQ internal work records: 1", data["reply"])
                self.assertIn("£250.00", data["reply"])
                self.assertIn("£174.00", data["reply"])
                self.assertIn("synthetic TEST", data["reply"])
            provider.assert_not_called()

    def test_unknown_question_never_reads_work_records(self):
        with patch("hq_assistant_local.work_summary") as reader:
            self.assertIsNone(answer("What is my live Monzo balance?"))
            reader.assert_not_called()

    def test_unconfigured_persistent_storage_fails_closed(self):
        with (patch("hq_assistant_local.storage_ready", return_value=False),
              patch("hq_assistant_local.work_summary") as reader):
            with self.assertRaises(LocalRecordsUnavailable):
                answer("show my hq work summary")
            reader.assert_not_called()

    def test_invalid_messages_are_rejected(self):
        for message in ("", " ", None, 7, "x" * 3001):
            with self.subTest(message=str(message)[:15]), self.assertRaises(ValueError):
                answer(message)


if __name__ == "__main__":
    unittest.main()
