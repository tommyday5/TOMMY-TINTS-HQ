"""Synthetic customer-owned profile correction requests and owner review workflow."""
import os
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_customer_portal import issue_customer_invite
from hq_customer_profile_requests import (
    submit_profile_correction, decide_profile_correction,
    customer_profile_snapshot, owner_pending_corrections,
)
from hq_native_work import create_native_work, native_work_record

ROOT = Path(__file__).resolve().parents[1]


class PortalProfileRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.tmp.name,
            "HQ_SESSION_SECRET": "profile-correction-test-secret",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
            "GOOGLE_CLIENT_ID": "synthetic", "GOOGLE_CLIENT_SECRET": "synthetic",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner = app.test_client()
        self.customer = app.test_client()
        self.other = app.test_client()
        self.work_id = self.create_work("Sample One", "one@example.invalid", "AA21 AAA")
        work = native_work_record(self.work_id)
        self.customer_id, self.vehicle_id = work["customer_id"], work["vehicle_id"]
        self.other_work = self.create_work("Sample Two", "two@example.invalid", "BB22 BBB")
        other = native_work_record(self.other_work)
        self.other_customer_id, self.other_vehicle_id = other["customer_id"], other["vehicle_id"]

    def create_work(self, name, email, reg):
        with app.app_context():
            return create_native_work({
                "operation_id": uuid.uuid4().hex, "customer_id": "", "vehicle_id": "",
                "customer_name": name, "email": email, "phone": "07700 900111",
                "make": "Hyundai", "model": "i30", "model_year": "2021",
                "registration": reg, "kind": "quote", "service": "Bodywork & Paint",
                "customer_tier": "retail", "notes": "SECRET MARGIN 500%",
                "preferred_date": "", "preferred_time": "",
            })

    def owner_login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "owner-fixture"),
                        csrf="owner-csrf", google_sub="owner-fixture")

    def signin(self, client, customer_id):
        code = issue_customer_invite(customer_id, uuid.uuid4().hex)
        self.assertEqual(client.get("/customer/login").status_code, 200)
        with client.session_transaction() as sess:
            csrf = sess["customer_portal_login_csrf"]
        response = client.post("/customer/login", data={"csrf": csrf, "code": code})
        self.assertEqual(response.status_code, 303)
        with client.session_transaction() as sess:
            return sess["customer_portal_csrf"]

    def test_customer_form_owner_reconcile_persists_without_sheets_write(self):
        self.assertEqual(self.customer.get("/customer/profile").status_code, 302)
        self.assertEqual(self.owner.get("/hq/profile-change-requests").status_code, 302)
        self.owner_login()
        csrf = self.signin(self.customer, self.customer_id)
        page = self.customer.get("/customer/account")
        self.assertIn(b"/customer/profile", page.data)
        response = self.customer.get("/customer/profile")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"AA21 AAA", response.data)
        self.assertNotIn(b"BB22 BBB", response.data)
        self.assertNotIn(b"SECRET MARGIN", response.data)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("no-referrer", response.headers["Referrer-Policy"])
        self.assertNotIn(b"two@example.invalid", response.data)
        op = uuid.uuid4().hex
        payload = {"csrf": csrf, "operation_id": op, "vehicle_id": "",
                   "field": "name", "value": "Updated One"}
        self.assertEqual(self.customer.post("/customer/profile/correction", data=payload).status_code, 303)
        self.assertIn(b"Correction request saved",
                      self.customer.get("/customer/profile?submitted=1").data)
        self.assertEqual(self.customer.post("/customer/profile/correction", data=payload).status_code, 303)
        self.assertEqual(len(owner_pending_corrections()), 1)
        self.assertEqual(customer_profile_snapshot(self.customer_id)[0]["name"], "Sample One")

        inbox = self.owner.get("/hq/profile-change-requests")
        self.assertEqual(inbox.status_code, 200)
        self.assertIn(b"Updated One", inbox.data)
        ref = owner_pending_corrections()[0]["id"]
        decision = {"csrf": "owner-csrf", "request_id": ref, "decision": "resolved"}
        denied = self.owner.post("/hq/profile-change-requests", data=decision)
        self.assertEqual(denied.status_code, 409)
        self.assertIn(b"does not match", denied.data)
        self.assertEqual(self.owner.post("/hq/customers/" + self.customer_id + "/edit", data={
            "csrf": "owner-csrf", "revision": "0", "name": "Updated One",
            "email": "one@example.invalid", "phone": "07700 900111",
            "address": "", "postcode": "",
        }).status_code, 303)
        self.assertEqual(self.owner.post("/hq/profile-change-requests", data=decision).status_code, 303)
        self.assertEqual(self.owner.post("/hq/profile-change-requests", data=decision).status_code, 303)
        self.assertEqual(customer_profile_snapshot(self.customer_id)[0]["name"], "Updated One")
        self.assertEqual(len(owner_pending_corrections()), 0)
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hq_portal_profile_requests").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT status FROM hq_portal_profile_requests").fetchone()[0], "resolved")
            self.assertEqual(db.execute("SELECT count(*) FROM hq_portal_profile_events").fetchone()[0], 2)
        self.assertNotIn(b"Updated One", self.other.get("/customer/profile").data)

    def test_linked_vehicle_only_owner_decline_and_customer_isolation(self):
        self.owner_login()
        csrf = self.signin(self.customer, self.customer_id)
        other_csrf = self.signin(self.other, self.other_customer_id)
        foreign = {"csrf": csrf, "operation_id": uuid.uuid4().hex,
                   "vehicle_id": self.other_vehicle_id, "field": "registration",
                   "value": "ZZ21 XXX"}
        self.assertEqual(self.customer.post("/customer/profile/correction", data=foreign).status_code, 400)
        invalid = {**foreign, "vehicle_id": self.vehicle_id, "field": "registration", "value": "!!"}
        self.assertEqual(self.customer.post("/customer/profile/correction", data=invalid).status_code, 400)
        local = {**invalid, "value": "CD23 XYZ"}
        self.assertEqual(self.customer.post("/customer/profile/correction", data=local).status_code, 303)
        ref = owner_pending_corrections()[0]["id"]
        self.assertEqual(len(customer_profile_snapshot(self.customer_id)[2]), 1)
        self.assertEqual(len(customer_profile_snapshot(self.other_customer_id)[2]), 0)
        self.assertNotIn(b"CD23 XYZ", self.other.get("/customer/profile").data)
        self.assertEqual(self.other.post("/customer/profile/correction", data={
            "csrf": other_csrf, "operation_id": uuid.uuid4().hex, "vehicle_id": self.vehicle_id,
            "field": "registration", "value": "INVALID",
        }).status_code, 400)
        self.assertEqual(self.owner.post("/hq/profile-change-requests", data={
            "csrf": "bad", "request_id": ref, "decision": "declined",
        }).status_code, 403)
        self.assertEqual(self.owner.post("/hq/profile-change-requests", data={
            "csrf": "owner-csrf", "request_id": ref, "decision": "declined",
        }).status_code, 303)
        self.assertEqual(self.owner.post("/hq/profile-change-requests", data={
            "csrf": "owner-csrf", "request_id": ref, "decision": "resolved",
        }).status_code, 409)
        self.assertEqual(len(owner_pending_corrections()), 0)
        with connect() as db:
            reg = db.execute("SELECT registration FROM hq_vehicles WHERE id=?", (self.vehicle_id,)).fetchone()[0]
            self.assertEqual(reg, "AA21 AAA")

    def test_csrf_validation_replay_collision_and_field_policy(self):
        csrf = self.signin(self.customer, self.customer_id)
        self.assertEqual(self.customer.post("/customer/profile/correction", data={
            "csrf": "wrong", "operation_id": uuid.uuid4().hex,
            "field": "email", "value": "test@example.invalid",
        }).status_code, 403)
        op = uuid.uuid4().hex
        created = submit_profile_correction(self.customer_id, None, "email",
                                           "new@example.invalid", op)
        self.assertEqual(submit_profile_correction(self.customer_id, None, "email",
                                                   "new@example.invalid", op), created)
        with self.assertRaises(ValueError):
            submit_profile_correction(self.customer_id, None, "phone", "07700 900333", op)
        with self.assertRaises(ValueError):
            submit_profile_correction(self.customer_id, None, "finance_notes", "private", uuid.uuid4().hex)
        with self.assertRaises(ValueError):
            submit_profile_correction(self.customer_id, self.vehicle_id, "model_year",
                                       "202A", uuid.uuid4().hex)
        with self.assertRaises(ValueError):
            submit_profile_correction(self.customer_id, None, "email", "bad-address", uuid.uuid4().hex)
        with self.assertRaises(ValueError):
            decide_profile_correction(created, "other")
        self.assertEqual(self.customer.post("/customer/profile/correction", data={
            "csrf": csrf, "operation_id": op, "field": "email",
            "value": "different@example.invalid",
        }).status_code, 400)
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hq_portal_profile_requests").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
