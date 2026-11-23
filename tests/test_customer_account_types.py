"""Synthetic-only native customer account classification and portal privacy tests."""
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
from hq_customer_account_types import (
    account_status, set_account_type, StaleAccountType, AccountTypeRetryConflict,
)
from hq_customer_archive import set_archived
from hq_customer_directory import create_standalone_customer
from hq_customer_portal import issue_customer_invite, redeem_customer_invite

ROOT = Path(__file__).resolve().parents[1]


class CustomerAccountTypeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.tmp.name,
            "HQ_SESSION_SECRET": "synthetic-account-types-only",
            "GOOGLE_CLIENT_ID": "synthetic",
            "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner = app.test_client()
        self.customer = app.test_client()
        with app.app_context():
            self.customer_id = create_standalone_customer({
                "operation_id": uuid.uuid4().hex,
                "name": "Synthetic Account Customer",
                "email": "account@example.invalid",
                "phone": "07700 900555",
                "address": "Synthetic private address",
                "postcode": "ZZ1 1ZZ",
            }, "fixture-owner")
            self.other_id = create_standalone_customer({
                "operation_id": uuid.uuid4().hex,
                "name": "Unrelated Synthetic Customer",
                "email": "unrelated@example.invalid",
                "phone": "07700 900556",
                "address": "", "postcode": "",
            }, "fixture-owner")
        self.path = "/hq/customers/" + self.customer_id + "/account-type"

    def login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "fixture-owner"),
                        google_sub="fixture-owner", csrf="account-type-csrf")

    def form(self, tier="retail", basis="owner_review", revision="0", operation_id=None):
        return {
            "csrf": "account-type-csrf", "tier": tier, "basis": basis,
            "revision": revision, "operation_id": operation_id or uuid.uuid4().hex,
        }

    def test_private_editor_auth_csrf_input_validation_and_link(self):
        self.assertEqual(self.owner.get(self.path).status_code, 302)
        self.assertEqual(self.owner.post(self.path, data=self.form()).status_code, 302)
        self.login()
        page = self.owner.get(self.path)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Retail", page.data)
        self.assertIn(b"not yet reviewed", page.data)
        self.assertIn("no-store", page.headers["Cache-Control"])
        self.assertIn(b"Review Retail / Trade / Maintenance",
                      self.owner.get("/hq/customers/" + self.customer_id).data)
        self.assertEqual(self.owner.post(self.path, data={
            **self.form(), "csrf": "bad",
        }).status_code, 403)
        self.assertEqual(self.owner.post(self.path, data={
            **self.form(), "tier": "trade",
        }).status_code, 400)
        self.assertEqual(self.owner.post(self.path, data={
            **self.form(), "basis": "maintenance_agreement_reviewed",
        }).status_code, 400)
        self.assertEqual(self.owner.post(self.path, data={
            **self.form(), "revision": "-1",
        }).status_code, 400)
        self.assertEqual(self.owner.get(
            "/hq/customers/invalid/account-type").status_code, 404)

    def test_save_reload_trade_and_maintenance_audit_with_no_tariff_side_effects(self):
        self.login()
        self.assertEqual(account_status(self.customer_id)["reviewed"], False)
        retail = self.owner.post(self.path, data=self.form())
        self.assertEqual(retail.status_code, 303, retail.data)
        self.assertEqual(account_status(self.customer_id)["revision"], 1)
        trade = self.owner.post(self.path, data=self.form(
            "trade", "trade_evidence_reviewed", "1"))
        self.assertEqual(trade.status_code, 303)
        self.assertIn(b"Trade", self.owner.get(self.path).data)
        maintenance = self.owner.post(self.path, data=self.form(
            "maintenance", "maintenance_agreement_reviewed", "2"))
        self.assertEqual(maintenance.status_code, 303)
        fresh = account_status(self.customer_id)
        self.assertEqual((fresh["tier"], fresh["basis"], fresh["revision"]),
                         ("maintenance", "maintenance_agreement_reviewed", 3))
        with connect() as db:
            events = db.execute("""SELECT revision,previous_tier,next_tier,basis
                FROM hq_customer_account_type_events WHERE customer_id=?
                ORDER BY revision""", (self.customer_id,)).fetchall()
            self.assertEqual([tuple(e) for e in events], [
                (1, "unreviewed", "retail", "owner_review"),
                (2, "retail", "trade", "trade_evidence_reviewed"),
                (3, "trade", "maintenance", "maintenance_agreement_reviewed"),
            ])
            self.assertFalse(db.execute("SELECT 1 FROM hq_work_records").fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM hq_service_prices").fetchone()
                             if db.execute("""SELECT 1 FROM sqlite_master
                                 WHERE name='hq_service_prices'""").fetchone() else False)
            self.assertNotIn("Synthetic Account Customer", str(events))
            self.assertNotIn("account@example.invalid", str(events))

    def test_replay_stale_revision_and_mismatched_operation(self):
        self.login()
        op = uuid.uuid4().hex
        payload = self.form("trade", "trade_evidence_reviewed", "0", op)
        self.assertEqual(self.owner.post(self.path, data=payload).status_code, 303)
        self.assertEqual(self.owner.post(self.path, data=payload).status_code, 303)
        self.assertEqual(self.owner.post(self.path, data={
            **payload, "tier": "maintenance",
            "basis": "maintenance_agreement_reviewed",
        }).status_code, 409)
        self.assertEqual(self.owner.post(self.path, data=self.form(
            "retail", "owner_review", "0")).status_code, 409)
        unchanged = self.owner.post(self.path, data=self.form(
            "trade", "trade_evidence_reviewed", "1"))
        self.assertEqual(unchanged.status_code, 303)
        with connect() as db:
            self.assertEqual(db.execute("""SELECT COUNT(*) FROM
                hq_customer_account_type_events WHERE customer_id=?""",
                (self.customer_id,)).fetchone()[0], 1)
        self.assertEqual(account_status(self.customer_id)["revision"], 1)

    def test_account_archive_lock_and_foreign_reference(self):
        with app.app_context():
            with self.assertRaises(LookupError):
                account_status(uuid.uuid4().hex)
            set_archived(self.customer_id, True, 0, uuid.uuid4().hex, "fixture-owner")
            with self.assertRaises(StaleAccountType):
                set_account_type(self.customer_id, "retail", "owner_review", 0,
                                 uuid.uuid4().hex, "fixture-owner")
            self.assertEqual(account_status(self.customer_id)["revision"], 0)
            set_archived(self.customer_id, False, 1, uuid.uuid4().hex, "fixture-owner")
            self.assertEqual(set_account_type(self.customer_id, "trade",
                "trade_evidence_reviewed", 0, uuid.uuid4().hex, "fixture-owner"), 1)

    def test_authenticated_customer_sees_own_label_without_evidence_or_costs(self):
        self.login()
        self.assertEqual(self.owner.post(self.path, data=self.form(
            "trade", "trade_evidence_reviewed")).status_code, 303)
        with app.app_context():
            code = issue_customer_invite(self.customer_id, uuid.uuid4().hex)
            sid = redeem_customer_invite(code, "synthetic-test-visitor")
            self.assertIsNotNone(sid)
        with self.customer.session_transaction() as sess:
            sess["customer_portal_sid"] = sid
            sess["customer_portal_csrf"] = "synthetic-public-csrf"
        account = self.customer.get("/customer/account")
        self.assertEqual(account.status_code, 200)
        self.assertIn(b"Account classification: Trade", account.data)
        self.assertNotIn(b"trade_evidence_reviewed", account.data)
        self.assertNotIn(b"Synthetic private address", account.data)
        self.assertNotIn(b"materials cost", account.data)
        self.assertNotIn(self.other_id.encode(), account.data)
        self.assertEqual(self.customer.get(self.path).status_code, 302)
        self.assertEqual(self.customer.post(self.path, data=self.form()).status_code, 302)


if __name__ == "__main__":
    unittest.main()
