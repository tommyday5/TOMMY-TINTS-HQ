"""Private approved quote PDFs: owner/customer routes, reconciled scope and privacy."""
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
from hq_core_records import _schema, change_work_status
from hq_customer_portal import issue_customer_invite
from hq_quote_pdf import approved_quote_snapshot, render_quote_pdf, QuoteNotPrintable
from hq_service_quotes import (create_request, review_request, save_service_price,
                               convert_approved_service_quote, linked_quote_lines,
                               save_linked_quote_lines)

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("Bodywork & Paint", "Window Tinting")


class ApprovedQuotePdfTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": temp.name, "HQ_SESSION_SECRET": "synthetic-pdf-secret",
            "GOOGLE_CLIENT_ID": "synthetic", "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner = app.test_client()
        self.customer = app.test_client()
        self.other = app.test_client()
        self.a, self.b = uuid.uuid4().hex, uuid.uuid4().hex
        self.va, self.vb = uuid.uuid4().hex, uuid.uuid4().hex
        with connect() as db:
            _schema(db)
            for customer, vehicle, name in (
                (self.a, self.va, "Synthetic A"),
                (self.b, self.vb, "Synthetic B"),
            ):
                db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                           (customer, None, name, "synthetic@example.invalid",
                            "07700 900000", "2026-11-22"))
                db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                           (vehicle, customer, "Test", "Vehicle", "", "TEST123",
                            "2026-11-22"))
        for name in SERVICES:
            save_service_price(name, "100.00", "90.00", "80.00", True, 0)

    def owner_login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="owner-csrf", google_sub="synthetic-owner")

    def customer_login(self, client, ident):
        code = issue_customer_invite(ident, uuid.uuid4().hex)
        with client.session_transaction() as sess:
            sess["customer_portal_login_csrf"] = "synthetic-csrf"
        response = client.post("/customer/login", data={
            "csrf": "synthetic-csrf", "code": code})
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True))

    def prepare(self, *, allocate=True, approve=True):
        ident = create_request(self.a, uuid.uuid4().hex, self.va, "trade",
                               list(SERVICES), "PRIVATE COST 99999 GBP")
        self.assertEqual(review_request(ident, 0, "quoted", "175.00",
                                        "Approved vehicle work."), 1)
        work_id = convert_approved_service_quote(ident, 1, "retail")
        revision = linked_quote_lines(ident)["work_revision"]
        if allocate:
            revision = save_linked_quote_lines(ident, revision, [
                {"service": SERVICES[0], "price": "100.00",
                 "description": "PRIVATE MARGIN 44444"},
                {"service": SERVICES[1], "price": "75.00",
                 "description": "INTERNAL SUBCONTRACTOR 55555"},
            ])
        if approve:
            change_work_status(work_id, "approved", expected_revision=revision)
        return ident, work_id

    def test_public_approved_pdf_and_owner_pdf_share_canonical_total(self):
        ident, work_id = self.prepare()
        snap = approved_quote_snapshot(ident, customer_id=self.a)
        self.assertEqual(snap["total_pence"], 17500)
        self.assertEqual([x["pence"] for x in snap["services"]], [10000, 7500])
        self.assertNotIn("price_pence", snap)
        self.assertNotIn("owner_prices_json", str(snap))
        self.assertNotIn("PRIVATE MARGIN", str(snap))
        self.assertNotIn("PRIVATE COST", str(snap))
        data = render_quote_pdf(snap)
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertGreater(len(data), 1000)

        self.assertEqual(self.owner.get(f"/hq/service-enquiries/{ident}/quote.pdf").status_code, 302)
        self.owner_login()
        owner_pdf = self.owner.get(f"/hq/service-enquiries/{ident}/quote.pdf")
        self.assertEqual(owner_pdf.status_code, 200)
        self.assertEqual(owner_pdf.mimetype, "application/pdf")
        self.assertTrue(owner_pdf.data.startswith(b"%PDF-"))
        self.assertEqual(owner_pdf.headers["Cache-Control"], "private, no-store")
        self.assertEqual(owner_pdf.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("attachment", owner_pdf.headers["Content-Disposition"])

        self.customer_login(self.customer, self.a)
        self.customer_login(self.other, self.b)
        portal_pdf = self.customer.get(f"/customer/service-enquiries/{ident}/quote.pdf")
        self.assertEqual(portal_pdf.status_code, 200)
        self.assertTrue(portal_pdf.data.startswith(b"%PDF-"))
        self.assertIn(("/customer/service-enquiries/" + ident + "/quote.pdf").encode(),
                      self.customer.get("/customer/service-enquiries").data)
        self.assertEqual(self.other.get(
            f"/customer/service-enquiries/{ident}/quote.pdf").status_code, 404)
        self.assertEqual(self.other.get("/customer/service-enquiries").status_code, 200)

    def test_no_draft_unbalanced_or_mismatched_pdf_exposure(self):
        ident, work_id = self.prepare(allocate=False, approve=False)
        self.owner_login()
        self.customer_login(self.customer, self.a)
        self.assertEqual(self.owner.get(
            f"/hq/service-enquiries/{ident}/quote.pdf").status_code, 409)
        self.assertEqual(self.customer.get(
            f"/customer/service-enquiries/{ident}/quote.pdf").status_code, 404)
        change_work_status(work_id, "approved", expected_revision=0)
        self.assertEqual(self.owner.get(
            f"/hq/service-enquiries/{ident}/quote.pdf").status_code, 409)
        self.assertNotIn(("/customer/service-enquiries/" + ident + "/quote.pdf").encode(),
                         self.customer.get("/customer/service-enquiries").data)
        with connect() as db:
            db.execute("UPDATE hq_work_records SET price_pence=17600 WHERE id=?",
                       (work_id,))
        self.assertEqual(self.customer.get(
            f"/customer/service-enquiries/{ident}/quote.pdf").status_code, 404)
        with self.assertRaises(QuoteNotPrintable):
            approved_quote_snapshot(ident, customer_id=self.a)

    def test_invalid_ids_and_anonymous_customer_fail_closed(self):
        self.assertEqual(self.customer.get(
            "/customer/service-enquiries/" + uuid.uuid4().hex + "/quote.pdf"
        ).status_code, 302)
        self.customer_login(self.customer, self.a)
        self.assertEqual(self.customer.get(
            "/customer/service-enquiries/not-a-uuid/quote.pdf").status_code, 404)
        self.owner_login()
        self.assertEqual(self.owner.get(
            "/hq/service-enquiries/not-a-uuid/quote.pdf").status_code, 404)


if __name__ == "__main__":
    unittest.main()
