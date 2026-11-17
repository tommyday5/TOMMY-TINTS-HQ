"""Regression coverage for owner-only optimistic work-record edits and migration."""
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import (
    StaleWorkRecordError, change_work_status, create_test_job,
    read_work_record, search_work_records, set_draft_financials, work_summary,
)


class WorkRecordRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.temp.name,
            "HQ_SESSION_SECRET": "test-revision-secret",
            "GOOGLE_CLIENT_ID": "test", "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()

    def sign_in(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "test-owner"), csrf="revision-csrf")

    def post(self, record, endpoint, values):
        return self.client.post("/api/hq/work-records/" + record + "/" + endpoint,
            json=values, headers={"X-HQ-CSRF": "revision-csrf"})

    def test_revision_conflict_and_idempotent_repeat_do_not_overwrite_financials(self):
        work = create_test_job()
        self.assertEqual(read_work_record(work)["revision"], 0)
        saved = set_draft_financials(work, 25000, 7600, expected_revision=0)
        self.assertEqual((saved["balance_pence"], saved["revision"]), (17400, 1))
        self.assertEqual(set_draft_financials(work, 25000, 7600, expected_revision=0)["revision"], 1)
        with self.assertRaises(StaleWorkRecordError):
            set_draft_financials(work, 27500, 7600, expected_revision=0)
        self.assertEqual(read_work_record(work)["price_pence"], 25000)
        updated = set_draft_financials(work, 27500, 7600, expected_revision=1)
        self.assertEqual(updated["revision"], 2)
        with connect() as db:
            rows = db.execute("SELECT * FROM hq_work_audit WHERE work_id=? ORDER BY revision", (work,)).fetchall()
        self.assertEqual([row["revision"] for row in rows], [1, 2])
        self.assertEqual(json.loads(rows[0]["before_value"])["price_pence"], None)
        self.assertEqual(json.loads(rows[1]["after_value"])["price_pence"], 27500)
        self.assertTrue(all("TEST CUSTOMER" not in row["after_value"] for row in rows))

    def test_status_rejects_stale_price_review_and_tracks_promoted_quotes(self):
        work = create_test_job()
        set_draft_financials(work, 20000, 5000, expected_revision=0)
        with self.assertRaises(StaleWorkRecordError):
            change_work_status(work, "ready_for_review", expected_revision=0)
        self.assertEqual(change_work_status(work, "ready_for_review", expected_revision=1), "ready_for_review")
        self.assertEqual(read_work_record(work)["revision"], 2)
        self.assertEqual(change_work_status(work, "ready_for_review", expected_revision=1), "ready_for_review")
        self.assertEqual(read_work_record(work)["revision"], 2)
        with self.assertRaises(StaleWorkRecordError):
            set_draft_financials(work, 21000, 5000, expected_revision=1)
        self.assertEqual(read_work_record(work)["price_pence"], 20000)
        with connect() as db:
            events = db.execute("SELECT action, revision FROM hq_work_audit ORDER BY revision").fetchall()
        self.assertEqual([(row["action"], row["revision"]) for row in events],
                         [("financials", 1), ("status", 2)])

    def test_http_writes_require_owner_csrf_revision_and_return_409(self):
        work = create_test_job()
        endpoint = "financials"
        fields = {"price_pence": 25000, "deposit_pence": 7600, "expected_revision": 0}
        self.assertEqual(self.post(work, endpoint, fields).status_code, 302)
        self.sign_in()
        self.assertEqual(self.client.post(
            "/api/hq/work-records/" + work + "/financials", json=fields).status_code, 403)
        for payload in ({**fields, "expected_revision": True},
                        {**fields, "expected_revision": -1},
                        {"price_pence": 25000, "deposit_pence": 7600}):
            self.assertEqual(self.post(work, endpoint, payload).status_code, 400)
        first = self.post(work, endpoint, fields)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["revision"], 1)
        page = self.client.get("/api/hq/work-records/search")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.json["records"][0]["revision"], 1)
        self.assertEqual(self.client.get("/api/hq/work-records").json["records"][0]["revision"], 1)
        stale = self.post(work, endpoint, {**fields, "price_pence": 35000})
        self.assertEqual(stale.status_code, 409)
        self.assertIn("Refresh", stale.json["error"])
        self.assertEqual(self.post(work, endpoint, fields).json["revision"], 1)
        self.assertEqual(self.post(work, "status", {"status": "ready_for_review"}).status_code, 400)
        self.assertEqual(self.post(work, "status", {"status": "ready_for_review", "expected_revision": 0}).status_code, 409)
        self.assertEqual(self.post(work, "status", {"status": "ready_for_review", "expected_revision": 1}).status_code, 200)
        self.assertEqual(read_work_record(work)["revision"], 2)
        self.assertEqual(read_work_record(work)["price_pence"], 25000)
        self.assertEqual(work_summary()["record_count"], 1)

    def test_simultaneous_form_submissions_have_single_winner(self):
        work = create_test_job()
        def write(value):
            try:
                return set_draft_financials(work, value, 0, expected_revision=0)["revision"]
            except StaleWorkRecordError:
                return "stale"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, (18000, 19000)))
        self.assertCountEqual(results, [1, "stale"])
        self.assertIn(read_work_record(work)["price_pence"], (18000, 19000))
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_audit").fetchone()[0], 1)

    def test_legacy_database_migrates_additively_without_losing_prices(self):
        with connect() as db:
            db.execute("""CREATE TABLE hq_work_records (
                id TEXT PRIMARY KEY, source_request_id TEXT NOT NULL UNIQUE,
                customer_id TEXT NOT NULL, vehicle_id TEXT NOT NULL,
                kind TEXT NOT NULL, service TEXT NOT NULL, notes TEXT NOT NULL,
                preferred_date TEXT NOT NULL, preferred_time TEXT NOT NULL,
                status TEXT NOT NULL, price_pence INTEGER,
                deposit_pence INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
            db.execute("""INSERT INTO hq_work_records VALUES (
                ?, 'TEST-legacy', 'customer', 'vehicle', 'job', 'Window Tinting',
                'Historical record', '', '', 'draft', 12500, 2500, '2026-01-01')""",
                ("a" * 32,))
        self.assertEqual(work_summary()["outstanding_recorded_pence"], 10000)
        self.assertEqual(set_draft_financials("a" * 32, 13000, 2500, expected_revision=0)["revision"], 1)
        with connect() as db:
            original = db.execute("SELECT price_pence,deposit_pence,revision FROM hq_work_records WHERE id=?", ("a" * 32,)).fetchone()
            self.assertEqual(tuple(original), (13000, 2500, 1))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_audit").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
