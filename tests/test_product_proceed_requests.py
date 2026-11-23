"""Synthetic customer-to-HQ product-proceed request tests; never touch production."""
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
from hq_combined_baskets import (
    submit_combined, review_combined, create_product_only_quote,
    change_product_quote_status,
)
from hq_commerce import database as commerce_db, save_product
from hq_core_records import _schema
from hq_customer_portal import issue_customer_invite
from hq_product_quote_lines import save_product_quote_lines
from hq_product_proceed_requests import (
    request_to_proceed, customer_proceed_state, owner_proceed_requests,
    owner_review_proceed,
)

ROOT = Path(__file__).resolve().parents[1]


class ProductProceedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name, "HQ_SESSION_SECRET": "synthetic-proceed-secret",
            "GOOGLE_CLIENT_ID": "fixture", "GOOGLE_CLIENT_SECRET": "fixture",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner, self.mine, self.other = (app.test_client() for _ in range(3))
        self.cid, self.stranger = uuid.uuid4().hex, uuid.uuid4().hex
        with connect() as db:
            _schema(db)
            for customer_id, name in ((self.cid, "Test Product Buyer"),
                                      (self.stranger, "Other Buyer")):
                db.execute("INSERT INTO hq_customers VALUES (?,?,?,?,?,?)",
                           (customer_id, None, name, "buyer@example.invalid",
                            "07000000000", "2026-11-23"))
        self.product = save_product({
            "name": "Synthetic Spray", "variant": "Sample",
            "description": "Unit test only", "retail": 3000, "trade": 2000,
            "cost": 1000, "stock": 5, "delivery": 0, "published": 1})
        self.basket = submit_combined(
            self.cid, uuid.uuid4().hex, None, "retail", [],
            [{"id": self.product, "quantity": 1}], "collection")
        review_combined(self.basket, 0, "quoted", "40.00", "Test product quote")
        self.quote = create_product_only_quote(self.basket, 1)
        save_product_quote_lines(self.basket, 0, ["40.00"])
        change_product_quote_status(self.basket, 1, "approved")

    def owner_login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="owner-csrf", google_sub="synthetic-owner")

    def login_customer(self, client, customer_id):
        code = issue_customer_invite(customer_id, uuid.uuid4().hex)
        with client.session_transaction() as sess:
            sess["customer_portal_login_csrf"] = "login-csrf"
        response = client.post("/customer/login",
                               data={"csrf": "login-csrf", "code": code})
        self.assertEqual(response.status_code, 303)

    def test_customer_request_owner_approval_and_durable_customer_visibility(self):
        self.login_customer(self.mine, self.cid)
        self.login_customer(self.other, self.stranger)
        self.assertEqual(self.owner.get("/hq/product-proceed-requests").status_code, 302)
        page = self.mine.get("/customer/basket-requests")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Request to proceed with this product quotation", page.data)
        self.assertNotIn(self.basket.encode(), self.other.get("/customer/basket-requests").data)
        with self.mine.session_transaction() as sess:
            csrf = sess["customer_portal_csrf"]
        path = "/customer/basket-requests/" + self.basket + "/request-proceed"
        op = uuid.uuid4().hex
        self.assertEqual(self.mine.post(path, data={
            "csrf": csrf, "operation_id": op
        }).status_code, 303)
        ident = request_to_proceed(self.basket, self.cid, op)
        self.assertEqual(ident, request_to_proceed(self.basket, self.cid, uuid.uuid4().hex))
        self.assertEqual(len(owner_proceed_requests()), 1)
        self.assertEqual(customer_proceed_state(self.basket, self.cid)["status"], "pending")
        self.assertIsNone(customer_proceed_state(self.basket, self.stranger))
        self.assertIn(b"Request to proceed: Pending",
                      self.mine.get("/customer/basket-requests").data)
        self.owner_login()
        owner_path = "/hq/product-proceed-requests"
        self.assertEqual(self.owner.get(owner_path).status_code, 200)
        self.assertEqual(self.owner.post(owner_path, data={
            "csrf": "wrong", "request_id": ident, "revision": "0",
            "status": "approved"}).status_code, 403)
        self.assertEqual(self.owner.post(owner_path, data={
            "csrf": "owner-csrf", "request_id": ident, "revision": "0",
            "status": "approved", "public_note": "We will contact you to arrange next steps."
        }).status_code, 303)
        result = customer_proceed_state(self.basket, self.cid)
        self.assertEqual(result["status"], "approved")
        self.assertIn("arrange next steps", result["public_note"])
        self.assertIn(b"Request to proceed: Approved",
                      self.mine.get("/customer/basket-requests").data)
        self.assertNotIn(b"Request to proceed: Approved",
                         self.other.get("/customer/basket-requests").data)
        with connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_product_proceed_events"
            ).fetchone()[0], 2)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)
        with commerce_db() as db:
            self.assertEqual(db.execute(
                "SELECT stock FROM products WHERE id=?", (self.product,)).fetchone()[0], 5)

    def test_security_conflicts_and_immutable_approval_gate(self):
        self.login_customer(self.mine, self.cid)
        self.login_customer(self.other, self.stranger)
        path = "/customer/basket-requests/" + self.basket + "/request-proceed"
        with self.mine.session_transaction() as sess:
            csrf = sess["customer_portal_csrf"]
        with self.other.session_transaction() as sess:
            other_csrf = sess["customer_portal_csrf"]
        self.assertEqual(self.other.post(path, data={
            "csrf": other_csrf, "operation_id": uuid.uuid4().hex
        }).status_code, 404)
        self.assertEqual(self.mine.post(path, data={
            "csrf": "wrong", "operation_id": uuid.uuid4().hex
        }).status_code, 403)
        first_op = uuid.uuid4().hex
        ident = request_to_proceed(self.basket, self.cid, first_op)
        with self.assertRaisesRegex(ValueError, "previously used"):
            request_to_proceed(uuid.uuid4().hex, self.cid, first_op)
        with self.assertRaises(ValueError):
            owner_review_proceed(ident, 55, "approved", "", "synthetic-owner")
        self.owner_login()
        with self.assertRaisesRegex(ValueError, "customer-visible note|too long"):
            owner_review_proceed(ident, 0, "approved", "x" * 401, "synthetic-owner")
        change_product_quote_status(self.basket, 2, "withdrawn")
        with self.assertRaisesRegex(ValueError, "changed or withdrawn"):
            owner_review_proceed(ident, 0, "approved", "", "synthetic-owner")
        with connect() as db:
            self.assertEqual(db.execute(
                "SELECT status FROM hq_product_proceed_requests WHERE id=?",
                (ident,)).fetchone()[0], "pending")
        with commerce_db() as db:
            self.assertEqual(db.execute(
                "SELECT stock FROM products WHERE id=?", (self.product,)).fetchone()[0], 5)


if __name__ == "__main__":
    unittest.main()
