"""Synthetic owner-only standalone HQ vehicle creation; no Sheets or live bookings."""
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
from hq_customer_archive import set_archived
from hq_customer_directory import create_standalone_customer
from hq_native_work import create_native_work, native_vehicle_record, native_work_record

ROOT = Path(__file__).resolve().parents[1]


class StandaloneVehicleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.tmp.name,
            "HQ_SESSION_SECRET": "standalone-vehicle-fixture-secret",
            "GOOGLE_CLIENT_ID": "synthetic", "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner = app.test_client()
        self.anonymous = app.test_client()
        with app.app_context():
            self.customer = create_standalone_customer({
                "operation_id": uuid.uuid4().hex, "name": "Vehicle Fixture A",
                "email": "a@example.invalid", "phone": "07700 900321",
                "address": "", "postcode": "",
            }, "synthetic-owner")
            self.other = create_standalone_customer({
                "operation_id": uuid.uuid4().hex, "name": "Vehicle Fixture B",
                "email": "b@example.invalid", "phone": "07700 900322",
                "address": "", "postcode": "",
            }, "synthetic-owner")
        self.url = "/hq/customers/" + self.customer + "/vehicles/new"
        self.payload = {
            "csrf": "vehicle-create-csrf", "operation_id": uuid.uuid4().hex,
            "make": "Hyundai", "model": "i30", "model_year": "2022",
            "registration": "AB22 CDE",
        }

    def login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="vehicle-create-csrf", google_sub="synthetic-owner")

    def test_owner_only_form_csrf_and_customer_link(self):
        self.assertEqual(self.anonymous.get(self.url).status_code, 302)
        self.assertEqual(self.anonymous.post(self.url, data=self.payload).status_code, 302)
        self.login()
        page = self.owner.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Save vehicle to HQ customer", page.data)
        self.assertIn(b"Vehicle Fixture A", page.data)
        self.assertIn("no-store", page.headers["Cache-Control"])
        self.assertIn(b"Add vehicle without a job",
                      self.owner.get("/hq/customers/" + self.customer).data)
        self.assertEqual(self.owner.post(self.url, data={
            **self.payload, "csrf": "wrong"}).status_code, 403)
        self.assertEqual(self.owner.get("/hq/customers/bad/vehicles/new").status_code, 404)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_vehicles").fetchone()[0], 0)

    def test_save_reload_idempotent_reuse_and_audit(self):
        self.login()
        response = self.owner.post(self.url, data=self.payload)
        self.assertEqual(response.status_code, 303)
        self.assertIn("/hq/customers/" + self.customer, response.headers["Location"])
        self.assertEqual(self.owner.post(self.url, data=self.payload).status_code, 303)
        profile = self.owner.get(response.headers["Location"])
        self.assertEqual(profile.status_code, 200)
        self.assertIn(b"Vehicle saved", profile.data)
        self.assertIn(b"AB22 CDE", profile.data)
        with connect() as db:
            row = db.execute("SELECT * FROM hq_vehicles").fetchone()
            self.assertEqual(row["customer_id"], self.customer)
            self.assertEqual(row["registration"], "AB22 CDE")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_vehicles").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_standalone_vehicle_events").fetchone()[0], 1)
            audit = db.execute("SELECT payload_fingerprint,actor_fingerprint FROM hq_standalone_vehicle_creations").fetchone()
            self.assertEqual(len(audit["payload_fingerprint"]), 64)
            self.assertNotIn("AB22 CDE", str(tuple(audit)))
            vehicle_id = row["id"]
        self.assertEqual(native_vehicle_record(vehicle_id)["revision"], 0)
        self.assertEqual(self.owner.get("/hq/vehicles/" + vehicle_id + "/edit").status_code, 200)
        self.assertEqual(self.owner.post(self.url, data={
            **self.payload, "model": "i20"}).status_code, 409)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_vehicles").fetchone()[0], 1)

        # Same canonical vehicle is selectable for a later native quote;
        # no duplicate vehicle or historical Sheets reference is created.
        with app.app_context():
            work_id = create_native_work({
                "operation_id": uuid.uuid4().hex,
                "customer_id": self.customer, "vehicle_id": vehicle_id,
                "customer_name": "", "email": "", "phone": "",
                "make": "", "model": "", "model_year": "", "registration": "",
                "kind": "quote", "service": "Window Tinting",
                "customer_tier": "retail", "notes": "Synthetic test",
                "preferred_date": "", "preferred_time": "",
            })
        work = native_work_record(work_id)
        self.assertEqual(work["vehicle_id"], vehicle_id)
        self.assertEqual(work["customer_id"], self.customer)
        self.assertEqual(work["source_request_id"], "HQ-" + work_id)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_vehicles").fetchone()[0], 1)

    def test_validation_duplicate_registration_and_separate_ownership(self):
        self.login()
        for invalid in (
            {"make": ""}, {"model": ""}, {"model_year": "1900"},
            {"registration": "INVALID!"}, {"operation_id": "bad"},
        ):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.owner.post(self.url, data={
                    **self.payload, **invalid}).status_code, 400)
        self.assertEqual(self.owner.post(self.url, data=self.payload).status_code, 303)
        other_url = "/hq/customers/" + self.other + "/vehicles/new"
        self.assertEqual(self.owner.post(other_url, data={
            **self.payload, "operation_id": uuid.uuid4().hex,
            "registration": "AB22-CDE"}).status_code, 400)
        self.assertEqual(self.owner.post(other_url, data={
            **self.payload, "operation_id": uuid.uuid4().hex,
            "registration": "BB23 FGH", "customer_id": self.customer,
        }).status_code, 303)
        with connect() as db:
            rows = db.execute("SELECT customer_id,registration FROM hq_vehicles ORDER BY registration").fetchall()
            self.assertEqual([(r["customer_id"], r["registration"]) for r in rows],
                             [(self.customer, "AB22 CDE"), (self.other, "BB23 FGH")])
        # An unregistered vehicle may be saved, but a repeat with the same
        # customer, make, model and year must be reviewed instead of duplicated.
        no_reg = {**self.payload, "operation_id": uuid.uuid4().hex,
                  "registration": "", "model": "i20"}
        self.assertEqual(self.owner.post(self.url, data=no_reg).status_code, 303)
        self.assertEqual(self.owner.post(self.url, data={
            **no_reg, "operation_id": uuid.uuid4().hex}).status_code, 400)

    def test_archived_customer_requires_restore(self):
        self.login()
        with app.app_context():
            set_archived(self.customer, True, 0, uuid.uuid4().hex, "synthetic-owner")
        page = self.owner.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Restore the customer", page.data)
        self.assertNotIn(b"Save vehicle to HQ customer", page.data)
        self.assertEqual(self.owner.post(self.url, data=self.payload).status_code, 400)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_vehicles").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
