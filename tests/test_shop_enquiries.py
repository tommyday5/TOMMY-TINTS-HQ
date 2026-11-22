"""Synthetic customer -> HQ shop quote workflow tests. No messages, jobs, payments or Sheets writes."""
import json
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
from hq_commerce import save_product, database
from hq_core_records import _schema
from hq_shop_enquiries import (create_enquiry, enquiries_for_customer, owner_review)

ROOT = Path(__file__).resolve().parents[1]


class ShopEnquiryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name, "HQ_SESSION_SECRET": "synthetic-shop-only",
            "GOOGLE_CLIENT_ID": "fixture", "GOOGLE_CLIENT_SECRET": "fixture",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.customer_a = uuid.uuid4().hex
        self.customer_b = uuid.uuid4().hex
        self.client = app.test_client()
        self.owner = app.test_client()
        with connect() as db:
            _schema(db)
            for ident, name in ((self.customer_a, "Synthetic One"), (self.customer_b, "Synthetic Two")):
                db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)", (
                    ident, None, name, "fixture@example.invalid", "07700 900000", "2026-11-22"))
        self.product = save_product({
            "name": "Synthetic film", "variant": "TEST", "description": "fixture",
            "retail": 1599, "trade": 750, "cost": 400,
            "stock": 3, "delivery": 250, "published": 1,
        })

    def lines(self, qty=2):
        return [{"id": self.product, "quantity": qty}]

    def customer_login(self, client):
        with client.session_transaction() as sess:
            sess["customer_portal_csrf"] = "synthetic-csrf"

    def owner_login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="owner-csrf", google_sub="synthetic-owner")

    def test_retained_canonical_customer_id_and_server_owned_retail_snapshot(self):
        operation = uuid.uuid4().hex
        ident = create_enquiry(self.customer_a, operation, self.lines(), "delivery")
        self.assertEqual(create_enquiry(self.customer_a, operation, self.lines(), "delivery"), ident)
        with self.assertRaisesRegex(ValueError, "different basket"):
            create_enquiry(self.customer_a, operation, self.lines(1), "delivery")
        with self.assertRaisesRegex(ValueError, "different basket"):
            create_enquiry(self.customer_b, operation, self.lines(), "delivery")
        rows = enquiries_for_customer(self.customer_a)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], ident)
        self.assertEqual(rows[0]["indicative_total_pence"], 3448)
        self.assertEqual(rows[0]["items"][0]["unit_pence"], 1599)
        self.assertNotIn("cost", json.dumps(rows))
        self.assertNotIn("trade", json.dumps(rows))
        self.assertEqual(enquiries_for_customer(self.customer_b), [])
        with database() as db:
            self.assertEqual(db.execute("SELECT stock FROM products").fetchone()[0], 3)
        # SQLite reload from disk, not an in-memory mock.
        with connect() as db:
            self.assertEqual(db.execute("SELECT customer_id FROM hq_shop_enquiries WHERE id=?",
                                        (ident,)).fetchone()[0], self.customer_a)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_shop_enquiry_audit").fetchone()[0], 1)

    def test_customer_submit_route_auth_csrf_and_owner_review_are_separate(self):
        data = {"operation_id": uuid.uuid4().hex, "lines": self.lines(), "fulfilment": "collection",
                "tier": "trade", "total_pence": 1}
        self.assertEqual(self.client.post("/api/shop/enquiries", json=data).status_code, 401)
        self.assertEqual(self.owner.get("/hq/shop/enquiries").status_code, 302)
        self.customer_login(self.client)
        with patch("hq_shop_enquiries.active_customer", return_value=self.customer_a):
            self.assertEqual(self.client.post("/api/shop/enquiries", json=data).status_code, 403)
            response = self.client.post("/api/shop/enquiries", json=data,
                                        headers={"X-CSRF-Token": "synthetic-csrf"})
            self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
            ident = response.json["id"]
            self.assertFalse(response.json["payment_collected"])
            self.assertFalse(response.json["booking_created"])
            again = self.client.post("/api/shop/enquiries", json=data,
                                     headers={"X-CSRF-Token": "synthetic-csrf"})
            self.assertEqual(again.json["id"], ident)
            view = self.client.get("/customer/shop/enquiries")
            self.assertEqual(view.status_code, 200)
            self.assertIn(b"15.99", view.data)
            self.assertNotIn(b"7.50", view.data)
            self.assertNotIn(b"4.00", view.data)
        self.owner_login()
        listing = self.owner.get("/hq/shop/enquiries")
        self.assertEqual(listing.status_code, 200)
        self.assertIn(ident.encode(), listing.data)
        self.assertEqual(self.owner.post("/hq/shop/enquiries", data={
            "enquiry_id": ident, "revision": 0, "status": "quoted",
            "approved_pounds": "30.00", "public_note": "Owner-reviewed",
        }).status_code, 403)
        decision = self.owner.post("/hq/shop/enquiries", data={
            "csrf": "owner-csrf", "enquiry_id": ident, "revision": 0,
            "status": "quoted", "approved_pounds": "30.00",
            "public_note": "Owner-reviewed quote; no payment taken",
        })
        self.assertEqual(decision.status_code, 303)
        with patch("hq_shop_enquiries.active_customer", return_value=self.customer_a):
            view = self.client.get("/customer/shop/enquiries")
            self.assertIn(b"30.00", view.data)
            self.assertIn(b"Owner-reviewed quote", view.data)
        with patch("hq_shop_enquiries.active_customer", return_value=self.customer_b):
            view = self.client.get("/customer/shop/enquiries")
            self.assertNotIn(ident.encode(), view.data)
            self.assertNotIn(b"Owner-reviewed quote", view.data)
        with connect() as db:
            audit = db.execute("SELECT revision,action FROM hq_shop_enquiry_audit WHERE enquiry_id=? ORDER BY revision",
                               (ident,)).fetchall()
            self.assertEqual([(x[0], x[1]) for x in audit], [(0, "submitted"), (1, "quoted")])

    def test_owner_revision_conflicts_and_terminal_status(self):
        ident = create_enquiry(self.customer_a, uuid.uuid4().hex, self.lines(1), "collection")
        self.assertEqual(owner_review(ident, 0, "reviewing", "", ""), 1)
        self.assertEqual(owner_review(ident, 0, "reviewing", "", ""), 1)  # exact retry
        with self.assertRaisesRegex(ValueError, "updated elsewhere"):
            owner_review(ident, 0, "quoted", "50.00", "")
        with self.assertRaises(ValueError):
            owner_review(ident, 1, "quoted", "", "")
        with self.assertRaises(ValueError):
            owner_review(ident, 1, "quoted", "10.001", "")
        self.assertEqual(owner_review(ident, 1, "declined", "", ""), 2)
        with self.assertRaisesRegex(ValueError, "cannot be reopened"):
            owner_review(ident, 2, "reviewing", "", "")

    def test_stock_and_validation_fails_closed(self):
        with self.assertRaises(ValueError):
            create_enquiry(self.customer_a, uuid.uuid4().hex, self.lines(4), "collection")
        with self.assertRaises(ValueError):
            create_enquiry(self.customer_a, uuid.uuid4().hex, self.lines(1)*2, "collection")
        with self.assertRaises(ValueError):
            create_enquiry(self.customer_a, uuid.uuid4().hex, self.lines(True), "collection")
        with self.assertRaises(ValueError):
            create_enquiry(self.customer_a, uuid.uuid4().hex, self.lines(), "courier")
        self.assertEqual(enquiries_for_customer(self.customer_a), [])
        self.assertEqual(self.client.get("/book/shop").status_code, 200)
        self.assertIn(b"Sign in", self.client.get("/book/shop").data)


if __name__ == "__main__":
    unittest.main()
