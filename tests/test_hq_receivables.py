"""Synthetic owner-only receivables API and persistence regression tests."""
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_core_records import _schema
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_receivables import save


class ReceivablesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        environment = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name, "HQ_SESSION_SECRET": "synthetic",
            "GOOGLE_CLIENT_ID": "test", "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        environment.start()
        self.addCleanup(environment.stop)
        # Persistent production requires /data; synthetic fixture uses a temporary directory.
        storage = patch('hq_receivables.storage_ready', return_value=True)
        storage.start()
        self.addCleanup(storage.stop)
        self.client = app.test_client()
        self.url = "/api/hq/finance/receivables"
        self.customer_a, self.customer_b = uuid.uuid4().hex, uuid.uuid4().hex
        self.vehicle_a = uuid.uuid4().hex
        self.work = uuid.uuid4().hex
        with connect() as db:
            _schema(db)
            for ident, name in [(self.customer_a, "Synthetic Client A"), (self.customer_b, "Synthetic Client B")]:
                db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                           (ident, None, name, "", "", "2026-11-22"))
            db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                       (self.vehicle_a, self.customer_a, "Test", "Car", "", "", "2026-11-22"))
            db.execute("""INSERT INTO hq_work_records(id,source_request_id,customer_id,
                vehicle_id,kind,service,notes,preferred_date,preferred_time,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (self.work, "HQ-" + self.work, self.customer_a, self.vehicle_a,
                 "quote", "Window Tinting", "", "", "", "draft", "2026-11-22"))

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess["hq_sid"] = issue_session(db, "synthetic-owner")
            sess["csrf"] = "test"

    def payload(self, **changes):
        value = dict(id="a" * 32, operation_id=uuid.uuid4().hex, revision=0,
                     customer_id=self.customer_a, work_id=self.work,
                     invoice_ref="TEST-2026-001", issue_date="2026-11-22",
                     due_date="2026-12-10", amount_pence=35000,
                     status="draft", memo="Synthetic invoice only", archived=False)
        value.update(changes)
        return value

    def post(self, value, csrf="test"):
        return self.client.post(self.url, json=value, headers={"X-HQ-CSRF": csrf})

    def test_owner_ui_save_reload_and_archive_audit(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.login()
        page = self.client.get("/hq/finance/receivables")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"name=\"amount_pounds\"", page.data)
        self.assertIn(b"customer_id", page.data)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(b"Customer invoices", self.client.get("/hq/finance/command").data)
        self.assertEqual(self.post(self.payload(), csrf="wrong").status_code, 403)
        initial = self.payload()
        first = self.post(initial)
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertFalse(first.json["receipts_verified"])
        self.assertEqual(self.post(initial).json["revision"], 1)  # retry does not duplicate
        data = self.client.get(self.url).json
        self.assertEqual(data["receivables"][0]["customer_id"], self.customer_a)
        self.assertEqual(data["receivables"][0]["work_id"], self.work)
        self.assertEqual(data["receivables"][0]["amount_pence"], 35000)
        self.assertFalse(data["receipts_verified"])
        self.assertFalse(data["outstanding_verified"])
        self.assertIsNone(data["received_income"])
        changed = self.payload(revision=1, operation_id=uuid.uuid4().hex,
                               status="issued", archived=True, amount_pence=40000)
        self.assertEqual(self.post(changed).status_code, 200)
        self.assertEqual(self.post(self.payload()).status_code, 409)  # stale revision
        restored = self.payload(revision=2, operation_id=uuid.uuid4().hex,
                                status="disputed", archived=False, amount_pence=40000)
        self.assertEqual(self.post(restored).status_code, 200)
        with connect() as db:  # independent SQLite connection verifies durability
            self.assertEqual(db.execute("SELECT revision FROM hq_receivables").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_events").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_operations").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 1)
        data = self.client.get(self.url).json
        self.assertEqual(len(data["history"]), 3)
        self.assertEqual(data["receivables"][0]["status"], "disputed")

    def test_invalid_customers_references_money_dates_and_lifecycle(self):
        self.login()
        self.assertEqual(self.post(self.payload(customer_id=self.customer_b)).status_code, 400)
        self.assertEqual(self.post(self.payload(customer_id=uuid.uuid4().hex, work_id=None)).status_code, 400)
        self.assertEqual(self.post(self.payload(amount_pence=0)).status_code, 400)
        self.assertEqual(self.post(self.payload(amount_pence=True)).status_code, 400)
        self.assertEqual(self.post(self.payload(due_date="2026-11-20")).status_code, 400)
        self.assertEqual(self.post(self.payload(invoice_ref="invalid ref")).status_code, 400)
        self.assertEqual(self.post(self.payload(archived="yes")).status_code, 400)
        self.assertEqual(self.post(self.payload()).status_code, 200)
        self.assertEqual(self.post(self.payload(id="b"*32,work_id=None,invoice_ref="test-2026-001")).status_code, 409)
        self.assertEqual(self.post(self.payload(revision=1,customer_id=self.customer_b,work_id=None)).status_code, 400)
        self.assertEqual(self.post(self.payload(revision=1,status="void")).status_code, 200)
        self.assertEqual(self.post(self.payload(revision=2,status="issued")).status_code, 400)
        self.assertEqual(self.client.get(self.url).json["receivables"][0]["status"], "void")

    def test_conflicting_replay_rejected_and_no_external_calls(self):
        self.login()
        payload = self.payload()
        with patch("requests.post") as external_post, patch("requests.get") as external_get:
            self.assertEqual(self.post(payload).status_code, 200)
            self.assertEqual(self.post({**payload, "amount_pence": 99999}).status_code, 400)
            self.assertEqual(self.client.get(self.url).status_code, 200)
            external_post.assert_not_called()
            external_get.assert_not_called()
        with ThreadPoolExecutor(max_workers=2) as pool:
            replay = self.payload(id="b"*32,work_id=None,invoice_ref="TEST-2026-002")
            revisions = list(pool.map(save, (replay, replay)))
        self.assertEqual(revisions, [1, 1])
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivables").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
