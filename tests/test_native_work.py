"""Synthetic end-to-end tests for private, durable HQ native work."""
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import (change_work_status, list_work_records,
                             read_work_record, set_draft_financials)
from hq_job_sync import status as sheet_sync_status
from hq_native_work import (StaleWorkRecordError, create_native_work,
                            native_work_record, update_native_work)


class NativeWorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.tmp.name, "HQ_SESSION_SECRET": "native-test-secret",
            "GOOGLE_CLIENT_ID": "synthetic", "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()
        self.payload = {
            "operation_id": uuid.uuid4().hex, "customer_id": "",
            "vehicle_id": "", "customer_name": "Synthetic Customer",
            "email": "owner-test@example.invalid", "phone": "07700 900001",
            "make": "Hyundai", "model": "i30", "model_year": "2022",
            "registration": "AB22 AAA", "kind": "quote",
            "service": "Bodywork & Paint", "customer_tier": "retail",
            "notes": "Synthetic bonnet repaint", "preferred_date": "",
            "preferred_time": "",
        }

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "test-owner"), csrf="test-csrf",
                        google_sub="test-owner")

    def post(self, payload=None):
        return self.client.post("/hq/work/new", data={
            **(self.payload if payload is None else payload), "csrf": "test-csrf"})

    def test_owner_can_create_save_reload_and_edit_real_native_draft_schema(self):
        self.assertEqual(self.client.get("/hq/work/new").status_code, 302)
        self.assertEqual(self.post().status_code, 302)
        self.login()
        page = self.client.get("/hq/work/new")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(b"Save new HQ record", page.data)
        self.assertEqual(self.client.post("/hq/work/new", data=self.payload).status_code, 403)

        created = self.post()
        self.assertEqual(created.status_code, 303, created.get_data(as_text=True))
        self.assertIn("/hq/work/", created.headers["Location"])
        work_id = created.headers["Location"].split("/")[3]
        self.assertEqual(len(work_id), 32)
        saved = self.client.get(created.headers["Location"])
        self.assertEqual(saved.status_code, 200)
        self.assertIn(b"record created and saved", saved.data)
        self.assertIn(b"Synthetic bonnet repaint", saved.data)
        self.assertEqual(saved.headers["Cache-Control"], "no-store")
        row = read_work_record(work_id)
        self.assertEqual(row["source_request_id"], "HQ-" + work_id)
        self.assertEqual((row["name"], row["registration"], row["kind"]),
                         ("Synthetic Customer", "AB22 AAA", "quote"))
        self.assertEqual(native_work_record(work_id)["customer_tier"], "retail")
        self.assertEqual(sheet_sync_status(work_id)["state"], "not_linked")
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_native_work_operations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_native_work_events").fetchone()[0], 1)

        # Save and re-open using a separate HTTP request and SQLite connection.
        edited = self.client.post("/hq/work/" + work_id + "/edit", data={
            "csrf": "test-csrf", "revision": "0", "service": "Window Tinting",
            "customer_tier": "trade", "notes": "Synthetic rear tint",
            "preferred_date": "2026-12-01", "preferred_time": "10:30"})
        self.assertEqual(edited.status_code, 303)
        updated = native_work_record(work_id)
        self.assertEqual((updated["service"], updated["customer_tier"], updated["revision"]),
                         ("Window Tinting", "trade", 1))
        self.assertIn(b"Synthetic rear tint", self.client.get(edited.headers["Location"]).data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_audit").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_native_work_events").fetchone()[0], 2)
            audit = db.execute("SELECT before_value,after_value FROM hq_work_audit").fetchone()
            self.assertNotIn("Synthetic", audit["before_value"] + audit["after_value"])
        self.assertEqual(sheet_sync_status(work_id)["state"], "not_linked")

        stale = self.client.post("/hq/work/" + work_id + "/edit", data={
            "csrf": "test-csrf", "revision": "0", "service": "Wheel Refurbishment",
            "customer_tier": "retail", "notes": "Old form",
            "preferred_date": "", "preferred_time": ""})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(native_work_record(work_id)["revision"], 1)
        replay = self.client.post("/hq/work/" + work_id + "/edit", data={
            "csrf": "test-csrf", "revision": "0", "service": "Window Tinting",
            "customer_tier": "trade", "notes": "Synthetic rear tint",
            "preferred_date": "2026-12-01", "preferred_time": "10:30"})
        self.assertEqual(replay.status_code, 303)
        self.assertEqual(native_work_record(work_id)["revision"], 1)

        set_draft_financials(work_id, 25000, 5000, expected_revision=1)
        change_work_status(work_id, "ready_for_review", expected_revision=2)
        change_work_status(work_id, "approved", expected_revision=3)
        self.assertEqual(read_work_record(work_id)["kind"], "job")
        self.assertEqual(native_work_record(work_id)["customer_tier"], "trade")
        self.assertEqual(self.client.post("/hq/work/" + work_id + "/edit", data={
            "csrf": "test-csrf", "revision": "4", "service": "Vehicle Wrapping",
            "customer_tier": "trade", "notes": "Should fail",
            "preferred_date": "", "preferred_time": ""}).status_code, 400)
        self.assertEqual(sheet_sync_status(work_id)["state"], "not_linked")

    def test_idempotent_creation_duplicate_detection_and_no_partial_writes(self):
        self.login()
        first = self.post()
        self.assertEqual(first.status_code, 303)
        self.assertEqual(self.post().headers["Location"], first.headers["Location"])
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_customers").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_vehicles").fetchone()[0], 1)
        altered = self.post({**self.payload, "notes": "Different job with reused operation ID"})
        self.assertEqual(altered.status_code, 409)
        duplicate = self.post({**self.payload, "operation_id": uuid.uuid4().hex,
                               "email": "OWNER-TEST@example.invalid"})
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn(b"already in HQ", duplicate.data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_customers").fetchone()[0], 1)

    def test_existing_customer_vehicle_reuse_and_cross_customer_vehicle_protection(self):
        self.login()
        first = self.post()
        self.assertEqual(first.status_code, 303)
        row = list_work_records()[0]
        chosen = {**self.payload, "operation_id": uuid.uuid4().hex,
                  "customer_id": row["customer_id"], "vehicle_id": row["vehicle_id"],
                  "customer_name": "", "email": "", "phone": "",
                  "make": "", "model": "", "model_year": "", "registration": "",
                  "kind": "job", "customer_tier": "maintenance"}
        self.assertEqual(self.post(chosen).status_code, 303)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_customers").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_vehicles").fetchone()[0], 1)
        self.assertEqual(len(list_work_records()), 2)
        self.assertEqual(list_work_records()[0]["customer_id"], row["customer_id"])
        self.assertEqual(self.post({**chosen, "operation_id": uuid.uuid4().hex,
                    "customer_id": uuid.uuid4().hex}).status_code, 400)
        other = self.post({**chosen, "operation_id": uuid.uuid4().hex,
                    "vehicle_id": "", "make": "BMW", "model": "3 Series"})
        self.assertEqual(other.status_code, 303)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_vehicles").fetchone()[0], 2)

    def test_strict_form_validation_and_no_public_native_access(self):
        self.login()
        bad = [
            {**self.payload, "kind": "confirmed"},
            {**self.payload, "customer_tier": "owner-only-profit"},
            {**self.payload, "operation_id": "bad"},
            {**self.payload, "preferred_date": "2026-02-30"},
            {**self.payload, "preferred_time": "25:95"},
            {**self.payload, "email": "not-an-email"},
            {**self.payload, "model_year": "1890"},
            {**self.payload, "service": "Unknown"},
        ]
        for p in bad:
            with self.subTest(p=p):
                self.assertEqual(self.post(p).status_code, 400)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)
        self.assertEqual(self.client.get("/api/public/customer-request/status").status_code, 200)
        self.assertEqual(self.client.get("/hq/work/" + uuid.uuid4().hex + "/edit").status_code, 404)

    def test_concurrent_replay_creates_only_one_record(self):
        # Each synthetic worker has an app context but shares the same SQLite file.
        def create():
            with app.app_context():
                return create_native_work(self.payload)
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(ids[0], ids[1])
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
