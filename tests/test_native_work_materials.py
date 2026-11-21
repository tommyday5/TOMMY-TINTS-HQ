"""Synthetic owner UI and persistence acceptance for native job material usage."""
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import (
    change_work_status, read_work_record, set_draft_financials, StaleWorkRecordError,
)
from hq_job_sync import status as sheet_sync_status
from hq_native_work import create_native_work
from hq_work_materials import append_work_material, material_ledger_snapshot, void_work_material

ROOT = Path(__file__).resolve().parents[1]


class NativeMaterialLedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.directory.name,
            "HQ_SESSION_SECRET": "synthetic-material-ledger-secret",
            "GOOGLE_CLIENT_ID": "synthetic",
            "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()
        with app.app_context():
            self.work_id = create_native_work({
                "operation_id": uuid.uuid4().hex,
                "customer_id": "", "vehicle_id": "",
                "customer_name": "Synthetic material customer",
                "email": "material@example.invalid", "phone": "07700 900005",
                "make": "Hyundai", "model": "i30", "model_year": "2022",
                "registration": "MT22 ABC", "kind": "job",
                "service": "Bodywork & Paint", "customer_tier": "trade",
                "notes": "Synthetic test only", "preferred_date": "", "preferred_time": "",
            })
        self.url = "/hq/work/" + self.work_id + "/materials"

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "material-owner"),
                        csrf="material-csrf", google_sub="material-owner")

    def approve(self):
        set_draft_financials(self.work_id, 35000, 5000, expected_revision=0)
        change_work_status(self.work_id, "ready_for_review", expected_revision=1)
        change_work_status(self.work_id, "approved", expected_revision=2)

    def form(self, revision="3", operation=None, **overrides):
        fields = {
            "csrf": "material-csrf", "revision": revision,
            "operation_id": operation or uuid.uuid4().hex,
            "material_name": "Octoral synthetic paint",
            "quantity": "300", "unit": "ml",
            "actual_cost_pence": "", "source_note": "Synthetic mixing note",
        }
        fields.update(overrides)
        return fields

    def test_owner_can_create_reload_replay_and_reverse_with_unknown_costs(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.login()
        self.approve()
        initial = self.client.get(self.url)
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.headers["Cache-Control"], "no-store")
        self.assertIn(b"Save material usage", initial.data)
        self.assertIn(b'name="revision" value="3"', initial.data)

        unknown_form = self.form()
        self.assertEqual(self.client.post(self.url, data=unknown_form).status_code, 303)
        first = material_ledger_snapshot(self.work_id)
        self.assertEqual(first["entry_count"], 1)
        self.assertEqual(first["unknown_cost_count"], 1)
        self.assertIsNone(first["actual_material_cost_pence"])
        self.assertEqual(first["revision"], 4)
        self.assertIn(b"Octoral synthetic paint", self.client.get(self.url).data)
        # The same operation is replay-safe, including with a now-stale form revision.
        self.assertEqual(self.client.post(self.url, data=unknown_form).status_code, 303)
        self.assertEqual(material_ledger_snapshot(self.work_id)["revision"], 4)

        actual_form = self.form(revision="4", material_name="Primer",
                                quantity="0.750", unit="g", actual_cost_pence="861")
        self.assertEqual(self.client.post(self.url, data=actual_form).status_code, 303)
        partial = material_ledger_snapshot(self.work_id)
        self.assertEqual(partial["entry_count"], 2)
        self.assertEqual(partial["known_cost_subtotal_pence"], 861)
        self.assertIsNone(partial["actual_material_cost_pence"])
        self.assertIn(b"Partial known-cost subtotal", self.client.get(self.url).data)
        self.assertEqual(read_work_record(self.work_id)["price_pence"], 35000)
        self.assertEqual(read_work_record(self.work_id)["deposit_pence"], 5000)
        self.assertEqual(sheet_sync_status(self.work_id)["state"], "not_linked")
        self.assertFalse(partial["payment_verified"])

        to_void = next(x for x in partial["entries"] if x["actual_cost_pence"] is None)
        self.assertEqual(self.client.post(self.url + "/" + to_void["entry_id"] + "/void",
            data={"csrf": "material-csrf", "revision": "5"}).status_code, 303)
        current = material_ledger_snapshot(self.work_id)
        self.assertEqual(current["entry_count"], 1)
        self.assertEqual(current["actual_material_cost_pence"], 861)
        self.assertEqual(current["revision"], 6)
        self.assertTrue(any(x["voided_at"] for x in current["entries"]))
        with connect() as db:
            actions = [x["action"] for x in db.execute(
                "SELECT action FROM hq_work_audit WHERE work_id=? ORDER BY revision",
                (self.work_id,))]
            self.assertEqual(actions[-3:],
                ["material_usage_added", "material_usage_added", "material_usage_voided"])
            audit = " ".join(x["after_value"] for x in db.execute(
                "SELECT after_value FROM hq_work_audit WHERE work_id=?", (self.work_id,)))
            self.assertNotIn("Octoral synthetic paint", audit)
            self.assertNotIn("Synthetic mixing note", audit)
        # Replaying the reversal changes nothing.
        self.assertEqual(self.client.post(self.url + "/" + to_void["entry_id"] + "/void",
            data={"csrf": "material-csrf", "revision": "5"}).status_code, 303)
        self.assertEqual(material_ledger_snapshot(self.work_id)["revision"], 6)

    def test_owner_only_csrf_validation_and_closed_job_lock(self):
        other = app.test_client()
        self.assertEqual(other.get(self.url).status_code, 302)
        self.assertEqual(other.post(self.url, data=self.form()).status_code, 302)
        self.login()
        self.approve()
        self.assertEqual(self.client.post(self.url, data=self.form(csrf="wrong")).status_code, 403)
        for invalid in (
            {"operation_id": "bad"}, {"material_name": ""}, {"material_name": "x" * 101},
            {"quantity": "0"}, {"quantity": "0.0001"}, {"quantity": "1000001"},
            {"quantity": "-1"}, {"quantity": "NaN"}, {"unit": "barrels"},
            {"unit": "each", "quantity": "1.5"}, {"actual_cost_pence": "-1"},
            {"actual_cost_pence": "1.50"}, {"actual_cost_pence": "100000001"},
            {"source_note": "x" * 161},
        ):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.client.post(self.url, data=self.form(**invalid)).status_code, 400)
        self.assertEqual(material_ledger_snapshot(self.work_id)["entry_count"], 0)
        self.assertEqual(self.client.get("/hq/work/" + uuid.uuid4().hex + "/materials").status_code, 404)
        self.assertEqual(self.client.post(self.url, data=self.form(revision="invalid")).status_code, 400)
        self.assertEqual(self.client.post(self.url, data=self.form(actual_cost_pence="0")).status_code, 303)
        zero = material_ledger_snapshot(self.work_id)
        self.assertEqual(zero["actual_material_cost_pence"], 0)  # Owner explicitly entered zero.
        change_work_status(self.work_id, "in_progress", expected_revision=4)
        change_work_status(self.work_id, "completed", expected_revision=5)
        self.assertIn(b"Closed or draft records", self.client.get(self.url).data)
        self.assertNotIn(b"Save material usage", self.client.get(self.url).data)
        self.assertEqual(self.client.post(self.url, data=self.form(revision="6")).status_code, 400)

    def test_stale_conflicting_replays_and_revision_is_shared_with_work_time(self):
        self.login()
        self.approve()
        first = self.form()
        self.assertEqual(self.client.post(self.url, data=first).status_code, 303)
        self.assertEqual(self.client.post(self.url, data=self.form()).status_code, 409)
        altered = self.form(revision="4", operation=first["operation_id"], quantity="200")
        self.assertEqual(self.client.post(self.url, data=altered).status_code, 409)
        self.assertEqual(material_ledger_snapshot(self.work_id)["entry_count"], 1)
        from hq_work_time import append_work_time
        with app.app_context():
            append_work_time(self.work_id, {
                "operation_id": uuid.uuid4().hex, "minutes": "30", "stage": "repair",
                "hourly_cost_pence": "", "note": "",
            }, 4, "material-owner")
        self.assertEqual(self.client.post(self.url,
            data=self.form(revision="4")).status_code, 409)
        self.assertEqual(material_ledger_snapshot(self.work_id)["revision"], 5)

    def test_racing_material_posts_one_revision_wins(self):
        self.approve()
        def add(_):
            with app.app_context():
                try:
                    append_work_material(self.work_id, self.form(), 3, "material-owner")
                    return "saved"
                except StaleWorkRecordError:
                    return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(add, range(2)))
        self.assertEqual(sorted(outcomes), ["conflict", "saved"])
        self.assertEqual(material_ledger_snapshot(self.work_id)["entry_count"], 1)

    def test_public_enquiry_to_same_hq_job_private_materials(self):
        with patch.dict(os.environ, {"HQ_PUBLIC_REQUESTS_ENABLED": "true"}):
            submitted = self.client.post("/api/public/customer-request", json={
                "kind": "quote", "name": "Synthetic public enquiry",
                "email": "visitor-material@example.invalid", "phone": "07700 900999",
                "service": "Window Tinting", "make": "Ford", "model": "Fiesta",
                "year": "2020", "registration": "EF20 ABC",
                "notes": "Synthetic quote", "privacy_acknowledged": True,
            })
        self.assertEqual(submitted.status_code, 201, submitted.json)
        self.assertFalse(submitted.json["confirmed_booking"])
        self.login()
        reference = submitted.json["reference"]
        headers = {"X-HQ-CSRF": "material-csrf"}
        self.assertEqual(self.client.post(
            "/api/hq/customer-requests/" + reference + "/draft",
            json={"draft_type": "quote"}, headers=headers).status_code, 201)
        converted = self.client.post("/api/hq/customer-requests/" + reference + "/work-record",
                                     json={}, headers=headers)
        self.assertEqual(converted.status_code, 201, converted.json)
        linked = converted.json["id"]
        set_draft_financials(linked, 15000, 0, expected_revision=0)
        change_work_status(linked, "ready_for_review", expected_revision=1)
        change_work_status(linked, "approved", expected_revision=2)
        self.assertEqual(self.client.post("/hq/work/" + linked + "/materials",
            data=self.form(actual_cost_pence="250", unit="ml", quantity="120")).status_code, 303)
        self.assertEqual(material_ledger_snapshot(linked)["actual_material_cost_pence"], 250)
        self.assertEqual(read_work_record(linked)["source_request_id"], reference)
        self.assertEqual(read_work_record(linked)["kind"], "job")
        self.assertIn(b"Record material usage",
                      self.client.get("/hq/work/" + linked + "/edit").data)
        self.assertEqual(app.test_client().get("/hq/work/" + linked + "/materials").status_code, 302)
        self.assertEqual(sheet_sync_status(linked)["state"], "not_linked")


if __name__ == "__main__":
    unittest.main()
