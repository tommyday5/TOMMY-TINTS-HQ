"""Vehicle editing uses canonical HQ vehicle IDs and no provider writes."""
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import change_work_status, read_work_record, set_draft_financials
from hq_job_sync import status as sheets_status
from hq_native_work import (create_native_work, native_work_record,
                            native_vehicle_record, update_native_vehicle,
                            StaleWorkRecordError)


class NativeVehicleEditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.directory.name,
            "HQ_SESSION_SECRET": "vehicle-editor-fixture-secret",
            "GOOGLE_CLIENT_ID": "synthetic",
            "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()
        self.work = {
            "operation_id": uuid.uuid4().hex, "customer_id": "",
            "vehicle_id": "", "customer_name": "Synthetic owner contact",
            "email": "vehicle@example.invalid", "phone": "07700 900555",
            "make": "Hyundai", "model": "i30", "model_year": "2022",
            "registration": "AB22 AAA", "kind": "quote",
            "service": "Bodywork & Paint", "customer_tier": "retail",
            "notes": "Only a test", "preferred_date": "", "preferred_time": "",
        }
        with app.app_context():
            self.work_id = create_native_work(self.work)
        self.vehicle_id = native_work_record(self.work_id)["vehicle_id"]
        self.path = "/hq/vehicles/" + self.vehicle_id + "/edit"
        self.form = {"csrf": "vehicle-csrf", "revision": "0",
                     "make": "Hyundai", "model": "i20",
                     "model_year": "2023", "registration": "CD23 BBB"}

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "test-owner"),
                        csrf="vehicle-csrf", google_sub="test-owner")

    def test_owner_form_save_reload_history_and_unlinked_sheet(self):
        self.assertEqual(self.client.get(self.path).status_code, 302)
        self.login()
        page = self.client.get(self.path)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(b"Save vehicle details", page.data)
        self.assertIn(b'name="revision" value="0"', page.data)
        self.assertEqual(self.client.post(self.path, data=self.form).status_code, 303)
        saved = self.client.get(self.path + "?saved=1")
        self.assertIn(b"Vehicle details saved", saved.data)
        self.assertIn(b"CD23 BBB", saved.data)
        self.assertIn(b'name="revision" value="1"', saved.data)
        self.assertEqual(native_vehicle_record(self.vehicle_id)["model"], "i20")
        self.assertEqual(read_work_record(self.work_id)["registration"], "CD23 BBB")
        self.assertEqual(sheets_status(self.work_id)["state"], "not_linked")
        with connect() as db:
            audit = db.execute("SELECT changed_fields,vehicle_id FROM hq_vehicle_edit_audit").fetchall()
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["vehicle_id"], self.vehicle_id)
            self.assertEqual(audit[0]["changed_fields"], "model,model_year,registration")
            self.assertNotIn("CD23 BBB", audit[0]["changed_fields"])
        # Same replay does not advance revision even with a stale page token.
        self.assertEqual(self.client.post(self.path, data=self.form).status_code, 303)
        self.assertEqual(native_vehicle_record(self.vehicle_id)["revision"], 1)
        self.assertIn(b"Edit linked vehicle",
                      self.client.get("/hq/work/" + self.work_id + "/edit").data)

    def test_stale_concurrent_edit_and_invalid_input_fail_closed(self):
        self.login()
        self.assertEqual(self.client.post(self.path, data=self.form).status_code, 303)
        stale = self.client.post(self.path, data={**self.form, "model": "Stale model"})
        self.assertEqual(stale.status_code, 409)
        self.assertIn(b"vehicle changed", stale.data)
        self.assertEqual(native_vehicle_record(self.vehicle_id)["model"], "i20")
        for changes in ({"model_year": "1800"}, {"registration": "BAD!REG"},
                        {"make": ""}, {"model": "X" * 61}):
            response = self.client.post(self.path, data={
                **self.form, "revision": "1", **changes})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post(self.path, data={
            **self.form, "revision": "invalid"}).status_code, 400)
        self.assertEqual(self.client.post(self.path, data={
            **self.form, "revision": "1", "csrf": "incorrect"}).status_code, 403)
        with connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_vehicle_edit_audit").fetchone()[0], 1)

    def test_cross_vehicle_registration_collision_and_customer_ownership(self):
        self.login()
        other = {**self.work, "operation_id": uuid.uuid4().hex,
                 "email": "other@example.invalid", "phone": "07700 900777",
                 "registration": "XY24 ZZZ"}
        with app.app_context():
            second = create_native_work(other)
        other_vehicle = native_work_record(second)["vehicle_id"]
        self.assertEqual(self.client.post(self.path, data={
            **self.form, "registration": "XY24ZZZ"}).status_code, 400)
        self.assertEqual(native_vehicle_record(self.vehicle_id)["registration"], "AB22 AAA")
        self.assertNotEqual(native_vehicle_record(other_vehicle)["customer_id"],
                            native_vehicle_record(self.vehicle_id)["customer_id"])
        self.assertEqual(self.client.post(self.path, data={
            **self.form, "customer_id": native_vehicle_record(other_vehicle)["customer_id"]
        }).status_code, 303)
        # Unknown extra form fields cannot reassign a vehicle to another customer.
        self.assertEqual(native_vehicle_record(self.vehicle_id)["customer_id"],
                         self.work["customer_id"] or read_work_record(self.work_id)["customer_id"] if "customer_id" in read_work_record(self.work_id) else native_vehicle_record(self.vehicle_id)["customer_id"])

    def test_approved_job_preserves_historical_vehicle_details(self):
        self.login()
        set_draft_financials(self.work_id, 25000, 0, expected_revision=0)
        change_work_status(self.work_id, "ready_for_review", expected_revision=1)
        change_work_status(self.work_id, "approved", expected_revision=2)
        page = self.client.get(self.path)
        self.assertIn(b"Editing is locked", page.data)
        self.assertNotIn(b"Save vehicle details", page.data)
        self.assertEqual(self.client.post(self.path, data=self.form).status_code, 400)
        self.assertEqual(native_vehicle_record(self.vehicle_id)["registration"], "AB22 AAA")
        with connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_vehicle_edit_audit").fetchone()[0], 0)

    def test_public_booking_record_shares_canonical_vehicle_but_not_owner_editor(self):
        with patch.dict(os.environ, {"HQ_PUBLIC_REQUESTS_ENABLED": "true"}):
            response = self.client.post("/api/public/customer-request", json={
                "kind": "quote", "name": "Synthetic public person",
                "email": "public-vehicle@example.invalid", "phone": "07700 900112",
                "service": "Window Tinting", "make": "Ford", "model": "Focus",
                "year": "2021", "registration": "EF21 ABC",
                "notes": "Test quote", "privacy_acknowledged": True,
            })
        self.assertEqual(response.status_code, 201, response.json)
        request_id = response.json["reference"]
        self.login()
        headers = {"X-HQ-CSRF": "vehicle-csrf"}
        self.assertEqual(self.client.post(
            "/api/hq/customer-requests/" + request_id + "/draft",
            json={"draft_type": "quote"}, headers=headers).status_code, 201)
        record = self.client.post(
            "/api/hq/customer-requests/" + request_id + "/work-record",
            json={}, headers=headers)
        self.assertEqual(record.status_code, 201, record.json)
        work_id = record.json["id"]
        vehicle_id = native_work_record(self.work_id)["vehicle_id"]
        public_vehicle = read_work_record(work_id)["registration"]
        self.assertEqual(public_vehicle, "EF21 ABC")
        with connect() as db:
            linked = db.execute("SELECT vehicle_id FROM hq_work_records WHERE id=?",
                                (work_id,)).fetchone()["vehicle_id"]
        self.assertNotEqual(linked, vehicle_id)
        self.assertEqual(self.client.get("/hq/vehicles/" + linked + "/edit").status_code, 200)
        self.client.get("/auth/logout")
        # Explicitly clear owner session, even when logout route behaviour evolves.
        with self.client.session_transaction() as sess:
            sess.clear()
        self.assertEqual(self.client.get("/hq/vehicles/" + linked + "/edit").status_code, 302)
        self.assertEqual(self.client.post("/hq/vehicles/" + linked + "/edit",
                          data=self.form).status_code, 302)


if __name__ == "__main__":
    unittest.main()
