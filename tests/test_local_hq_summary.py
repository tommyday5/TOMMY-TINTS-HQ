"""Owner-only local HQ work summaries: functional and privacy regressions."""
import os
import tempfile
import unittest
from unittest.mock import patch

from hq_auth import issue_session
from hq_auth_store import auth_db
from app import app
from hq_assistant_local import answer, LocalRecordsUnavailable
from hq_core_records import create_test_job, set_draft_financials, create_internal_work_record
from customer_request_store import create_request, create_review_draft


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


    def test_read_only_quote_and_job_lookup_are_private_and_authenticated(self):
        payload = dict(kind="quote", name="Example Workshop Customer", email="owner@example.test",
                       phone="07123456789", service="Window Tinting", make="Audi", model="A4",
                       year="2020", registration="AB20 XYZ", date="", time="",
                       notes="Private repair notes", privacy_acknowledged=True)
        ref = create_request(payload, "test-fingerprint")
        create_review_draft(ref, "quote")
        quote = create_internal_work_record(ref)
        set_draft_financials(quote, 25000, 7600)
        job = create_test_job()
        with app.test_client() as client, patch("app.openai_chat", return_value={"reply": "Remote AI reply"}) as provider:
            path = "/api/hq/assistant/chat"
            # Owner sign-in and CSRF remain mandatory for each local lookup.
            self.assertEqual(client.post(path, json={"message": "HQ recent quotes"}).status_code, 302)
            with client.session_transaction() as sess:
                with auth_db() as db:
                    sess["hq_sid"] = issue_session(db, "test-owner")
                sess.update(csrf="csrf")
            self.assertEqual(client.post(path, json={"message": "HQ recent quotes"}).status_code, 403)
            def ask(msg):
                return client.post(path, headers={"X-HQ-CSRF": "csrf"}, json={"message": msg})
            quotes = ask("HQ recent quotes")
            self.assertEqual(quotes.status_code, 200)
            self.assertIn(quote, quotes.json["reply"])
            self.assertNotIn("Example Workshop Customer", quotes.json["reply"])
            self.assertNotIn("AB20 XYZ", quotes.json["reply"])
            jobs = ask("HQ recent jobs")
            self.assertEqual(jobs.status_code, 200)
            self.assertIn(job, jobs.json["reply"])
            self.assertNotIn(quote, jobs.json["reply"])
            detail = ask("HQ record " + quote)
            self.assertEqual(detail.status_code, 200)
            self.assertIn("Example Workshop Customer", detail.json["reply"])
            self.assertIn("AB20 XYZ", detail.json["reply"])
            self.assertIn("£250.00", detail.json["reply"])
            self.assertIn("£174.00", detail.json["reply"])
            self.assertNotIn("owner@example.test", detail.json["reply"])
            self.assertEqual(detail.json["actions_executed"], 0)
            self.assertEqual(ask("HQ record invalid").status_code, 400)
            provider.assert_not_called()
            # The complete local transcript is kept for the owner, never
            # attached to subsequent external OpenAI requests.
            visible = client.get("/api/hq/assistant/history").json["messages"]
            self.assertTrue(any("Example Workshop Customer" in m["content"] for m in visible))
            remote = ask("Good morning")
            self.assertEqual(remote.status_code, 200)
            self.assertEqual(remote.json["reply"], "Remote AI reply")
            provider.assert_called_once()
            history_sent = provider.call_args.args[1]
            self.assertFalse(any("Example Workshop Customer" in m["content"]
                                 or "AB20 XYZ" in m["content"] for m in history_sent))

    def test_missing_and_invalid_local_records_never_call_provider(self):
        with app.test_client() as client, patch("app.openai_chat") as provider:
            with client.session_transaction() as sess:
                with auth_db() as db:
                    sess["hq_sid"] = issue_session(db, "test-owner")
                sess.update(csrf="csrf")
            response = client.post("/api/hq/assistant/chat",
                headers={"X-HQ-CSRF": "csrf"}, json={"message": "HQ record " + "a" * 32})
            self.assertEqual(response.status_code, 404)
            provider.assert_not_called()

if __name__ == "__main__":
    unittest.main()
