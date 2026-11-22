"""Synthetic end-to-end tests for owner-invited customer accounts and approved statuses."""
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
from hq_core_records import read_work_record, set_draft_financials, change_work_status
from hq_customer_documents import save_public_description
from hq_native_work import create_native_work, native_work_record
from hq_customer_portal import (
    active_customer, customer_portal_snapshot, issue_customer_invite,
    portal_access_summary, revoke_customer_access, redeem_customer_invite,
)

ROOT = Path(__file__).resolve().parents[1]


class CustomerPortalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.tmp.name, "HQ_SESSION_SECRET": "portal-fixture-secret",
            "GOOGLE_CLIENT_ID": "synthetic", "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner = app.test_client()
        self.customer = app.test_client()
        self.work_id = self.create_work("Portal Customer", "portal@example.invalid", "AA21 AAA")
        self.customer_id = native_work_record(self.work_id)["customer_id"]

    def create_work(self, name, email, reg):
        with app.app_context():
            return create_native_work({
                "operation_id": uuid.uuid4().hex, "customer_id": "",
                "vehicle_id": "", "customer_name": name,
                "email": email, "phone": "07700 900111",
                "make": "Hyundai", "model": "i30", "model_year": "2021",
                "registration": reg, "kind": "quote",
                "service": "Bodywork & Paint", "customer_tier": "retail",
                "notes": "PRIVATE ACTUAL MATERIAL COST 9999 MARGIN 85",
                "preferred_date": "2026-12-01", "preferred_time": "10:00",
            })

    def login_owner(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="portal-test-csrf", google_sub="synthetic-owner")

    def invite(self, customer_id=None, operation_id=None):
        customer_id = customer_id or self.customer_id
        operation_id = operation_id or uuid.uuid4().hex
        path = f"/hq/customers/{customer_id}/portal-access"
        response = self.owner.post(path, data={
            "csrf": "portal-test-csrf", "action": "issue",
            "operation_id": operation_id,
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        match = re.search(rb'<code id="access-code">([A-Za-z0-9_-]+)</code>', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode(), operation_id

    def customer_signin(self, code):
        page = self.customer.get("/customer/login")
        self.assertEqual(page.status_code, 200)
        with self.customer.session_transaction() as sess:
            csrf = sess["customer_portal_login_csrf"]
        response = self.customer.post("/customer/login", data={"csrf": csrf, "code": code})
        return response

    def approve(self, work_id, label="Repaint bonnet with matching finish."):
        save_public_description(work_id, label, expected_revision=0)
        set_draft_financials(work_id, 35000, 10000, expected_revision=1)
        change_work_status(work_id, "ready_for_review", expected_revision=2)
        change_work_status(work_id, "approved", expected_revision=3)

    def test_owner_invitation_one_use_and_customer_approved_status_isolation(self):
        path = f"/hq/customers/{self.customer_id}/portal-access"
        self.assertEqual(self.owner.get(path).status_code, 302)
        self.assertEqual(self.customer.get("/customer/account").status_code, 302)
        self.login_owner()
        response = self.owner.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Generate single-use", response.data)
        self.assertNotIn(b"access-code", response.data)
        self.assertEqual(self.owner.post(path, data={"action": "revoke"}).status_code, 403)
        self.assertEqual(self.owner.post(path, data={
            "csrf": "wrong", "action": "issue", "operation_id": uuid.uuid4().hex,
        }).status_code, 403)

        code, op = self.invite()
        self.assertEqual(len(code), 43)
        self.assertNotIn(code, self.owner.get(path).get_data(as_text=True))
        self.assertEqual(portal_access_summary(self.customer_id)["invites"], 1)
        with connect() as db:
            raw_invite = db.execute("SELECT token_hash FROM hq_customer_portal_invites").fetchone()[0]
            self.assertNotEqual(raw_invite, code)
            self.assertNotIn(code, str(db.execute("SELECT * FROM hq_customer_portal_invites").fetchone()))

        # A repeated POST with the same operation must not generate a second code.
        replay = self.owner.post(path, data={
            "csrf": "portal-test-csrf", "action": "issue", "operation_id": op,
        })
        self.assertEqual(replay.status_code, 200)
        self.assertNotIn(code.encode(), replay.data)
        self.assertNotIn(b'id="access-code"', replay.data)

        result = self.customer_signin(code)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers["Location"], "/customer/account")
        page = self.customer.get("/customer/account")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"No approved work", page.data)
        self.assertNotIn(b"PRIVATE ACTUAL", page.data)
        self.assertNotIn(b"portal@example.invalid", page.data)
        self.assertIn("no-store", page.headers["Cache-Control"])
        self.assertIn("no-referrer", page.headers["Referrer-Policy"])
        self.assertIn("noindex", page.headers["X-Robots-Tag"])

        # Same one-time code cannot be used from another browser.
        new_browser = app.test_client()
        new_browser.get("/customer/login")
        with new_browser.session_transaction() as sess:
            csrf = sess["customer_portal_login_csrf"]
        self.assertEqual(new_browser.post("/customer/login", data={
            "csrf": csrf, "code": code,
        }).status_code, 401)

        other_id = self.create_work("Other Person", "other@example.invalid", "ZZ20 BBB")
        self.approve(other_id, "PRIVATE OTHER CUSTOMER WORK")
        self.approve(self.work_id, "Repaint bonnet with matching finish.")
        view = self.customer.get("/customer/account").get_data(as_text=True)
        self.assertIn("Repaint bonnet with matching finish.", view)
        self.assertIn("£350.00", view)
        self.assertIn(self.work_id, view)
        self.assertNotIn("PRIVATE ACTUAL", view)
        self.assertNotIn("PRIVATE OTHER CUSTOMER", view)
        self.assertNotIn(other_id, view)
        self.assertNotIn("deposit", view.lower())
        self.assertNotIn("invoice received", view.lower())
        self.assertNotIn("9999", view)
        self.assertEqual(read_work_record(self.work_id)["kind"], "job")

        self.assertEqual(self.owner.post(path, data={
            "csrf": "portal-test-csrf", "action": "revoke",
        }).status_code, 200)
        self.assertEqual(portal_access_summary(self.customer_id)["sessions"], 0)
        self.assertEqual(self.customer.get("/customer/account").status_code, 302)
        self.assertEqual(new_browser.post("/customer/login", data={
            "csrf": csrf, "code": code,
        }).status_code, 401)

    def test_expiry_replacement_logout_validation_and_stale_sessions(self):
        self.login_owner()
        code, _ = self.invite()
        replacement, _ = self.invite()
        self.assertNotEqual(code, replacement)
        self.assertEqual(self.customer_signin(code).status_code, 401)
        # Fresh code after a failed login, against the same client.
        self.assertEqual(self.customer_signin(replacement).status_code, 303)
        self.assertEqual(self.customer.post("/customer/logout", data={"csrf": "wrong"}).status_code, 403)
        with self.customer.session_transaction() as sess:
            logout_csrf = sess["customer_portal_csrf"]
        self.assertEqual(self.customer.post("/customer/logout", data={
            "csrf": logout_csrf,
        }).status_code, 303)
        self.assertEqual(self.customer.get("/customer/account").status_code, 302)
        self.assertEqual(self.customer_signin(replacement).status_code, 401)

        fresh, _ = self.invite()
        with connect() as db:
            db.execute("UPDATE hq_customer_portal_invites SET expires_at=0 WHERE used_at IS NULL")
        self.assertEqual(self.customer_signin(fresh).status_code, 401)
        self.assertEqual(portal_access_summary(self.customer_id)["invites"], 0)

    def test_login_attempt_limit_no_public_record_lookup_and_secrets_required(self):
        self.login_owner()
        self.assertEqual(self.customer.get("/hq/customers/" + self.customer_id).status_code, 302)
        self.assertEqual(self.customer.get("/hq/customers/" + self.customer_id +
                                          "/portal-access").status_code, 302)
        self.customer.get("/customer/login")
        with self.customer.session_transaction() as sess:
            csrf = sess["customer_portal_login_csrf"]
        for _ in range(8):
            self.assertEqual(self.customer.post("/customer/login", data={
                "csrf": csrf, "code": "x" * 43,
            }).status_code, 401)
        self.assertEqual(self.customer.post("/customer/login", data={
            "csrf": csrf, "code": "x" * 43,
        }).status_code, 429)
        with patch.dict(os.environ, {"HQ_SESSION_SECRET": ""}):
            self.assertEqual(self.customer.get("/customer/login").status_code, 503)
        self.assertEqual(self.customer.get("/customer/account").status_code, 302)
        self.assertEqual(self.customer.post("/customer/login", data={
            "csrf": "incorrect", "code": "x" * 43,
        }).status_code, 403)

    def test_public_book_request_to_canonical_owner_approved_portal(self):
        # Public /book request is not itself a booking, customer login, or confirmed slot.
        with patch.dict(os.environ, {"HQ_PUBLIC_REQUESTS_ENABLED": "true"}):
            inbound = self.customer.post("/api/public/customer-request", json={
                "kind": "quote", "name": "Enquiry Person",
                "email": "enquiry@example.invalid", "phone": "07700 900333",
                "service": "Window Tinting", "make": "Ford", "model": "Fiesta",
                "year": "2021", "registration": "AB21 AAA",
                "notes": "PRIVATE ENQUIRY NOTES", "privacy_acknowledged": True,
            })
        self.assertEqual(inbound.status_code, 201, inbound.json)
        self.assertFalse(inbound.json["confirmed_booking"])
        request_ref = inbound.json["reference"]
        self.login_owner()
        headers = {"X-HQ-CSRF": "portal-test-csrf"}
        self.assertEqual(self.owner.post(
            "/api/hq/customer-requests/" + request_ref + "/draft",
            json={"draft_type": "quote"}, headers=headers).status_code, 201)
        promoted = self.owner.post(
            "/api/hq/customer-requests/" + request_ref + "/work-record",
            json={}, headers=headers)
        self.assertEqual(promoted.status_code, 201, promoted.json)
        work_id = promoted.json["id"]
        self.assertEqual(read_work_record(work_id)["source_request_id"], request_ref)
        with connect() as db:
            customer_id = db.execute("SELECT customer_id FROM hq_work_records WHERE id=?",
                                     (work_id,)).fetchone()[0]
        code, _ = self.invite(customer_id)
        self.assertEqual(self.customer_signin(code).status_code, 303)
        self.assertNotIn(work_id.encode(), self.customer.get("/customer/account").data)
        self.approve(work_id, "Reviewed and approved rear window tint.")
        page = self.customer.get("/customer/account")
        self.assertIn(work_id.encode(), page.data)
        self.assertIn(b"Reviewed and approved rear window tint", page.data)
        self.assertNotIn(b"PRIVATE ENQUIRY NOTES", page.data)
        self.assertIn(b"not a confirmed booking", page.data)


if __name__ == "__main__":
    unittest.main()
