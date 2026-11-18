"""Quote-to-job provenance and owner-only change history regression tests."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app import app
from customer_request_store import connect, create_request, create_review_draft
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import (
    create_internal_work_record, create_test_job, change_work_status,
    list_work_records, read_work_record, search_work_records, set_draft_financials,
)
from hq_work_history import work_record_history


class QuoteJobHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": temporary.name,
            "HQ_SESSION_SECRET": "history-test-secret",
            "GOOGLE_CLIENT_ID": "test",
            "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()

    def sign_in(self):
        with self.client.session_transaction() as session, auth_db() as db:
            session.update(hq_sid=issue_session(db, "test-owner"), csrf="token")

    def make_quote(self):
        request_id = create_request({
            "kind": "quote", "name": "PRIVATE TEST CUSTOMER",
            "email": "private-customer@example.invalid", "phone": "07123456789",
            "service": "Window Tinting", "make": "Ford", "model": "Focus",
            "year": "2020", "registration": "ZZ20 TEST",
            "date": "", "time": "", "notes": "SENSITIVE TEST NOTES",
            "privacy_acknowledged": True,
        }, "unique-quote-test")
        create_review_draft(request_id, "quote")
        return create_internal_work_record(request_id)

    def test_approved_quote_keeps_id_and_origin_in_existing_searches(self):
        work = self.make_quote()
        self.assertEqual(read_work_record(work)["original_draft_kind"], "quote")
        self.assertEqual(search_work_records(kind="quote")["records"][0]["id"], work)
        set_draft_financials(work, 35000, 7600, expected_revision=0)
        change_work_status(work, "ready_for_review", expected_revision=1)
        change_work_status(work, "approved", expected_revision=2)
        record = read_work_record(work)
        self.assertEqual((record["id"], record["kind"], record["status"]),
                         (work, "job", "approved"))
        self.assertEqual(record["original_draft_kind"], "quote")
        self.assertEqual(record["price_pence"], 35000)
        self.assertEqual([x["id"] for x in search_work_records(kind="job")["records"]], [work])
        self.assertEqual(search_work_records(kind="quote")["total"], 0)
        self.assertEqual(list_work_records()[0]["original_draft_kind"], "quote")
        history = work_record_history(work)
        self.assertTrue(history["approved_from_quote"])
        self.assertIsNotNone(history["approved_at"])
        self.assertEqual(history["original_draft_kind"], "quote")
        self.assertFalse(history["payment_verified"])
        self.assertEqual([e["action"] for e in history["events"]],
                         ["financials", "status", "status"])
        self.assertEqual(history["events"][-1]["before"]["kind"], "quote")
        self.assertEqual(history["events"][-1]["after"],
                         {"status": "approved", "kind": "job"})
        self.assertEqual(history["events"][0]["after"]["price_pence"], 35000)

    def test_unknown_synthetic_origin_not_mistaken_for_quote(self):
        work = create_test_job()
        self.assertIsNone(read_work_record(work)["original_draft_kind"])
        set_draft_financials(work, 10000, 0, expected_revision=0)
        change_work_status(work, "ready_for_review", expected_revision=1)
        change_work_status(work, "approved", expected_revision=2)
        history = work_record_history(work)
        self.assertIsNone(history["original_draft_kind"])
        self.assertFalse(history["approved_from_quote"])
        self.assertEqual(history["current_kind"], "job")

    def test_api_is_owner_only_no_store_and_never_leaks_customer_data(self):
        work = self.make_quote()
        self.assertEqual(self.client.get("/api/hq/work-records/" + work + "/history").status_code, 302)
        set_draft_financials(work, 15000, 0, expected_revision=0)
        self.sign_in()
        with connect() as db:
            before = db.execute("SELECT COUNT(*) FROM hq_work_audit").fetchone()[0]
            # A future/old unexpected field must never appear in the API.
            db.execute("UPDATE hq_work_audit SET after_value=? WHERE work_id=? AND action='financials'",
                       (json.dumps({"price_pence": 15000, "deposit_pence": 0,
                                    "email": "LEAK-SENTINEL@example.invalid",
                                    "notes": "LEAK-SENTINEL"}), work))
        res = self.client.get("/api/hq/work-records/" + work + "/history")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("Cache-Control"), "no-store")
        self.assertIn("original_draft_kind", res.json)
        for sensitive in ("PRIVATE TEST CUSTOMER", "private-customer@example.invalid",
                          "07123456789", "ZZ20 TEST", "SENSITIVE TEST NOTES",
                          "LEAK-SENTINEL"):
            self.assertNotIn(sensitive, res.get_data(as_text=True))
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_audit").fetchone()[0], before)

    def test_missing_or_invalid_references_fail_closed(self):
        self.sign_in()
        self.assertEqual(self.client.get("/api/hq/work-records/bad/history").status_code, 400)
        self.assertEqual(self.client.get("/api/hq/work-records/" + "a"*32 + "/history").status_code, 404)

    def test_history_is_available_in_existing_job_card_ui(self):
        self.sign_in()
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Review quote & job history", html)
        self.assertIn('"/api/hq/work-records/"+encodeURIComponent(job.id)+"/history"', html)
        self.assertIn("original_draft_kind", html)
        self.assertIn("Entr" + "ed financial amounts are not payment verification.", html)


if __name__ == "__main__":
    unittest.main()
