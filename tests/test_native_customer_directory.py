"""Synthetic owner-only HQ customer directory and public-to-HQ linkage tests."""
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import _schema
from hq_customer_directory import find_customers, customer_snapshot
from hq_native_work import create_native_work, native_work_record

ROOT = Path(__file__).resolve().parents[1]


class CustomerDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.tmp.name,
            "HQ_SESSION_SECRET": "directory-test-fixture-secret",
            "GOOGLE_CLIENT_ID": "synthetic",
            "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()
        self.payload = {
            "operation_id": uuid.uuid4().hex,
            "customer_id": "", "vehicle_id": "",
            "customer_name": "Synthetic Directory Customer",
            "email": "directory@example.invalid", "phone": "07700 900666",
            "make": "Ford", "model": "Fiesta", "model_year": "2021",
            "registration": "AA21 BBB", "kind": "quote",
            "service": "Window Tinting", "customer_tier": "retail",
            "notes": "private note not for customer directory",
            "preferred_date": "", "preferred_time": "",
        }
        with app.app_context():
            self.work_id = create_native_work(self.payload)
        self.customer_id = native_work_record(self.work_id)["customer_id"]

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "test-owner"),
                        csrf="directory-csrf", google_sub="test-owner")

    def test_owner_auth_csrf_and_private_response_headers(self):
        self.assertEqual(self.client.get("/hq/customers").status_code, 302)
        self.assertEqual(self.client.get("/hq/customers/" + self.customer_id).status_code, 302)
        self.login()
        response = self.client.get("/hq/customers")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("noindex", response.headers["X-Robots-Tag"])
        self.assertIn(b"Synthetic Directory Customer", response.data)
        self.assertEqual(self.client.post("/hq/customers", data={"q": "Synthetic"}).status_code, 403)
        self.assertEqual(self.client.post("/hq/customers", data={
            "csrf": "bad", "q": "Synthetic"}).status_code, 403)
        self.assertEqual(self.client.post("/hq/customers", data={
            "csrf": "directory-csrf", "q": "X" * 81, "offset": "0"}).status_code, 400)
        self.assertEqual(self.client.post("/hq/customers", data={
            "csrf": "directory-csrf", "q": "Synthetic", "offset": "-1"}).status_code, 400)
        self.assertEqual(self.client.get("/hq/customers/not-an-id").status_code, 404)
        # Search text is only submitted in a private POST body, not a request URL.
        self.assertNotIn(b'name="q" value=', response.data)

    def test_name_contact_registration_search_and_no_cost_exposure(self):
        self.login()
        for term in ("Synthetic", "directory@example.invalid", "900666", "AA21 BBB", "aa21"):
            with self.subTest(term=term):
                result = self.client.post("/hq/customers", data={
                    "csrf": "directory-csrf", "q": term, "offset": "0"})
                self.assertEqual(result.status_code, 200)
                self.assertIn(b"Synthetic Directory Customer", result.data)
                self.assertIn(b"View profile", result.data)
        self.assertEqual(self.client.post("/hq/customers", data={
            "csrf": "directory-csrf", "q": "no matching customer", "offset": "0"
        }).status_code, 200)
        self.assertEqual(find_customers("no matching customer")[0], 0)
        result = self.client.get("/hq/customers/" + self.customer_id)
        self.assertEqual(result.status_code, 200)
        self.assertIn(self.work_id.encode(), result.data)
        self.assertIn(b"AA21 BBB", result.data)
        self.assertIn(b"Create quote or job for this customer", result.data)
        self.assertNotIn(b"private note", result.data)
        self.assertNotIn(b"materials cost", result.data)
        self.assertNotIn(b"bank balance", result.data)
        self.assertIn("no-store", result.headers["Cache-Control"])

    def test_profile_can_preselect_customer_and_reuse_canonical_ids(self):
        self.login()
        page = self.client.get("/hq/work/new?customer_id=" + self.customer_id)
        self.assertEqual(page.status_code, 200)
        self.assertIn(('value="' + self.customer_id + '" selected').encode(), page.data)
        self.assertEqual(self.client.get("/hq/work/new?customer_id=bad").status_code, 404)
        self.assertEqual(self.client.get("/hq/work/new?customer_id=" + uuid.uuid4().hex).status_code, 404)
        another = {**self.payload, "operation_id": uuid.uuid4().hex,
                   "customer_id": self.customer_id, "customer_name": "",
                   "email": "", "phone": "", "vehicle_id": native_work_record(self.work_id)["vehicle_id"],
                   "make": "", "model": "", "model_year": "", "registration": ""}
        with app.app_context():
            next_work = create_native_work(another)
        customer, vehicles, work, count = customer_snapshot(self.customer_id)
        self.assertEqual(customer["id"], self.customer_id)
        self.assertEqual(len(vehicles), 1)
        self.assertEqual(count, 2)
        self.assertEqual({w["id"] for w in work}, {self.work_id, next_work})
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_customers").fetchone()[0], 1)

    def test_pagination_and_old_contact_selection_beyond_first_100(self):
        self.login()
        with connect() as db:
            _schema(db)
            for i in range(105):
                db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)", (
                    uuid.uuid4().hex, None, "Additional synthetic %03d" % i,
                    "extra%03d@example.invalid" % i, "", "2026-01-01T00:00:00+00:00"))
        total, first_page = find_customers()
        self.assertEqual(total, 106)
        self.assertEqual(len(first_page), 25)
        result = self.client.post("/hq/customers", data={
            "csrf": "directory-csrf", "q": "", "offset": "25"})
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"Previous page", result.data)
        self.assertIn(b"Next page", result.data)
        selected = self.client.get("/hq/work/new?customer_id=" + self.customer_id)
        self.assertEqual(selected.status_code, 200)
        self.assertIn(('value="' + self.customer_id + '" selected').encode(), selected.data)

    def test_customer_editor_changes_reflected_in_profile_without_recreation(self):
        self.login()
        updated = self.client.post("/hq/customers/" + self.customer_id + "/edit", data={
            "csrf": "directory-csrf", "revision": "0",
            "name": "Updated Synthetic Customer", "email": "directory@example.invalid",
            "phone": "07700 900666", "address": "Fixture only", "postcode": "AA1 1AA"})
        self.assertEqual(updated.status_code, 303)
        page = self.client.get("/hq/customers/" + self.customer_id)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Updated Synthetic Customer", page.data)
        self.assertEqual(customer_snapshot(self.customer_id)[0]["name"], "Updated Synthetic Customer")
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_customers").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
