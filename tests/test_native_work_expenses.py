"""Synthetic native HQ job expenses, approvals, revisions and /book isolation."""
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import StaleWorkRecordError, change_work_status, read_work_record, set_draft_financials
from hq_native_work import create_native_work
from hq_work_expenses import append_work_expense, void_work_expense, expense_ledger_snapshot
from hq_work_cost_overview import cost_overview_snapshot

ROOT = Path(__file__).resolve().parents[1]


class NativeWorkExpenseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.tmp.name,
            "HQ_SESSION_SECRET": "synthetic-expense-test-secret",
            "GOOGLE_CLIENT_ID": "synthetic",
            "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()
        with app.app_context():
            self.work_id = create_native_work({
                "operation_id": uuid.uuid4().hex, "customer_id": "", "vehicle_id": "",
                "customer_name": "Private expense customer", "email": "expense@example.invalid",
                "phone": "07700 900333", "make": "Ford", "model": "Focus",
                "model_year": "2018", "registration": "AX18 XYZ", "kind": "job",
                "service": "Bodywork & Paint", "customer_tier": "retail",
                "notes": "Synthetic private work", "preferred_date": "", "preferred_time": "",
            })
        self.url = "/hq/work/" + self.work_id + "/expenses"

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "expense-owner"),
                        csrf="expense-csrf", google_sub="expense-owner")

    def approve(self):
        set_draft_financials(self.work_id, 35000, 5000, expected_revision=0)
        change_work_status(self.work_id, "ready_for_review", expected_revision=1)
        change_work_status(self.work_id, "approved", expected_revision=2)

    def fields(self, amount="825", operation=None):
        return {
            "operation_id": operation or uuid.uuid4().hex,
            "expense_type": "overhead",
            "description": "Allocated booth electricity",
            "allocated_cost_pence": amount,
            "source_note": "Synthetic allocation policy",
        }

    def test_owner_form_save_reload_cost_and_correction(self):
        self.approve()
        self.login()
        self.assertIn(b"Record actual job expense", self.client.get(self.url).data)
        form = dict(self.fields("825"), csrf="expense-csrf", revision="3")
        response = self.client.post(self.url, data=form)
        self.assertEqual(response.status_code, 303)
        page = self.client.get(self.url)
        self.assertIn(b"Allocated booth electricity", page.data)
        self.assertIn(b"8.25", page.data)
        with app.app_context():
            current = expense_ledger_snapshot(self.work_id)
            self.assertEqual(current["revision"], 4)
            self.assertEqual(current["actual_expenses_pence"], 825)
            cost = cost_overview_snapshot(self.work_id)
            self.assertEqual(cost["expense_known_pence"], 825)
            self.assertEqual(cost["partial_known_three_ledger_pence"], 825)
            self.assertIsNone(cost["recorded_three_ledger_total_pence"])
            self.assertIsNone(cost["total_job_cost_pence"])
            self.assertIsNone(cost["final_profit_pence"])
            self.assertFalse(cost["payment_verified"])
            self.assertEqual(read_work_record(self.work_id)["deposit_pence"], 5000)
            entry_id = current["entries"][0]["entry_id"]
        self.assertIn(b"Other expenses", self.client.get("/hq/work/" + self.work_id + "/costs").data)
        reversed_response = self.client.post(self.url + "/" + entry_id + "/void",
                                             data={"csrf": "expense-csrf", "revision": "4"})
        self.assertEqual(reversed_response.status_code, 303)
        with app.app_context():
            after = expense_ledger_snapshot(self.work_id)
            self.assertEqual(after["entry_count"], 0)
            self.assertIsNone(after["actual_expenses_pence"])
            self.assertIsNotNone(after["entries"][0]["voided_at"])
            self.assertEqual(after["revision"], 5)
            self.assertIsNone(cost_overview_snapshot(self.work_id)["partial_known_three_ledger_pence"])
        # Exact replay of a reversal cannot increment the work revision.
        self.assertEqual(self.client.post(self.url + "/" + entry_id + "/void",
                                         data={"csrf": "expense-csrf", "revision": "4"}).status_code, 303)
        with app.app_context():
            self.assertEqual(expense_ledger_snapshot(self.work_id)["revision"], 5)

    def test_missing_zero_and_strict_idempotency(self):
        self.approve()
        with app.app_context():
            unknown = self.fields("")
            entry_id = append_work_expense(self.work_id, unknown, 3, "expense-owner")
            self.assertEqual(append_work_expense(self.work_id, unknown, 3, "expense-owner"), entry_id)
            snap = expense_ledger_snapshot(self.work_id)
            self.assertEqual(snap["revision"], 4)
            self.assertEqual(snap["unknown_cost_count"], 1)
            self.assertIsNone(snap["actual_expenses_pence"])
            self.assertIsNone(cost_overview_snapshot(self.work_id)["partial_known_three_ledger_pence"])
            with self.assertRaises(StaleWorkRecordError):
                append_work_expense(self.work_id, self.fields("0", unknown["operation_id"]), 4, "expense-owner")
            with self.assertRaises(StaleWorkRecordError):
                append_work_expense(self.work_id, self.fields("25"), 3, "expense-owner")
            self.assertEqual(expense_ledger_snapshot(self.work_id)["entry_count"], 1)
            void_work_expense(self.work_id, entry_id, 4, "expense-owner")
            append_work_expense(self.work_id, self.fields("0"), 5, "expense-owner")
            snap = expense_ledger_snapshot(self.work_id)
            self.assertEqual(snap["actual_expenses_pence"], 0)
            self.assertEqual(snap["known_cost_subtotal_pence"], 0)
            self.assertEqual(cost_overview_snapshot(self.work_id)["partial_known_three_ledger_pence"], 0)

    def test_permissions_validation_and_locking(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.post(self.url, data={}).status_code, 302)
        self.login()
        self.assertEqual(self.client.get("/hq/work/not-a-uuid/expenses").status_code, 404)
        self.assertEqual(self.client.get("/hq/work/" + uuid.uuid4().hex + "/expenses").status_code, 404)
        self.assertEqual(self.client.post(self.url, data=self.fields()).status_code, 403)
        self.assertEqual(self.client.post(self.url, data=dict(self.fields(), csrf="expense-csrf", revision="0")).status_code, 400)
        self.approve()
        for malformed in [
            dict(self.fields(), expense_type="paid"),
            dict(self.fields(), allocated_cost_pence="-100"),
            dict(self.fields(), allocated_cost_pence="1.50"),
            dict(self.fields(), allocated_cost_pence=""),
            dict(self.fields(), description=""),
        ]:
            if malformed["allocated_cost_pence"] == "" and malformed["description"]:
                # Blank is deliberately valid (unknown cost).
                continue
            response = self.client.post(self.url, data=dict(malformed, csrf="expense-csrf", revision="3"))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post(self.url, data=dict(self.fields(), csrf="expense-csrf", revision="3")).status_code, 303)
        change_work_status(self.work_id, "in_progress", expected_revision=4)
        change_work_status(self.work_id, "completed", expected_revision=5)
        with app.app_context():
            self.assertFalse(expense_ledger_snapshot(self.work_id)["can_edit_expenses"])
        self.assertEqual(self.client.post(self.url, data=dict(self.fields(), csrf="expense-csrf", revision="6")).status_code, 400)

    def test_public_enquiry_converts_to_same_private_work_id_without_leak(self):
        with patch.dict(os.environ, {"HQ_PUBLIC_REQUESTS_ENABLED": "true"}):
            posted = self.client.post("/api/public/customer-request", json={
                "kind": "quote", "name": "Public synthetic expense",
                "email": "public-cost@example.invalid", "phone": "07700 900022",
                "service": "Window Tinting", "make": "Seat", "model": "Leon",
                "year": "2020", "registration": "AP20 BBB",
                "notes": "Synthetic quote", "privacy_acknowledged": True,
            })
        self.assertEqual(posted.status_code, 201, posted.json)
        self.assertFalse(posted.json["confirmed_booking"])
        self.login()
        ref = posted.json["reference"]
        headers = {"X-HQ-CSRF": "expense-csrf"}
        draft = self.client.post("/api/hq/customer-requests/" + ref + "/draft",
                                 json={"draft_type": "quote"}, headers=headers)
        self.assertEqual(draft.status_code, 201, draft.json)
        converted = self.client.post("/api/hq/customer-requests/" + ref + "/work-record",
                                     json={}, headers=headers)
        self.assertEqual(converted.status_code, 201, converted.json)
        work = converted.json["id"]
        set_draft_financials(work, 13000, 0, expected_revision=0)
        change_work_status(work, "ready_for_review", expected_revision=1)
        change_work_status(work, "approved", expected_revision=2)
        with app.app_context():
            append_work_expense(work, self.fields("250"), 3, "expense-owner")
            snap = cost_overview_snapshot(work)
            self.assertEqual(snap["source_request_id"], ref)
            self.assertEqual(snap["expense_known_pence"], 250)
            self.assertIsNone(snap["final_profit_pence"])
        self.assertEqual(self.client.get("/hq/work/" + work + "/expenses").status_code, 200)
        visitor = app.test_client()
        self.assertEqual(visitor.get("/hq/work/" + work + "/expenses").status_code, 302)
        self.assertNotIn(b"Allocated booth", visitor.get("/book").data)
