"""Owner-only local HQ work summaries: functional and privacy regressions."""
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from hq_auth import issue_session
from hq_auth_store import auth_db
from app import app
from hq_assistant_local import answer, LocalRecordsUnavailable
from hq_core_records import (create_test_job, set_draft_financials,
                             create_internal_work_record, change_work_status)
from customer_request_store import create_request, create_review_draft, connect


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
                self.assertIn("Approved job prices (NOT collected): £0.00", data["reply"])
                self.assertIn("Planned deposits (NOT confirmed received): £0.00", data["reply"])
                self.assertIn("Verified cash received and actual unpaid balances: unavailable", data["reply"])
                self.assertIn("synthetic TEST", data["reply"])
                # A draft TEST job is not an approved sale or a cash receipt.
                self.assertNotIn("£250.00", data["reply"])
                self.assertNotIn("£174.00", data["reply"])
            provider.assert_not_called()


    def test_approved_pipeline_excludes_synthetic_jobs_and_does_not_claim_cash(self):
        payload = dict(kind="quote", name="Private Customer", email="example@example.invalid",
                       phone="07123456789", service="Window Tinting", make="Ford", model="Focus",
                       year="2021", registration="AB21 XYZ", date="", time="", notes="",
                       privacy_acknowledged=True)
        source = create_request(payload, "local-summary-" + uuid.uuid4().hex)
        create_review_draft(source, "job")
        real_job = create_internal_work_record(source)
        set_draft_financials(real_job, 35000, 7600)
        change_work_status(real_job, "ready_for_review")
        change_work_status(real_job, "approved")

        fake_job = create_test_job()
        set_draft_financials(fake_job, 99000, 3000)
        change_work_status(fake_job, "ready_for_review")
        change_work_status(fake_job, "approved")

        with patch("hq_assistant_local.storage_ready", return_value=True):
            response = answer("HQ work summary")["reply"]
        self.assertIn("HQ internal work records: 2", response)
        self.assertIn("Approved priced jobs counted: 1", response)
        self.assertIn("Approved job prices (NOT collected): £350.00", response)
        self.assertIn("Planned deposits (NOT confirmed received): £76.00", response)
        self.assertIn("Conditional remainder after planned deposits (NOT verified outstanding): £274.00", response)
        self.assertIn("Synthetic approved TEST records excluded from these financial totals: 1", response)
        self.assertIn("Verified cash received and actual unpaid balances: unavailable", response)
        self.assertNotIn("£990.00", response)
        self.assertNotIn("£960.00", response)
        self.assertNotIn("received £76", response.lower())

    def test_inconsistent_legacy_deposit_withholds_remainder_in_record_detail(self):
        job = create_test_job()
        set_draft_financials(job, 10000, 1000)
        with connect() as db:
            db.execute("UPDATE hq_work_records SET deposit_pence=20000 WHERE id=?", (job,))
        with patch("hq_assistant_local.storage_ready", return_value=True):
            response = answer("HQ record " + job)["reply"]
        self.assertIn("planned deposit (NOT confirmed received): £200.00", response)
        self.assertIn("conditional remainder after planned deposit (NOT verified outstanding): Unavailable", response)
        self.assertNotIn("£-100.00", response)

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
        with app.test_client() as client, patch("app.openai_chat", return_value={"reply": "Remote AI reply"}) as provider, patch("hq_assistant_local.storage_ready", return_value=True):
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
            self.assertIn("planned deposit (NOT confirmed received)", detail.json["reply"])
            self.assertIn("NOT verified outstanding", detail.json["reply"])
            self.assertNotIn("expected balance (unverified)", detail.json["reply"])
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
        with app.test_client() as client, patch("app.openai_chat") as provider, patch("hq_assistant_local.storage_ready", return_value=True):
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
