"""Combined service/product/delivery quote price allocations and PDF privacy.

Entirely synthetic: uses temporary SQLite and no external bookings, money,
inventory mutations, messages or Google Sheets writes.
"""
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
from hq_commerce import database as commerce_db, save_product
from hq_combined_baskets import submit_combined, review_combined, convert_approved_combined
from hq_combined_quote_lines import (
    combined_quote_details, save_combined_quote_lines,
    approved_combined_snapshot, QuoteNotPrintable,
)
from hq_core_records import _schema, change_work_status, StaleWorkRecordError
from hq_customer_portal import issue_customer_invite
from hq_service_quotes import save_service_price

ROOT = Path(__file__).resolve().parents[1]


class CombinedQuoteLinesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": temp.name, "HQ_SESSION_SECRET": "synthetic-combined-lines-secret",
            "GOOGLE_CLIENT_ID": "synthetic", "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner, self.customer, self.other = (
            app.test_client() for _ in range(3))
        self.a, self.b, self.va = (uuid.uuid4().hex for _ in range(3))
        with connect() as db:
            _schema(db)
            for cid, name in ((self.a, "Fixture Alpha"), (self.b, "Fixture Beta")):
                db.execute("INSERT INTO hq_customers VALUES (?,?,?,?,?,?)",
                           (cid, None, name, "test@example.invalid", "07700 900000",
                            "2026-11-23"))
            db.execute("INSERT INTO hq_vehicles VALUES (?,?,?,?,?,?,?)",
                       (self.va, self.a, "Hyundai", "i30", "2018", "XX00 TST", "2026-11-23"))
        self.service = "Window Tinting"
        save_service_price(self.service, "120.00", "90.00", "80.00", True, 0)
        self.product = save_product({
            "name": "Test film", "variant": "smoke", "description": "fixture",
            "retail": 1599, "trade": 750, "cost": 400, "stock": 3,
            "delivery": 250, "published": 1})
        self.ident = submit_combined(
            self.a, uuid.uuid4().hex, self.va, "trade",
            [self.service], [{"id": self.product, "quantity": 2}],
            "delivery", "OWNER COST SECRET 98765")
        review_combined(self.ident, 0, "quoted", "149.00", "Customer-approved quote")
        self.work_id = convert_approved_combined(self.ident, 1, "retail")
        self.path = f"/hq/combined-enquiries/{self.ident}/line-prices"
        self.owner_pdf = f"/hq/combined-enquiries/{self.ident}/quote.pdf"
        self.customer_pdf = f"/customer/basket-requests/{self.ident}/quote.pdf"

    def owner_login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="owner-csrf", google_sub="synthetic-owner")

    def customer_login(self, client, customer_id):
        code = issue_customer_invite(customer_id, uuid.uuid4().hex)
        with client.session_transaction() as sess:
            sess["customer_portal_login_csrf"] = "customer-csrf"
        response = client.post("/customer/login", data={
            "csrf": "customer-csrf", "code": code})
        self.assertEqual(response.status_code, 303)

    def entries(self, prices=("110.00", "36.50", "2.50")):
        detail = combined_quote_details(self.ident)
        return [{"line_type": line["line_type"], "source_ref": line["source_ref"],
                 "description": "PRIVATE MARGIN 54321", "price": price}
                for line, price in zip(detail["lines"], prices)]

    def test_atomic_itemised_lines_reload_revision_idempotency_and_stock(self):
        original = combined_quote_details(self.ident)
        self.assertEqual([x["line_type"] for x in original["lines"]],
                         ["service", "product", "delivery"])
        self.assertFalse(original["fully_allocated"])
        proposed = self.entries()
        revision = save_combined_quote_lines(self.ident, 0, proposed)
        self.assertEqual(revision, 1)
        self.assertEqual(save_combined_quote_lines(self.ident, 0, proposed), 1)
        reopened = combined_quote_details(self.ident)
        self.assertTrue(reopened["fully_allocated"])
        self.assertEqual(reopened["work_revision"], 1)
        self.assertEqual([x["amount_pence"] for x in reopened["lines"]],
                         [11000, 3650, 250])
        self.assertEqual(reopened["lines"][1]["quantity"], 2)
        with connect() as db:
            self.assertEqual(db.execute("""SELECT COUNT(*) FROM hq_combined_quote_lines
                WHERE work_id=?""", (self.work_id,)).fetchone()[0], 3)
            self.assertEqual(db.execute("""SELECT COUNT(*) FROM hq_work_audit
                WHERE work_id=? AND action='combined_line_allocation'""",
                (self.work_id,)).fetchone()[0], 1)
        with commerce_db() as db:
            self.assertEqual(db.execute(
                "SELECT stock FROM products WHERE id=?", (self.product,)).fetchone()[0], 3)

        with self.assertRaisesRegex(StaleWorkRecordError, "reload"):
            save_combined_quote_lines(
                self.ident, 0, self.entries(("105.00", "41.50", "2.50")))
        self.assertEqual(combined_quote_details(self.ident)["work_revision"], 1)
        with self.assertRaisesRegex(ValueError, "Source service"):
            altered = self.entries()
            altered[0]["source_ref"] = "Another service"
            save_combined_quote_lines(self.ident, 1, altered)

    def test_price_integrity_and_private_owner_editor(self):
        with self.assertRaisesRegex(ValueError, "reconcile"):
            save_combined_quote_lines(self.ident, 0,
                                      self.entries(("110.00", "30.00", "2.50")))
        with self.assertRaisesRegex(ValueError, "price"):
            save_combined_quote_lines(self.ident, 0,
                                      self.entries(("110.00", "-1.00", "40.00")))
        self.assertEqual(self.owner.get(self.path).status_code, 302)
        self.assertEqual(self.owner.get(self.owner_pdf).status_code, 302)
        self.owner_login()
        self.assertIn(b"Allocate combined", self.owner.get(self.path).data)
        form = {"csrf": "owner-csrf", "revision": "0",
                "price_0": "110.00", "price_1": "36.50", "price_2": "2.50",
                "description_0": "Owner note", "description_1": "Secret materials"}
        self.assertEqual(self.owner.post(self.path, data={**form, "csrf": "invalid"}).status_code, 403)
        self.assertEqual(self.owner.post(self.path, data={
            **form, "revision": "bad"}).status_code, 400)
        saved = self.owner.post(self.path, data=form)
        self.assertEqual(saved.status_code, 303, saved.get_data(as_text=True))
        self.assertEqual(self.owner.get(self.path).status_code, 200)
        self.assertIn(b"110.00", self.owner.get(self.path).data)
        self.assertEqual(self.owner.get(self.owner_pdf).status_code, 409)
        self.assertEqual(self.customer.get(self.customer_pdf).status_code, 302)

    def test_approved_pdf_requires_complete_reconciled_scope_and_correct_customer(self):
        self.owner_login()
        self.customer_login(self.customer, self.a)
        self.customer_login(self.other, self.b)
        self.assertEqual(self.customer.get(self.customer_pdf).status_code, 404)
        self.assertNotIn(self.customer_pdf.encode(),
                         self.customer.get("/customer/basket-requests").data)
        self.assertEqual(self.owner.get(self.owner_pdf).status_code, 409)
        save_combined_quote_lines(self.ident, 0, self.entries())
        self.assertEqual(self.customer.get(self.customer_pdf).status_code, 404)
        change_work_status(self.work_id, "approved", expected_revision=1)
        snapshot = approved_combined_snapshot(self.ident, customer_id=self.a)
        self.assertEqual(snapshot["total_pence"], 14900)
        self.assertEqual([x["pence"] for x in snapshot["services"]],
                         [11000, 3650, 250])
        self.assertIn("2 ×", snapshot["services"][1]["name"])
        self.assertNotIn("PRIVATE MARGIN", str(snapshot))
        self.assertNotIn("OWNER COST", str(snapshot))
        pdf = self.customer.get(self.customer_pdf)
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF-"))
        self.assertEqual(pdf.headers["Cache-Control"], "private, no-store")
        self.assertIn(self.customer_pdf.encode(), self.customer.get(
            "/customer/basket-requests").data)
        self.assertEqual(self.owner.get(self.owner_pdf).status_code, 200)
        self.assertEqual(self.other.get(self.customer_pdf).status_code, 404)
        self.assertNotIn(self.ident.encode(),
                         self.other.get("/customer/basket-requests").data)
        with self.assertRaisesRegex(ValueError, "unapproved"):
            save_combined_quote_lines(self.ident, 2,
                self.entries(("111.00", "35.50", "2.50")))
        with connect() as db:
            db.execute("UPDATE hq_work_records SET price_pence=14901 WHERE id=?",
                       (self.work_id,))
        self.assertEqual(self.owner.get(self.owner_pdf).status_code, 409)
        self.assertEqual(self.customer.get(self.customer_pdf).status_code, 404)
        with self.assertRaises(QuoteNotPrintable):
            approved_combined_snapshot(self.ident, customer_id=self.a)


if __name__ == "__main__":
    unittest.main()
