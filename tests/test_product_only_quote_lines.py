"""End-to-end synthetic product-only quote allocations and customer-safe PDFs."""
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
from hq_combined_baskets import (submit_combined, review_combined,
                                  create_product_only_quote,
                                  change_product_quote_status)
from hq_commerce import database as commerce_db, save_product
from hq_core_records import _schema, StaleWorkRecordError
from hq_customer_portal import issue_customer_invite
from hq_product_quote_lines import (product_quote_details, save_product_quote_lines,
                                     approved_product_quote_snapshot)
from hq_quote_pdf import QuoteNotPrintable

ROOT = Path(__file__).resolve().parents[1]


class ProductOnlyQuoteLinesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name, "HQ_SESSION_SECRET": "synthetic-product-lines",
            "GOOGLE_CLIENT_ID": "fixture", "GOOGLE_CLIENT_SECRET": "fixture",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner, self.mine, self.other = (app.test_client() for _ in range(3))
        self.customer_id, self.stranger = uuid.uuid4().hex, uuid.uuid4().hex
        with connect() as db:
            _schema(db)
            for ident, name in ((self.customer_id, "Sample Product Buyer"),
                                (self.stranger, "Different Sample Buyer")):
                db.execute("INSERT INTO hq_customers VALUES (?,?,?,?,?,?)",
                           (ident, None, name, "test@example.invalid", "07000000000",
                            "2026-11-23"))
        self.product = save_product({
            "name": "Synthetic Window Film", "variant": "Dark", "description": "Test",
            "retail": 3000, "trade": 700, "cost": 300, "stock": 6,
            "delivery": 500, "published": 1})
        self.basket = submit_combined(
            self.customer_id, uuid.uuid4().hex, None, "trade", [],
            [{"id": self.product, "quantity": 2}], "delivery")
        review_combined(self.basket, 0, "quoted", "70.00", "Please review before payment")
        self.quote_id = create_product_only_quote(self.basket, 1)

    def login_owner(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="owner-token", google_sub="synthetic-owner")

    def login_customer(self, client, ident):
        code = issue_customer_invite(ident, uuid.uuid4().hex)
        with client.session_transaction() as sess:
            sess["customer_portal_login_csrf"] = "login-token"
        response = client.post("/customer/login", data={"csrf": "login-token", "code": code})
        self.assertEqual(response.status_code, 303)

    def test_durable_revisions_exact_totals_and_approved_pdf(self):
        path = "/hq/combined-enquiries/" + self.basket + "/product-line-prices"
        self.assertEqual(self.owner.get(path).status_code, 302)
        self.login_owner()
        self.assertEqual(self.owner.get(path).status_code, 200)
        self.assertEqual(self.owner.post(path, data={
            "csrf": "wrong", "revision": "0", "price_0": "65", "price_1": "5"
        }).status_code, 403)
        self.assertEqual(self.owner.post(path, data={
            "csrf": "owner-token", "revision": "0", "price_0": "1", "price_1": "5"
        }).status_code, 409)
        self.assertEqual(self.owner.post(path, data={
            "csrf": "owner-token", "revision": "0", "price_0": "65.00", "price_1": "5.00"
        }).status_code, 303)
        self.assertEqual(product_quote_details(self.basket)["revision"], 1)
        self.assertTrue(product_quote_details(self.basket)["fully_allocated"])
        self.assertEqual(save_product_quote_lines(self.basket, 0, ["65", "5"]), 1)
        with self.assertRaises(StaleWorkRecordError):
            save_product_quote_lines(self.basket, 0, ["60", "10"])
        self.assertEqual(save_product_quote_lines(self.basket, 1, ["60", "10"]), 2)
        with self.assertRaises(QuoteNotPrintable):
            approved_product_quote_snapshot(self.basket)
        self.assertEqual(change_product_quote_status(self.basket, 2, "approved"), 3)
        with self.assertRaisesRegex(ValueError, "draft"):
            save_product_quote_lines(self.basket, 3, ["65", "5"])
        snap = approved_product_quote_snapshot(self.basket, customer_id=self.customer_id)
        self.assertEqual([line["pence"] for line in snap["services"]], [6000, 1000])
        self.assertEqual(snap["total_pence"], 7000)
        self.assertNotIn("cost", repr(snap))
        self.assertNotIn("trade", repr(snap))
        self.login_customer(self.mine, self.customer_id)
        self.login_customer(self.other, self.stranger)
        pdf = self.mine.get("/customer/basket-requests/" + self.basket + "/product-quote.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF"))
        self.assertIn("no-store", pdf.headers["Cache-Control"])
        self.assertEqual(self.other.get(
            "/customer/basket-requests/" + self.basket + "/product-quote.pdf"
        ).status_code, 404)
        self.assertIn(b"Download itemised approved products quotation",
                      self.mine.get("/customer/basket-requests").data)
        with connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_product_quote_line_audit"
            ).fetchone()[0], 2)
        with commerce_db() as db:
            self.assertEqual(db.execute(
                "SELECT stock FROM products WHERE id=?", (self.product,)).fetchone()[0], 6)

    def test_missing_allocations_not_printable_and_invalid_inputs(self):
        self.login_owner()
        owner_pdf = "/hq/combined-enquiries/" + self.basket + "/product-quote.pdf"
        self.assertEqual(self.owner.get(owner_pdf).status_code, 409)
        with self.assertRaisesRegex(ValueError, "exactly equal"):
            save_product_quote_lines(self.basket, 0, ["70", "5"])
        with self.assertRaisesRegex(ValueError, "at most two"):
            save_product_quote_lines(self.basket, 0, ["65.999", "5"])
        self.assertEqual(product_quote_details(self.basket)["revision"], 0)
        change_product_quote_status(self.basket, 0, "approved")
        self.assertEqual(self.owner.get(owner_pdf).status_code, 409)


if __name__ == "__main__":
    unittest.main()
