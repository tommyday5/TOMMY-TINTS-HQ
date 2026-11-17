"""Owner-only, searchable pagination of HQ internal job/quote records."""
import os
import tempfile
import unittest
from unittest.mock import patch

from app import app
from customer_request_store import create_request, create_review_draft
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import (
    create_internal_work_record, create_test_job, search_work_records,
)


class SearchWorkRecordsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name, "HQ_SESSION_SECRET": "test",
            "GOOGLE_CLIENT_ID": "test", "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()

    def sign_in(self):
        with self.client.session_transaction() as session, auth_db() as db:
            session.update(hq_sid=issue_session(db, "test-owner"), csrf="token")

    def make_quote(self):
        payload = {
            "kind": "quote", "name": "Alice Example", "email": "alice@example.invalid",
            "phone": "07123456789", "service": "Wheel Refurbishment",
            "make": "Ford", "model": "Fiesta", "year": "2019",
            "registration": "AB19 XYZ", "date": "", "time": "",
            "notes": "Four wheels", "privacy_acknowledged": True,
        }
        req = create_request(payload, "fingerprint-quote")
        create_review_draft(req, "quote")
        return create_internal_work_record(req)

    def test_stable_pagination_includes_more_than_latest_100(self):
        ids = {create_test_job() for _ in range(105)}
        pages = [search_work_records(limit=25, offset=page * 25)
                 for page in range(5)]
        self.assertEqual([p["total"] for p in pages], [105] * 5)
        self.assertEqual([len(p["records"]) for p in pages], [25, 25, 25, 25, 5])
        self.assertEqual([p["has_more"] for p in pages],
                         [True, True, True, True, False])
        self.assertEqual({row["id"] for p in pages for row in p["records"]}, ids)
        self.assertEqual(search_work_records(offset=110)["records"], [])

    def test_searches_customer_vehicle_service_and_literal_wildcards(self):
        quote = self.make_quote()
        job = create_test_job()
        for query in ("alice", "ALICE EXAMPLE", "AB19", "Fiesta", "wHEEL", "alice@example.invalid",
                      "071234"):
            with self.subTest(query=query):
                result = search_work_records(query=query)
                self.assertEqual([r["id"] for r in result["records"]], [quote])
        self.assertEqual([r["id"] for r in search_work_records(kind="quote")["records"]], [quote])
        self.assertEqual([r["id"] for r in search_work_records(kind="job")["records"]], [job])
        self.assertEqual(search_work_records(kind="quote", status="completed")["total"], 0)
        self.assertEqual(search_work_records(query="%")["total"], 0)
        self.assertEqual(search_work_records(query="_")["total"], 0)
        self.assertEqual(search_work_records(query="AB19", kind="job")["total"], 0)

    def test_invalid_search_parameters_fail_closed(self):
        for kwargs in ({"limit": 0}, {"limit": 51}, {"limit": True},
                       {"offset": -1}, {"offset": True}, {"offset": 100001},
                       {"kind": "booking"}, {"status": "paid"}, {"query": "x" * 81},
                       {"query": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                search_work_records(**kwargs)

    def test_http_requires_owner_and_rejects_invalid_pages(self):
        quote = self.make_quote()
        path = "/api/hq/work-records/search"
        self.assertEqual(self.client.get(path).status_code, 302)
        self.sign_in()
        response = self.client.get(path, query_string={
            "q": "AB19", "kind": "quote", "limit": "25", "offset": "0"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["records"][0]["id"], quote)
        self.assertEqual(data["records"][0]["name"], "Alice Example")
        self.assertEqual(data["offset"], 0)
        self.assertFalse(data["has_more"])
        for query_string in ({"limit": "0"}, {"limit": "abc"},
                             {"offset": "-5"}, {"offset": "100001"},
                             {"kind": "other"}, {"status": "paid"}, {"q": "x" * 81}):
            with self.subTest(query_string=query_string):
                self.assertEqual(self.client.get(path, query_string=query_string).status_code, 400)
        # Existing consumers still receive the original list response.
        legacy = self.client.get("/api/hq/work-records")
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(legacy.get_json()["records"][0]["id"], quote)

    def test_dashboard_exposes_search_and_paging_controls(self):
        self.sign_in()
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        for required_id in (b'id="workSearch"', b'id="workKind"',
                            b'id="workStatus"', b'id="workPrev"',
                            b'id="workNext"', b'id="workPageStatus"'):
            self.assertIn(required_id, page.data)
        self.assertIn(b"/api/hq/work-records/search?", page.data)


if __name__ == "__main__":
    unittest.main()
