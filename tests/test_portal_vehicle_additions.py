"""Synthetic customer-to-HQ vehicle-addition lifecycle and permission isolation."""
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
from hq_customer_directory import create_standalone_customer, create_standalone_vehicle
from hq_customer_portal import issue_customer_invite
from hq_customer_archive import set_archived
from hq_portal_vehicle_additions import (
    submit_vehicle_request, customer_vehicle_requests, owner_vehicle_requests,
    decide_vehicle_request, owner_prefill,
)

ROOT = Path(__file__).resolve().parents[1]


class CustomerVehicleAdditionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name,
            "HQ_SESSION_SECRET": "vehicle-request-fixture-session-secret",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
            "GOOGLE_CLIENT_ID": "synthetic", "GOOGLE_CLIENT_SECRET": "synthetic",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner = app.test_client()
        self.customer = app.test_client()
        self.other = app.test_client()
        with app.app_context():
            self.customer_id = create_standalone_customer({
                "name": "Vehicle Request One", "email": "one@example.invalid",
                "phone": "07700 900341", "address": "", "postcode": "",
                "operation_id": uuid.uuid4().hex,
            }, "fixture-owner")
            self.other_id = create_standalone_customer({
                "name": "Vehicle Request Two", "email": "two@example.invalid",
                "phone": "07700 900342", "address": "", "postcode": "",
                "operation_id": uuid.uuid4().hex,
            }, "fixture-owner")
        self.payload = {"operation_id": uuid.uuid4().hex, "make": "Hyundai",
                        "model": "i30", "model_year": "2022", "registration": "AB22 CDE"}

    def owner_login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "fixture-owner"),
                        csrf="owner-csrf", google_sub="fixture-owner")

    def login_customer(self, client, customer_id):
        invite = issue_customer_invite(customer_id, uuid.uuid4().hex)
        self.assertEqual(client.get("/customer/login").status_code, 200)
        with client.session_transaction() as sess:
            login_csrf = sess["customer_portal_login_csrf"]
        self.assertEqual(client.post("/customer/login", data={
            "csrf": login_csrf, "code": invite}).status_code, 303)
        with client.session_transaction() as sess:
            return sess["customer_portal_csrf"]

    def test_end_to_end_request_owner_create_link_reload_customer_status(self):
        self.assertEqual(self.customer.get("/customer/profile").status_code, 302)
        self.assertEqual(self.owner.get("/hq/vehicle-add-requests").status_code, 302)
        customer_csrf = self.login_customer(self.customer, self.customer_id)
        self.owner_login()
        self.assertIn(b"Request vehicle addition", self.customer.get("/customer/profile").data)
        self.assertEqual(self.customer.post("/customer/profile/vehicle-request", data={
            **self.payload, "csrf": "incorrect"}).status_code, 403)
        payload = {**self.payload, "csrf": customer_csrf}
        self.assertEqual(self.customer.post("/customer/profile/vehicle-request", data=payload).status_code, 303)
        self.assertEqual(self.customer.post("/customer/profile/vehicle-request", data=payload).status_code, 303)
        rows = owner_vehicle_requests()
        self.assertEqual(len(rows), 1)
        ref = rows[0]["id"]
        self.assertEqual(rows[0]["customer_id"], self.customer_id)
        self.assertEqual(len(customer_vehicle_requests(self.customer_id)), 1)
        self.assertEqual(customer_vehicle_requests(self.other_id), [])
        customer_page = self.customer.get("/customer/profile")
        self.assertIn(b"pending", customer_page.data.lower())
        self.assertNotIn(b"two@example.invalid", customer_page.data)
        self.assertEqual(self.owner.post("/hq/vehicle-add-requests", data={
            "csrf": "incorrect", "request_id": ref, "decision": "linked",
            "vehicle_id": uuid.uuid4().hex}).status_code, 403)
        self.assertEqual(self.owner.post("/hq/vehicle-add-requests", data={
            "csrf": "owner-csrf", "request_id": ref, "decision": "linked",
            "vehicle_id": uuid.uuid4().hex}).status_code, 409)
        prefill = self.owner.get("/hq/customers/" + self.customer_id +
                                 "/vehicles/new?vehicle_request=" + ref)
        self.assertEqual(prefill.status_code, 200)
        self.assertIn(b"AB22 CDE", prefill.data)
        self.assertIn(b"Vehicle Request One", self.owner.get("/hq/vehicle-add-requests").data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_vehicles").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)
        # Owner explicitly saves the canonical HQ vehicle using the existing form.
        create = self.owner.post("/hq/customers/" + self.customer_id + "/vehicles/new",
            data={**self.payload, "operation_id": uuid.uuid4().hex, "csrf": "owner-csrf"})
        self.assertEqual(create.status_code, 303)
        with connect() as db:
            vehicle_id = db.execute("SELECT id FROM hq_vehicles WHERE customer_id=?",
                                   (self.customer_id,)).fetchone()[0]
        decision = {"csrf": "owner-csrf", "request_id": ref, "decision": "linked",
                    "vehicle_id": vehicle_id}
        self.assertEqual(self.owner.post("/hq/vehicle-add-requests", data=decision).status_code, 303)
        self.assertEqual(self.owner.post("/hq/vehicle-add-requests", data=decision).status_code, 303)
        self.assertNotIn(b"Vehicle Request One", self.owner.get("/hq/vehicle-add-requests").data)
        page = self.customer.get("/customer/profile")
        self.assertIn(b"Linked", page.data)
        self.assertIn(b"AB22 CDE", page.data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_vehicles").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_portal_vehicle_add_requests").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_portal_vehicle_add_events").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT status,linked_vehicle_id FROM hq_portal_vehicle_add_requests").fetchone()[0], "linked")
            evidence = db.execute("SELECT action FROM hq_portal_vehicle_add_events").fetchall()
            self.assertEqual([r[0] for r in evidence], ["requested", "linked"])
        self.assertEqual(self.other.get("/customer/profile").status_code, 302)

    def test_duplicates_cross_customer_foreign_vehicle_and_decline(self):
        self.owner_login()
        with app.app_context():
            ref = submit_vehicle_request(self.customer_id, self.payload)
            self.assertEqual(submit_vehicle_request(self.customer_id, self.payload), ref)
            with self.assertRaises(ValueError):
                submit_vehicle_request(self.other_id, self.payload)
            with self.assertRaises(ValueError):
                submit_vehicle_request(self.customer_id, {**self.payload, "model": "i20"})
            with self.assertRaises(ValueError):
                submit_vehicle_request(self.customer_id, {**self.payload,
                    "operation_id": uuid.uuid4().hex, "registration": "AB22-CDE"})
            foreign = create_standalone_vehicle(self.other_id, {
                **self.payload, "registration": "ZZ23 XYZ",
                "operation_id": uuid.uuid4().hex}, "fixture-owner")
            with self.assertRaises(ValueError):
                decide_vehicle_request(ref, "linked", foreign)
            with self.assertRaises(ValueError):
                owner_prefill(ref, self.other_id)
            self.assertEqual(owner_prefill(ref, self.customer_id)["make"], "Hyundai")
            decide_vehicle_request(ref, "declined")
            decide_vehicle_request(ref, "declined")
            with self.assertRaises(ValueError):
                decide_vehicle_request(ref, "linked", foreign)
        self.assertEqual(self.owner.get("/hq/customers/" + self.customer_id +
            "/vehicles/new?vehicle_request=" + ref).status_code, 404)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_portal_vehicle_add_events").fetchone()[0], 2)

    def test_validation_no_forged_customer_archive_or_automatic_mutations(self):
        csrf = self.login_customer(self.customer, self.customer_id)
        other_csrf = self.login_customer(self.other, self.other_id)
        self.assertEqual(self.other.post("/customer/profile/vehicle-request", data={
            **self.payload, "csrf": other_csrf, "customer_id": self.customer_id,
            "registration": "ZZ24 ZZZ"}).status_code, 303)
        self.assertEqual(owner_vehicle_requests()[0]["customer_id"], self.other_id)
        invalid = [{**self.payload, "make": ""}, {**self.payload, "model_year": "0000"},
                   {**self.payload, "registration": "BAD!"},
                   {**self.payload, "operation_id": "bad"}]
        for item in invalid:
            self.assertEqual(self.customer.post("/customer/profile/vehicle-request",
                data={**item, "csrf": csrf}).status_code, 400)
        with app.app_context():
            set_archived(self.customer_id, True, 0, uuid.uuid4().hex, "fixture-owner")
        self.assertEqual(self.customer.post("/customer/profile/vehicle-request", data={
            **self.payload, "csrf": csrf}).status_code, 400)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_vehicles").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
