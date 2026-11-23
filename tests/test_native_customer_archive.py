"""Synthetic owner-only customer archive/restore with durable history and no data loss."""
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
from hq_customer_directory import create_standalone_customer, find_customers, customer_snapshot
from hq_customer_archive import (
    archive_status, set_archived, StaleCustomerArchive, CustomerArchiveRetryConflict,
)
from hq_native_work import create_native_work

ROOT = Path(__file__).resolve().parents[1]


class NativeCustomerArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.tmp.name,
            "HQ_SESSION_SECRET": "archive-fixture-secret",
            "GOOGLE_CLIENT_ID": "synthetic",
            "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()
        with app.app_context():
            self.customer_id = create_standalone_customer({
                "operation_id": uuid.uuid4().hex, "name": "Synthetic Dormant Contact",
                "email": "dormant@example.invalid", "phone": "07700 900678",
                "address": "Synthetic address", "postcode": "ZZ1 1ZZ",
            }, "synthetic-owner")
        self.archive_url = "/hq/customers/" + self.customer_id + "/archive"

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="archive-csrf", google_sub="synthetic-owner")

    def payload(self, archived="1", revision="0", operation_id=None):
        return {
            "csrf": "archive-csrf", "archived": archived, "revision": revision,
            "operation_id": operation_id or uuid.uuid4().hex,
        }

    def test_private_owner_only_and_csrf(self):
        self.assertEqual(self.client.get("/hq/customers/" + self.customer_id).status_code, 302)
        self.assertEqual(self.client.post(self.archive_url, data=self.payload()).status_code, 302)
        self.login()
        self.assertIn(b"Directory status: Active", self.client.get(
            "/hq/customers/" + self.customer_id).data)
        for req in [
            {**self.payload(), "csrf": "bad"},
            {k: v for k, v in self.payload().items() if k != "csrf"},
        ]:
            self.assertEqual(self.client.post(self.archive_url, data=req).status_code, 403)
        self.assertEqual(archive_status(self.customer_id), (False, 0))
        self.assertEqual(self.client.post(
            "/hq/customers/not-valid/archive", data=self.payload()).status_code, 404)

    def test_archive_restore_directory_save_reload_and_history(self):
        self.login()
        self.assertEqual(find_customers("Dormant")[0], 1)
        response = self.client.post(self.archive_url, data=self.payload())
        self.assertEqual(response.status_code, 303)
        self.assertEqual(archive_status(self.customer_id), (True, 1))
        self.assertEqual(find_customers("Dormant")[0], 0)
        self.assertEqual(find_customers("Dormant", archived=True)[0], 1)
        active = self.client.post("/hq/customers", data={
            "csrf": "archive-csrf", "q": "Dormant", "scope": "active", "offset": "0",
        })
        self.assertEqual(active.status_code, 200)
        self.assertNotIn(self.customer_id.encode(), active.data)
        archived = self.client.post("/hq/customers", data={
            "csrf": "archive-csrf", "q": "Dormant", "scope": "archived", "offset": "0",
        })
        self.assertEqual(archived.status_code, 200)
        self.assertIn(self.customer_id.encode(), archived.data)
        page = self.client.get("/hq/customers/" + self.customer_id)
        self.assertIn(b"Directory status: Archived", page.data)
        self.assertIn(b"Restore customer to active directory", page.data)
        self.assertEqual(self.client.post(
            self.archive_url, data=self.payload("0", "1")).status_code, 303)
        self.assertEqual(archive_status(self.customer_id), (False, 2))
        self.assertEqual(find_customers("Dormant")[0], 1)
        with connect() as db:
            events = db.execute("""SELECT revision,previous_archived,next_archived
                FROM hq_customer_archive_events WHERE customer_id=? ORDER BY revision""",
                (self.customer_id,)).fetchall()
            self.assertEqual([tuple(e) for e in events], [(1, 0, 1), (2, 1, 0)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_customers WHERE id=?",
                                        (self.customer_id,)).fetchone()[0], 1)

    def test_idempotent_retry_conflicting_reuse_and_stale_update(self):
        self.login()
        token = uuid.uuid4().hex
        first = self.payload("1", "0", token)
        self.assertEqual(self.client.post(self.archive_url, data=first).status_code, 303)
        self.assertEqual(self.client.post(self.archive_url, data=first).status_code, 303)
        self.assertEqual(self.client.post(self.archive_url, data={
            **first, "archived": "0"}).status_code, 409)
        self.assertEqual(self.client.post(self.archive_url, data=self.payload(
            "0", "0")).status_code, 409)
        self.assertEqual(archive_status(self.customer_id), (True, 1))
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_customer_archive_operations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_customer_archive_events").fetchone()[0], 1)
        with app.app_context():
            with self.assertRaises(StaleCustomerArchive):
                set_archived(self.customer_id, False, 0, uuid.uuid4().hex, "synthetic-owner")
            with self.assertRaises(CustomerArchiveRetryConflict):
                set_archived(self.customer_id, False, 0, token, "synthetic-owner")

    def test_invalid_form_and_archive_scope_are_rejected(self):
        self.login()
        for changes in [{"revision": "-1"}, {"revision": "abc"},
                        {"archived": "true"}, {"operation_id": "bad"}]:
            with self.subTest(changes=changes):
                response = self.client.post(self.archive_url, data={
                    **self.payload(), **changes})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post("/hq/customers", data={
            "csrf": "archive-csrf", "scope": "unknown", "offset": "0",
        }).status_code, 400)
        self.assertEqual(archive_status(self.customer_id), (False, 0))

    def test_linked_job_vehicle_and_contact_details_remain_intact(self):
        with app.app_context():
            work_id = create_native_work({
                "operation_id": uuid.uuid4().hex,
                "customer_id": self.customer_id, "customer_name": "", "email": "", "phone": "",
                "vehicle_id": "", "make": "Ford", "model": "Focus", "model_year": "2020",
                "registration": "ZZ20 TST", "kind": "quote", "service": "Window Tinting",
                "customer_tier": "retail", "notes": "synthetic", "preferred_date": "",
                "preferred_time": "",
            })
            set_archived(self.customer_id, True, 0, uuid.uuid4().hex, "synthetic-owner")
        record, vehicles, work, count = customer_snapshot(self.customer_id)
        self.assertEqual(record["email"], "dormant@example.invalid")
        self.assertEqual(count, 1)
        self.assertEqual(work[0]["id"], work_id)
        self.assertEqual(len(vehicles), 1)
        self.assertEqual(vehicles[0]["registration"], "ZZ20 TST")
        self.login()
        profile = self.client.get("/hq/customers/" + self.customer_id)
        self.assertIn(work_id.encode(), profile.data)
        with connect() as db:
            evidence = db.execute("""SELECT actor_fingerprint FROM hq_customer_archive_events
                WHERE customer_id=?""", (self.customer_id,)).fetchone()[0]
            self.assertNotIn("dormant@example.invalid", evidence)
            self.assertEqual(len(evidence), 64)


if __name__ == "__main__":
    unittest.main()
