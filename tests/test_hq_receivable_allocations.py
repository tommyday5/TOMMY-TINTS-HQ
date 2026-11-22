"""Synthetic, owner-only invoice allocation tests; no external settlement."""
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
from hq_core_records import _schema
from hq_receivables import save as save_invoice
from hq_receivable_payments import save as save_allocation


class AllocationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name,
            "HQ_SESSION_SECRET": "synthetic",
            "GOOGLE_CLIENT_ID": "test",
            "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        for target in ("hq_receivables.storage_ready", "hq_receivable_payments.storage_ready"):
            mocked = patch(target, return_value=True)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.client = app.test_client()
        self.customer = uuid.uuid4().hex
        self.invoice_id = uuid.uuid4().hex
        with connect() as db:
            _schema(db)
            db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                       (self.customer, None, "Synthetic Customer", "", "", "2026-11-22"))
        self.invoice = dict(id=self.invoice_id, operation_id=uuid.uuid4().hex,
                            revision=0, customer_id=self.customer, work_id=None,
                            invoice_ref="TEST-ALLOCATION-001", issue_date="2026-11-22",
                            due_date="2026-12-10", amount_pence=35000,
                            status="issued", memo="", archived=False)
        self.assertEqual(save_invoice(self.invoice), 1)

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess["hq_sid"] = issue_session(db, "synthetic-owner")
            sess["csrf"] = "test"

    def payload(self, **updates):
        data = dict(id=uuid.uuid4().hex, invoice_id=self.invoice_id,
                    operation_id=uuid.uuid4().hex, revision=0,
                    amount_pence=20000, received_on="2026-11-22",
                    method="bank_reported", evidence_ref="SIMULATED-REF",
                    memo="Unverified evidence, synthetic", status="reported")
        data.update(updates)
        return data

    def post(self, payload, csrf="test"):
        return self.client.post("/api/hq/finance/receipt-allocations", json=payload,
                                headers={"X-HQ-CSRF": csrf})

    def test_owner_create_edit_reverse_history_and_isolation(self):
        self.assertEqual(self.client.get("/hq/finance/receipt-allocations").status_code, 302)
        self.assertEqual(self.client.get("/api/hq/finance/receipt-allocations").status_code, 302)
        self.login()
        page = self.client.get("/hq/finance/receipt-allocations")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"not verified bank receipts", page.data)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(b"receipt-allocations", self.client.get("/hq/finance/receivables").data)
        first = self.payload()
        self.assertEqual(self.post(first, csrf="incorrect").status_code, 403)
        with patch("requests.post") as external_post, patch("requests.get") as external_get:
            response = self.post(first)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertFalse(response.json["bank_verified"])
            self.assertEqual(self.post(first).json["revision"], 1)
            external_post.assert_not_called()
            external_get.assert_not_called()
        initial = self.client.get("/api/hq/finance/receipt-allocations").json
        self.assertEqual(initial["allocations"][0]["invoice_id"], self.invoice_id)
        self.assertEqual(initial["invoices"][0]["owner_reported_allocated_pence"], 20000)
        self.assertEqual(initial["invoices"][0]["unallocated_owner_reported_pence"], 15000)
        self.assertFalse(initial["receipts_verified"])
        self.assertFalse(initial["outstanding_verified"])
        self.assertIsNone(initial["collected_income"])
        revised = {**first, "operation_id": uuid.uuid4().hex, "revision": 1,
                   "memo": "Reviewed manual reference"}
        self.assertEqual(self.post(revised).json["revision"], 2)
        # Original token is a legitimate idempotent replay; a *new* token is stale.
        self.assertEqual(self.post(first).json["revision"], 1)
        self.assertEqual(self.post({**first, "operation_id": uuid.uuid4().hex}).status_code, 409)
        reversed_entry = {**revised, "operation_id": uuid.uuid4().hex,
                          "revision": 2, "status": "reversed"}
        self.assertEqual(self.post(reversed_entry).json["revision"], 3)
        self.assertEqual(self.post({**reversed_entry, "operation_id": uuid.uuid4().hex,
                                    "revision": 3}).status_code, 400)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocation_events").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocation_operations").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivables").fetchone()[0], 1)
        after = self.client.get("/api/hq/finance/receipt-allocations").json
        self.assertEqual(after["invoices"][0]["owner_reported_allocated_pence"], 0)

    def test_amount_bounds_invoice_state_evidence_immutability(self):
        self.login()
        self.assertEqual(self.post(self.payload(amount_pence=True)).status_code, 400)
        self.assertEqual(self.post(self.payload(amount_pence=0)).status_code, 400)
        self.assertEqual(self.post(self.payload(invoice_id=uuid.uuid4().hex)).status_code, 400)
        self.assertEqual(self.post(self.payload(received_on="2026-02-30")).status_code, 400)
        self.assertEqual(self.post(self.payload(method="bank_verified")).status_code, 400)
        self.assertEqual(self.post(self.payload(evidence_ref="X"*121)).status_code, 400)
        self.assertEqual(self.post(self.payload(status="reversed")).status_code, 400)
        first = self.payload()
        self.assertEqual(self.post(first).status_code, 200)
        self.assertEqual(self.post(self.payload(amount_pence=15001)).status_code, 400)
        self.assertEqual(self.post({**first, "operation_id": uuid.uuid4().hex,
                                    "revision": 1, "amount_pence": 21000}).status_code, 400)
        self.assertEqual(self.post({**first, "amount_pence": 100}).status_code, 400)  # replay conflict
        # A report cannot silently be erased by reducing or voiding the invoice.
        changed = {**self.invoice, "operation_id": uuid.uuid4().hex, "revision": 1,
                   "amount_pence": 15000}
        with self.assertRaises(ValueError):
            save_invoice(changed)
        changed["status"] = "void"
        changed["amount_pence"] = 35000
        with self.assertRaises(ValueError):
            save_invoice(changed)

    def test_concurrent_retry_is_atomic_and_draft_is_not_payable(self):
        payload = self.payload()
        with ThreadPoolExecutor(max_workers=2) as executor:
            revisions = list(executor.map(save_allocation, (payload, payload)))
        self.assertEqual(revisions, [1, 1])
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocations").fetchone()[0], 1)
        # The source invoice has to have been reported issued/disputed first.
        pending_id = uuid.uuid4().hex
        pending = {**self.invoice, "id": pending_id, "operation_id": uuid.uuid4().hex,
                   "invoice_ref": "SYNTH-DRAFT", "status": "draft"}
        save_invoice(pending)
        with self.assertRaises(ValueError):
            save_allocation(self.payload(invoice_id=pending_id))


if __name__ == "__main__":
    unittest.main()
