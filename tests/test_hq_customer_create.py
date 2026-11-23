"""Synthetic-only owner-created HQ customer workflow; no Sheets or real records."""
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
from hq_customer_directory import (create_standalone_customer,
                                   CustomerCreationConflict, customer_snapshot)

ROOT = Path(__file__).resolve().parents[1]


class NativeStandaloneCustomerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.tmp.name,
            "HQ_SESSION_SECRET": "native-customer-fixture-secret",
            "GOOGLE_CLIENT_ID": "synthetic",
            "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()
        self.payload = {
            "operation_id": uuid.uuid4().hex,
            "name": "Synthetic Standalone Contact",
            "email": "CONTACT@example.invalid",
            "phone": "07700 900123",
            "address": "Fictional test lane",
            "postcode": "aa1 2bb",
        }

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="fixture-csrf", google_sub="synthetic-owner")

    def test_owner_only_and_csrf(self):
        self.assertEqual(self.client.get("/hq/customers/new").status_code, 302)
        self.assertEqual(self.client.post("/hq/customers/new", data={
            **self.payload, "csrf": "fixture-csrf"}).status_code, 302)
        self.login()
        page = self.client.get("/hq/customers/new")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Save new HQ customer", page.data)
        self.assertIn("no-store", page.headers["Cache-Control"])
        directory = self.client.get("/hq/customers")
        self.assertIn(b"Add new customer without a job", directory.data)
        self.assertEqual(self.client.post("/hq/customers/new", data=self.payload).status_code, 403)
        self.assertEqual(self.client.post("/hq/customers/new", data={
            **self.payload, "csrf": "wrong"}).status_code, 403)
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='hq_standalone_customer_creations'").fetchone()[0], 0)

    def test_save_reload_idempotency_audit_and_existing_editor(self):
        self.login()
        request = {**self.payload, "csrf": "fixture-csrf"}
        response = self.client.post("/hq/customers/new", data=request)
        self.assertEqual(response.status_code, 303)
        self.assertIn("/hq/customers/", response.headers["Location"])
        ident = response.headers["Location"].rsplit("/", 1)[-1]
        self.assertRegex(ident, r"^[0-9a-f]{32}$")
        same = self.client.post("/hq/customers/new", data=request)
        self.assertEqual(same.status_code, 303)
        self.assertEqual(same.headers["Location"], response.headers["Location"])
        with connect() as db:
            row = db.execute("SELECT name,email,phone,source_request_id FROM hq_customers WHERE id=?", (ident,)).fetchone()
            self.assertEqual(tuple(row), ("Synthetic Standalone Contact",
                                         "contact@example.invalid", "07700 900123", None))
            addr = db.execute("SELECT address,postcode FROM hq_customer_addresses WHERE customer_id=?", (ident,)).fetchone()
            self.assertEqual(tuple(addr), ("Fictional test lane", "AA1 2BB"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_standalone_customer_events WHERE customer_id=?", (ident,)).fetchone()[0], 1)
            journal = db.execute("SELECT payload_fingerprint,actor_fingerprint FROM hq_standalone_customer_creations WHERE customer_id=?", (ident,)).fetchone()
            self.assertEqual(len(journal["payload_fingerprint"]), 64)
            self.assertNotIn("contact@example.invalid", str(tuple(journal)))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records WHERE customer_id=?", (ident,)).fetchone()[0], 0)
        profile = self.client.get("/hq/customers/" + ident)
        self.assertEqual(profile.status_code, 200)
        self.assertIn(b"Synthetic Standalone Contact", profile.data)
        edit = self.client.get("/hq/customers/" + ident + "/edit")
        self.assertEqual(edit.status_code, 200)
        self.assertIn(b"Fictional test lane", edit.data)
        self.assertEqual(customer_snapshot(ident)[3], 0)
        # Historic source ID ownership has not been created or claimed.
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='hq_historical_j_candidates'").fetchone()[0], 0)

    def test_duplicate_contacts_and_conflicting_replay_are_not_merged(self):
        self.login()
        first = self.client.post("/hq/customers/new", data={
            **self.payload, "csrf": "fixture-csrf"})
        self.assertEqual(first.status_code, 303)
        conflict = self.client.post("/hq/customers/new", data={
            **self.payload, "phone": "07700 900999", "csrf": "fixture-csrf"})
        self.assertEqual(conflict.status_code, 409)
        self.assertIn(b"already used", conflict.data)
        with app.app_context():
            with self.assertRaises(CustomerCreationConflict):
                create_standalone_customer({**self.payload, "name": "Else"}, "synthetic-owner")
        for details in (
            {"operation_id": uuid.uuid4().hex, "email": "contact@example.invalid",
             "phone": "07700 900998"},
            {"operation_id": uuid.uuid4().hex, "email": "distinct@example.invalid",
             "phone": "(07700) 900123"},
        ):
            response = self.client.post("/hq/customers/new", data={
                **self.payload, **details, "csrf": "fixture-csrf"})
            self.assertEqual(response.status_code, 400)
            self.assertIn(b"already exists in HQ", response.data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_customers").fetchone()[0], 1)

    def test_invalid_fields_fail_without_partial_customer(self):
        self.login()
        bad = [
            {"name": ""}, {"email": "", "phone": ""},
            {"email": "bad-email"}, {"phone": "abc"},
            {"operation_id": "not-a-token"}, {"name": "Z" * 161},
        ]
        for fields in bad:
            with self.subTest(fields=fields):
                payload = {**self.payload, **fields, "csrf": "fixture-csrf"}
                response = self.client.post("/hq/customers/new", data=payload)
                self.assertEqual(response.status_code, 400)
        with connect() as db:
            try:
                count = db.execute("SELECT COUNT(*) FROM hq_customers").fetchone()[0]
            except Exception:
                count = 0
            self.assertEqual(count, 0)

    def test_distinct_contact_same_name_stays_separate(self):
        with app.app_context():
            one = create_standalone_customer(self.payload, "synthetic-owner")
            two = create_standalone_customer({
                **self.payload, "operation_id": uuid.uuid4().hex,
                "email": "second@example.invalid", "phone": "07700 900124",
            }, "synthetic-owner")
        self.assertNotEqual(one, two)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_customers").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
