"""Synthetic unified basket tests: customer to HQ, durable and privacy-protected.

No real bookings, payments, stock changes, messages or Sheets writes.
"""
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app import app
from customer_request_store import SERVICES, connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_commerce import save_product, database as commerce_db
from hq_combined_baskets import submit_combined, review_combined, _list
from hq_core_records import _schema
from hq_customer_portal import issue_customer_invite
from hq_service_quotes import save_service_price

ROOT = Path(__file__).resolve().parents[1]


class CombinedBasketTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name, "HQ_SESSION_SECRET": "synthetic-combined-only",
            "GOOGLE_CLIENT_ID": "fixture", "GOOGLE_CLIENT_SECRET": "fixture",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.a, self.b, self.vehicle = (uuid.uuid4().hex for _ in range(3))
        self.owner = app.test_client()
        self.customer = app.test_client()
        with connect() as db:
            _schema(db)
            for cid, name in ((self.a, "Synthetic Alpha"), (self.b, "Synthetic Beta")):
                db.execute("INSERT INTO hq_customers VALUES (?,?,?,?,?,?)",
                           (cid, None, name, "fixture@example.invalid", "07700 900000", "2026-11-22"))
            db.execute("INSERT INTO hq_vehicles VALUES (?,?,?,?,?,?,?)",
                       (self.vehicle, self.a, "Hyundai", "i30", "2018", "XX00 TST", "2026-11-22"))
        self.service = "Window Tinting"
        self.assertIn(self.service, SERVICES)
        save_service_price(self.service, "120.00", "70.00", "80.00", True, 0)
        self.product = save_product({
            "name": "Synthetic Film", "variant": "test", "description": "fixture",
            "retail": 1599, "trade": 750, "cost": 400, "stock": 3,
            "delivery": 250, "published": 1})
        self.op = uuid.uuid4().hex

    def submit(self, customer=None, op=None, quantity=2):
        return submit_combined(
            customer or self.a, op or self.op, self.vehicle, "trade",
            [self.service], [{"id": self.product, "quantity": quantity}],
            "delivery", "Synthetic scope")

    def customer_login(self, client, cid):
        code = issue_customer_invite(cid, uuid.uuid4().hex)
        with client.session_transaction() as sess:
            sess["customer_portal_login_csrf"] = "login-fixture"
        resp = client.post("/customer/login", data={"csrf": "login-fixture", "code": code})
        self.assertEqual(resp.status_code, 303, resp.get_data(as_text=True))
        with client.session_transaction() as sess:
            return sess["customer_portal_csrf"]

    def owner_login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="owner-fixture", google_sub="synthetic-owner")

    def test_one_canonical_record_server_prices_idempotency_and_stock(self):
        ident = self.submit()
        self.assertEqual(self.submit(), ident)
        with self.assertRaisesRegex(ValueError, "different basket"):
            self.submit(quantity=1)
        with self.assertRaisesRegex(ValueError, "Vehicle is not linked"):
            submit_combined(self.b, uuid.uuid4().hex, self.vehicle,
                            "retail", [self.service], [], "collection")
        rows = _list(self.a)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], ident)
        self.assertEqual(rows[0]["customer_id"], self.a)
        self.assertEqual(rows[0]["vehicle_id"], self.vehicle)
        self.assertEqual(rows[0]["service_retail_pence"], 12000)
        self.assertEqual(rows[0]["product_retail_pence"], 3198)
        self.assertEqual(rows[0]["delivery_pence"], 250)
        self.assertEqual(rows[0]["indicative_total_pence"], 15448)
        self.assertEqual(_list(self.b), [])
        with commerce_db() as db:
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?",
                                        (self.product,)).fetchone()["stock"], 3)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_combined_basket_audit").fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT customer_id FROM hq_combined_baskets WHERE id=?", (ident,)
            ).fetchone()[0], self.a)

    def test_review_revision_and_no_confirmed_payment(self):
        ident = self.submit()
        self.assertEqual(review_combined(ident, 0, "reviewing", "", ""), 1)
        self.assertEqual(review_combined(ident, 0, "reviewing", "", ""), 1)
        with self.assertRaisesRegex(ValueError, "changed elsewhere"):
            review_combined(ident, 0, "quoted", "100.00", "")
        self.assertEqual(review_combined(ident, 1, "quoted", "145.00", "Owner reviewed"), 2)
        self.assertEqual(review_combined(ident, 1, "quoted", "145.00", "Owner reviewed"), 2)
        self.assertEqual(_list(self.a)[0]["approved_total_pence"], 14500)
        with connect() as db:
            self.assertEqual([r[0] for r in db.execute(
                "SELECT action FROM hq_combined_basket_audit ORDER BY revision")],
                ["submitted", "reviewing", "quoted"])
        self.assertEqual(review_combined(ident, 2, "closed", "", ""), 3)
        with self.assertRaisesRegex(ValueError, "cannot be reopened"):
            review_combined(ident, 3, "reviewing", "", "")

    def test_customer_form_owner_inbox_and_isolation(self):
        self.assertEqual(self.customer.get("/book/basket").status_code, 302)
        self.assertEqual(self.owner.get("/hq/combined-enquiries").status_code, 302)
        csrf = self.customer_login(self.customer, self.a)
        page = self.customer.get("/book/basket")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"one quote request", page.data)
        payload = {"csrf": csrf, "operation_id": self.op, "vehicle_id": self.vehicle,
                   "requested_tier": "trade", "services": self.service,
                   "quantity_" + self.product: "2", "fulfilment": "delivery",
                   "notes": "Synthetic scope"}
        self.assertEqual(self.customer.post("/book/basket", data={
            **payload, "csrf": "wrong"}).status_code, 403)
        response = self.customer.post("/book/basket", data=payload)
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True))
        ident = _list(self.a)[0]["id"]
        self.assertIn(ident, response.headers["Location"])
        customer_page = self.customer.get("/customer/basket-requests")
        self.assertEqual(customer_page.status_code, 200)
        self.assertIn(b"154.48", customer_page.data)
        self.assertNotIn(b"70.00", customer_page.data)
        self.assertNotIn(b"4.00", customer_page.data)
        self.owner_login()
        self.assertIn(ident.encode(), self.owner.get("/hq/combined-enquiries").data)
        self.assertEqual(self.owner.post("/hq/combined-enquiries", data={
            "csrf": "bad", "enquiry_id": ident, "revision": 0,
            "status": "quoted", "approved_pounds": "140.00"}).status_code, 403)
        resp = self.owner.post("/hq/combined-enquiries", data={
            "csrf": "owner-fixture", "enquiry_id": ident, "revision": 0,
            "status": "quoted", "approved_pounds": "140.00",
            "public_note": "Private customer quote"})
        self.assertEqual(resp.status_code, 303, resp.get_data(as_text=True))
        self.assertIn(b"140.00", self.customer.get("/customer/basket-requests").data)
        other = app.test_client()
        self.customer_login(other, self.b)
        self.assertNotIn(ident.encode(), other.get("/customer/basket-requests").data)
        self.assertNotIn(b"140.00", other.get("/customer/basket-requests").data)

    def test_reject_invalid_requests_without_side_effects(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            submit_combined(self.a, uuid.uuid4().hex, None, "retail", [], [], "collection")
        with self.assertRaisesRegex(ValueError, "Duplicate service"):
            submit_combined(self.a, uuid.uuid4().hex, self.vehicle, "retail",
                            [self.service, self.service], [], "collection")
        with self.assertRaisesRegex(ValueError, "Retail, Trade or Maintenance"):
            submit_combined(self.a, uuid.uuid4().hex, self.vehicle, "wholesale",
                            [self.service], [], "collection")
        with self.assertRaises(ValueError):
            submit_combined(self.a, uuid.uuid4().hex, None, "retail",
                            [self.service], [], "collection")
        with self.assertRaises(ValueError):
            submit_combined(self.a, uuid.uuid4().hex, None, "retail", [],
                            [{"id": self.product, "quantity": 10}], "collection")
        self.assertEqual(_list(self.a), [])


if __name__ == "__main__":
    unittest.main()
