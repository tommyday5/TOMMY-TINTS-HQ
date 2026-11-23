"""Synthetic product-only HQ quotation lifecycle; no vehicle, stock or payment writes."""
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_core_records import _schema, StaleWorkRecordError
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_customer_portal import issue_customer_invite
from hq_commerce import save_product, database as commerce_db
from hq_combined_baskets import (
    submit_combined, review_combined, create_product_only_quote,
    change_product_quote_status, _list,
)

ROOT = Path(__file__).resolve().parents[1]


class ProductOnlyQuoteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name, "HQ_SESSION_SECRET": "synthetic-products-only",
            "GOOGLE_CLIENT_ID": "fixture", "GOOGLE_CLIENT_SECRET": "fixture",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner = app.test_client()
        self.mine = app.test_client()
        self.other = app.test_client()
        self.cid, self.stranger = uuid.uuid4().hex, uuid.uuid4().hex
        with connect() as db:
            _schema(db)
            for cid, name in ((self.cid, "Synthetic Buyer"), (self.stranger, "Synthetic Other")):
                db.execute("INSERT INTO hq_customers VALUES (?,?,?,?,?,?)",
                           (cid, None, name, "synthetic@example.invalid", "07000000000", "2026-11-23"))
        self.pid = save_product({
            "name": "Synthetic Film", "variant": "test", "description": "fixture",
            "retail": 2000, "trade": 800, "cost": 400, "stock": 6,
            "delivery": 250, "published": 1})
        self.ident = submit_combined(
            self.cid, uuid.uuid4().hex, None, "trade", [],
            [{"id": self.pid, "quantity": 1}], "collection")
        review_combined(self.ident, 0, "quoted", "25.00", "Owner-reviewed offer")

    def owner_login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="owner-fixture", google_sub="synthetic-owner")

    def login_customer(self, client, customer_id):
        code = issue_customer_invite(customer_id, uuid.uuid4().hex)
        with client.session_transaction() as sess:
            sess["customer_portal_login_csrf"] = "login-fixture"
        response = client.post("/customer/login", data={
            "csrf": "login-fixture", "code": code})
        self.assertEqual(response.status_code, 303)

    def test_durable_idempotent_quote_with_immutable_source_and_no_stock_changes(self):
        with self.assertRaisesRegex(ValueError, "Invalid vehicle"):
            service = submit_combined(
                self.cid, uuid.uuid4().hex, None, "retail",
                ["Window Tinting"], [], "collection")
        with self.assertRaises(StaleWorkRecordError):
            create_product_only_quote(self.ident, 0)
        quote_id = create_product_only_quote(self.ident, 1)
        self.assertEqual(quote_id, create_product_only_quote(self.ident, 1))
        with self.assertRaises(StaleWorkRecordError):
            create_product_only_quote(self.ident, 2)
        with self.assertRaisesRegex(ValueError, "original offer is locked"):
            review_combined(self.ident, 1, "quoted", "26.00", "Modified")
        with connect() as db:
            quote = db.execute("""SELECT * FROM hq_combined_product_quotes
                WHERE basket_id=?""", (self.ident,)).fetchone()
            self.assertEqual((quote["id"], quote["customer_id"], quote["status"],
                              quote["approved_total_pence"], quote["basket_revision"]),
                             (quote_id, self.cid, "draft", 2500, 1))
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_combined_product_quote_events"
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)
            self.assertIsNone(db.execute(
                "SELECT vehicle_id FROM hq_combined_baskets WHERE id=?",
                (self.ident,)).fetchone()[0])
        with commerce_db() as db:
            self.assertEqual(db.execute(
                "SELECT stock FROM products WHERE id=?", (self.pid,)).fetchone()[0], 6)
        self.assertIsNone(_list(self.cid)[0]["approved_product_quote_pence"])

    def test_owner_approval_withdrawal_concurrency_and_customer_isolation(self):
        self.assertEqual(self.owner.get("/hq/combined-enquiries").status_code, 302)
        self.owner_login()
        path = "/hq/combined-enquiries/" + self.ident
        self.assertIn(b"Create native product-only HQ quote",
                      self.owner.get("/hq/combined-enquiries").data)
        self.assertEqual(self.owner.post(path + "/create-product-quote", data={
            "csrf": "wrong", "revision": "1"}).status_code, 403)
        self.assertEqual(self.owner.post(path + "/create-product-quote", data={
            "csrf": "owner-fixture", "revision": "0"}).status_code, 409)
        response = self.owner.post(path + "/create-product-quote", data={
            "csrf": "owner-fixture", "revision": "1"})
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True))
        self.assertEqual(self.owner.post(path + "/create-product-quote", data={
            "csrf": "owner-fixture", "revision": "1"}).status_code, 303)
        self.login_customer(self.mine, self.cid)
        self.login_customer(self.other, self.stranger)
        before = self.mine.get("/customer/basket-requests")
        self.assertNotIn(b"Product-only HQ quote approved", before.data)
        self.assertNotIn(self.ident.encode(), self.other.get("/customer/basket-requests").data)
        self.assertEqual(self.owner.post(path + "/product-quote-status", data={
            "csrf": "bad", "revision": "0", "status": "approved"}).status_code, 403)
        self.assertEqual(self.owner.post(path + "/product-quote-status", data={
            "csrf": "owner-fixture", "revision": "0", "status": "approved"
        }).status_code, 303)
        self.assertEqual(change_product_quote_status(self.ident, 0, "approved"), 1)
        mine = self.mine.get("/customer/basket-requests").data
        self.assertIn(b"Product-only HQ quote approved", mine)
        self.assertIn(b"25.00", mine)
        self.assertNotIn(b"8.00", mine)  # private product tariff
        self.assertNotIn(b"4.00", mine)  # private product cost
        self.assertNotIn(self.ident.encode(), self.other.get("/customer/basket-requests").data)
        self.assertEqual(self.owner.post(path + "/product-quote-status", data={
            "csrf": "owner-fixture", "revision": "0", "status": "withdrawn"
        }).status_code, 409)
        self.assertEqual(change_product_quote_status(self.ident, 1, "withdrawn"), 2)
        self.assertIn(b"quotation withdrawn",
                      self.mine.get("/customer/basket-requests").data)
        with self.assertRaisesRegex(ValueError, "cannot be reopened"):
            change_product_quote_status(self.ident, 2, "approved")
        with connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_combined_product_quote_events"
            ).fetchone()[0], 3)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)
        with commerce_db() as db:
            self.assertEqual(db.execute(
                "SELECT stock FROM products WHERE id=?", (self.pid,)).fetchone()[0], 6)


if __name__ == "__main__":
    unittest.main()
