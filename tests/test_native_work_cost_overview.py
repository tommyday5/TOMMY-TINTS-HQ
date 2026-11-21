"""Synthetic same-transaction owner costing and public /book isolation tests."""
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
from hq_core_records import change_work_status, read_work_record, set_draft_financials
from hq_native_work import create_native_work
from hq_work_time import append_work_time, void_work_time
from hq_work_materials import append_work_material, void_work_material
from hq_work_cost_overview import cost_overview_snapshot

ROOT = Path(__file__).resolve().parents[1]


class NativeWorkCostOverviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.directory.name,
            "HQ_SESSION_SECRET": "synthetic-overview-secret",
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
                "customer_name": "Synthetic review customer", "email": "review@example.invalid",
                "phone": "07700 900099", "make": "Hyundai", "model": "i30",
                "model_year": "2022", "registration": "RX22 AAA", "kind": "job",
                "service": "Bodywork & Paint", "customer_tier": "retail",
                "notes": "Synthetic case", "preferred_date": "", "preferred_time": "",
            })
        self.url = "/hq/work/" + self.work_id + "/costs"

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "cost-owner"),
                        csrf="overview-csrf", google_sub="cost-owner")

    def approve(self):
        set_draft_financials(self.work_id, 35000, 5000, expected_revision=0)
        change_work_status(self.work_id, "ready_for_review", expected_revision=1)
        change_work_status(self.work_id, "approved", expected_revision=2)

    def time(self, revision, rate, minutes="120"):
        with app.app_context():
            return append_work_time(self.work_id, {
                "operation_id": uuid.uuid4().hex, "minutes": minutes,
                "stage": "repair", "hourly_cost_pence": rate, "note": "private"},
                revision, "cost-owner")

    def material(self, revision, cost):
        with app.app_context():
            return append_work_material(self.work_id, {
                "operation_id": uuid.uuid4().hex, "material_name": "Octoral primer",
                "quantity": "300", "unit": "ml", "actual_cost_pence": cost,
                "source_note": "private"}, revision, "cost-owner")

    def test_missing_costs_do_not_become_zero_or_imply_profit(self):
        with app.app_context():
            before = cost_overview_snapshot(self.work_id)
            self.assertEqual(before["labour_entry_count"], 0)
            self.assertEqual(before["material_entry_count"], 0)
            self.assertIsNone(before["recorded_labour_material_total_pence"])
            self.assertIsNone(before["partial_known_subtotal_pence"])
            self.assertIsNone(before["approved_sale_price"])
        self.login()
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(b"Not determinable", page.data)
        self.assertNotIn(b"Final profit: ", page.data)
        self.assertIn(b"/time", page.data)
        self.assertIn(b"/materials", page.data)
        self.assertIn(b"Print / save private review as PDF", page.data)
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_known_totals_missing_rate_reversal_and_single_source_revision(self):
        self.approve()
        self.time(3, "", "60")
        self.material(4, "861")
        with app.app_context():
            partial = cost_overview_snapshot(self.work_id)
            self.assertEqual(partial["revision"], 5)
            self.assertEqual(partial["labour_unknown_count"], 1)
            self.assertEqual(partial["partial_known_subtotal_pence"], 861)
            self.assertIsNone(partial["recorded_labour_material_total_pence"])
            self.assertIsNone(partial["total_job_cost_pence"])
        self.login()
        self.assertIn(b"Incomplete", self.client.get(self.url).data)
        with app.app_context():
            with connect() as db:
                entry_id = db.execute(
                    "SELECT entry_id FROM hq_work_time_entries WHERE work_id=?",
                    (self.work_id,)).fetchone()[0]
            void_work_time(self.work_id, entry_id, 5, "cost-owner")
        self.time(6, "1800", "120")
        with app.app_context():
            complete = cost_overview_snapshot(self.work_id)
            self.assertEqual(complete["labour_recorded_total_pence"], 3600)
            self.assertEqual(complete["material_recorded_total_pence"], 861)
            self.assertEqual(complete["recorded_labour_material_total_pence"], 4461)
            self.assertEqual(complete["approved_sale_price"], 35000)
            self.assertIsNone(complete["final_profit_pence"])
            self.assertFalse(complete["payment_verified"])
            self.assertEqual(read_work_record(self.work_id)["deposit_pence"], 5000)
        page = self.client.get(self.url)
        self.assertIn(b"44.61", page.data)
        self.assertIn(b"350.00", page.data)
        self.assertIn(b"prevent double counting", page.data)
        self.assertIn(b"recorded labour and material entries only", page.data)
        self.assertIn(b"Record / reverse materials", page.data)
        self.assertEqual(self.client.get("/hq/work/" + self.work_id + "/edit").status_code, 200)

    def test_explicit_zero_and_voided_entries_without_fabricated_completeness(self):
        self.approve()
        self.time(3, "0")
        material_id = self.material(4, "0")
        with app.app_context():
            snap = cost_overview_snapshot(self.work_id)
            self.assertEqual(snap["recorded_labour_material_total_pence"], 0)
            self.assertIsNone(snap["total_job_cost_pence"])
            void_work_material(self.work_id, material_id, 5, "cost-owner")
            absent = cost_overview_snapshot(self.work_id)
            self.assertEqual(absent["labour_recorded_total_pence"], 0)
            self.assertIsNone(absent["material_recorded_total_pence"])
            self.assertIsNone(absent["recorded_labour_material_total_pence"])
            self.assertEqual(absent["partial_known_subtotal_pence"], 0)

    def test_private_records_and_validation(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.get("/book").status_code, 200)
        self.assertNotIn(b"Synthetic review customer", self.client.get("/book").data)
        self.assertEqual(self.client.get("/hq/work/" + uuid.uuid4().hex + "/costs").status_code, 302)
        self.login()
        self.assertEqual(self.client.get("/hq/work/" + uuid.uuid4().hex + "/costs").status_code, 404)
        self.assertEqual(self.client.get("/hq/work/not-valid/costs").status_code, 404)
        self.assertEqual(self.client.get(self.url).status_code, 200)
