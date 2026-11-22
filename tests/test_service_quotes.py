"""Synthetic end-to-end native service pricing and customer-to-HQ quote tests.

Never posts to Sheets, Calendar, Square or payments.
"""
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import _schema, change_work_status, read_work_record
from hq_native_work import native_work_record
from hq_job_sync import status as sheet_sync_status
from hq_customer_portal import issue_customer_invite
from hq_service_quotes import (save_service_price, catalogue, create_request,
                               review_request, customer_requests, owner_requests,
                               convert_approved_service_quote)

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "Window Tinting"


class ServiceQuoteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        environment = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name, "HQ_SESSION_SECRET": "synthetic-service-only",
            "GOOGLE_CLIENT_ID": "fixture", "GOOGLE_CLIENT_SECRET": "fixture",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.customer_a, self.customer_b = uuid.uuid4().hex, uuid.uuid4().hex
        self.vehicle_a, self.vehicle_b = uuid.uuid4().hex, uuid.uuid4().hex
        self.customer_client = app.test_client()
        self.other_client = app.test_client()
        self.owner_client = app.test_client()
        with connect() as db:
            _schema(db)
            for ident, name, vehicle in (
                (self.customer_a, "Synthetic A", self.vehicle_a),
                (self.customer_b, "Synthetic B", self.vehicle_b),
            ):
                db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                           (ident, None, name, "test@example.invalid",
                            "07700 900000", "2026-11-22"))
                db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                           (vehicle, ident, "Test", "Car", "", "TEST123", "2026-11-22"))
        save_service_price(SERVICE, "120.00", "90.00", "80.00", True, 0)

    def login_customer(self, client, ident):
        token = issue_customer_invite(ident, uuid.uuid4().hex)
        with client.session_transaction() as sess:
            sess["customer_portal_login_csrf"] = "test-login"
        response = client.post("/customer/login",
                               data={"csrf": "test-login", "code": token})
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True))
        with client.session_transaction() as sess:
            return sess["customer_portal_csrf"]

    def login_owner(self):
        with self.owner_client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="owner-csrf", google_sub="synthetic-owner")

    def test_owner_tariff_edit_archive_conflicts_and_private_fields(self):
        self.assertEqual(catalogue()[[s["service"] for s in catalogue()].index(SERVICE)]["revision"], 1)
        self.assertEqual(save_service_price(SERVICE, "120.00", "90.00",
                                            "80.00", True, 0), 1)  # replay
        with self.assertRaisesRegex(ValueError, "changed elsewhere"):
            save_service_price(SERVICE, "125.00", "90.00", "80.00", True, 0)
        self.assertEqual(save_service_price(SERVICE, "125.00", "90.00",
                                            "", False, 1), 2)
        with self.assertRaisesRegex(ValueError, "retail tariff"):
            save_service_price(SERVICE, "", "", "", True, 2)
        self.assertEqual(save_service_price(SERVICE, "125.00", "90.00",
                                            "", True, 2), 3)
        self.assertEqual(self.customer_client.get("/hq/services").status_code, 302)
        self.login_owner()
        response = self.owner_client.get("/hq/services")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"90.00", response.data)
        self.assertEqual(self.owner_client.post("/hq/services", data={
            "service": SERVICE, "revision": 3, "retail": "130.00",
            "trade": "90.00", "maintenance": "", "published": "1",
        }).status_code, 403)
        saved = self.owner_client.post("/hq/services", data={
            "csrf": "owner-csrf", "service": SERVICE, "revision": 3,
            "retail": "130.00", "trade": "90.00", "maintenance": "", "published": "1",
        })
        self.assertEqual(saved.status_code, 303)

    def test_private_canonical_vehicle_replay_and_no_automatic_tier_grant(self):
        operation = uuid.uuid4().hex
        ident = create_request(self.customer_a, operation, self.vehicle_a, "trade",
                               [SERVICE], "Check sun strip")
        self.assertEqual(create_request(self.customer_a, operation, self.vehicle_a,
                                        "trade", [SERVICE], "Check sun strip"), ident)
        with self.assertRaisesRegex(ValueError, "different details"):
            create_request(self.customer_a, operation, self.vehicle_a, "retail",
                           [SERVICE], "Check sun strip")
        with self.assertRaisesRegex(ValueError, "Vehicle is not linked"):
            create_request(self.customer_a, uuid.uuid4().hex, self.vehicle_b,
                           "retail", [SERVICE], "")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            create_request(self.customer_a, uuid.uuid4().hex, self.vehicle_a,
                           "retail", [SERVICE, SERVICE], "")
        with connect() as db:
            row = db.execute("SELECT customer_id,vehicle_id,status FROM hq_service_quote_requests WHERE id=?",
                             (ident,)).fetchone()
            self.assertEqual((row["customer_id"], row["vehicle_id"], row["status"]),
                             (self.customer_a, self.vehicle_a, "submitted"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_service_quote_audit").fetchone()[0], 1)
        view = customer_requests(self.customer_a)
        self.assertEqual(len(view), 1)
        self.assertEqual(customer_requests(self.customer_b), [])
        self.assertNotIn("owner_prices_json", view[0])
        self.assertEqual(owner_requests()[0]["owner_prices"][SERVICE], 9000)
        self.assertEqual(review_request(ident, 0, "quoted", "135.00", "Owner approved"), 1)
        self.assertEqual(review_request(ident, 0, "quoted", "135.00", "Owner approved"), 1)
        with self.assertRaisesRegex(ValueError, "changed"):
            review_request(ident, 0, "quoted", "140.00", "Owner approved")

    def test_browser_customer_to_hq_review_with_customer_isolation(self):
        self.assertEqual(self.customer_client.get("/book/services").status_code, 200)
        self.assertIn(b"120.00", self.customer_client.get("/book/services").data)
        self.assertNotIn(b"90.00", self.customer_client.get("/book/services").data)
        self.assertEqual(self.customer_client.get("/customer/service-enquiries").status_code, 302)
        csrf = self.login_customer(self.customer_client, self.customer_a)
        operation = uuid.uuid4().hex
        form = {"csrf": csrf, "operation_id": operation,
                "vehicle_id": self.vehicle_a, "requested_tier": "maintenance",
                "services": SERVICE, "notes": "Synthetic vehicle check"}
        forbidden = self.customer_client.post("/book/services", data={**form, "csrf": "bad"})
        self.assertEqual(forbidden.status_code, 403)
        saved = self.customer_client.post("/book/services", data=form)
        self.assertEqual(saved.status_code, 303, saved.get_data(as_text=True))
        ident = customer_requests(self.customer_a)[0]["id"]
        self.assertEqual(self.customer_client.post("/book/services", data=form).status_code, 303)
        self.assertEqual(len(customer_requests(self.customer_a)), 1)
        self.assertIn(b"Window Tinting", self.customer_client.get("/customer/service-enquiries").data)
        self.assertNotIn(b"80.00", self.customer_client.get("/customer/service-enquiries").data)
        self.login_customer(self.other_client, self.customer_b)
        other = self.other_client.get("/customer/service-enquiries")
        self.assertEqual(other.status_code, 200)
        self.assertNotIn(ident.encode(), other.data)
        self.login_owner()
        self.assertEqual(self.owner_client.get("/hq/service-enquiries").status_code, 200)
        bad = self.owner_client.post("/hq/service-enquiries", data={
            "request_id": ident, "revision": "0", "status": "quoted",
            "approved_pounds": "150.00", "public_note": "Confirmed price",
        })
        self.assertEqual(bad.status_code, 403)
        approved = self.owner_client.post("/hq/service-enquiries", data={
            "csrf": "owner-csrf", "request_id": ident, "revision": "0",
            "status": "quoted", "approved_pounds": "150.00",
            "public_note": "Owner-reviewed only; not booked",
        })
        self.assertEqual(approved.status_code, 303)
        own = self.customer_client.get("/customer/service-enquiries")
        self.assertIn(b"150.00", own.data)
        self.assertIn(b"Owner-reviewed only", own.data)
        self.assertNotIn(b"80.00", own.data)
        self.assertNotIn(b"150.00", self.other_client.get("/customer/service-enquiries").data)
        with connect() as db:
            rows = db.execute("SELECT action FROM hq_service_quote_audit WHERE request_id=? ORDER BY revision",
                              (ident,)).fetchall()
            self.assertEqual([r[0] for r in rows], ["submitted", "quoted"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)


    def test_owner_explicit_conversion_to_native_quote_and_customer_approval_status(self):
        save_service_price("Bodywork & Paint", "270.00", "", "", True, 0)
        ident = create_request(self.customer_a, uuid.uuid4().hex, self.vehicle_a,
                               "trade", [SERVICE, "Bodywork & Paint"], "Multi-panel repair")
        with self.assertRaisesRegex(ValueError, "owner-reviewed"):
            convert_approved_service_quote(ident, 0, "trade")
        review_request(ident, 0, "quoted", "350.00", "Original owner quote")
        self.login_owner()
        path = "/hq/service-enquiries/" + ident + "/create-work"
        self.assertEqual(self.customer_client.post(path, data={
            "csrf": "owner-csrf", "revision": "1", "customer_tier": "trade"
        }).status_code, 302)
        self.assertEqual(self.owner_client.post(path, data={
            "revision": "1", "customer_tier": "trade"
        }).status_code, 403)
        self.assertEqual(self.owner_client.post(path, data={
            "csrf": "owner-csrf", "revision": "0", "customer_tier": "trade"
        }).status_code, 409)
        self.assertEqual(self.owner_client.post(path, data={
            "csrf": "owner-csrf", "revision": "1", "customer_tier": "owner"
        }).status_code, 400)
        created = self.owner_client.post(path, data={
            "csrf": "owner-csrf", "revision": "1", "customer_tier": "retail"
        })
        self.assertEqual(created.status_code, 303, created.get_data(as_text=True))
        work_id = created.headers["Location"].split("/")[3]
        work = native_work_record(work_id)
        self.assertEqual((work["customer_id"], work["vehicle_id"]),
                         (self.customer_a, self.vehicle_a))
        self.assertEqual((work["kind"], work["status"], work["customer_tier"]),
                         ("quote", "ready_for_review", "retail"))
        self.assertEqual(work["price_pence"], 35000)
        self.assertEqual(work["source_request_id"], "HQ-" + work_id)
        self.assertIn("Bodywork & Paint", work["notes"])
        self.assertIn(SERVICE, work["notes"])
        self.assertIn("Multi-panel repair", work["notes"])
        self.assertEqual(sheet_sync_status(work_id)["state"], "not_linked")
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_customers").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_vehicles").fetchone()[0], 2)
            link = db.execute("SELECT * FROM hq_service_quote_work_links").fetchone()
            self.assertEqual(link["request_id"], ident)
            self.assertEqual(link["work_id"], work_id)
            self.assertEqual(link["approved_total_pence"], 35000)
            self.assertEqual(link["customer_tier"], "retail")
            self.assertIn("Bodywork & Paint", link["services_json"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_job_sync_outbox").fetchone()[0]
                             if db.execute("SELECT name FROM sqlite_master WHERE name='hq_job_sync_outbox'").fetchone()
                             else 0, 0)
        self.assertEqual(self.owner_client.get(created.headers["Location"]).status_code, 200)
        self.assertIn(work_id.encode(), self.owner_client.get("/hq/service-enquiries").data)
        self.assertIn(b"Edit and approve it in HQ", self.owner_client.get("/hq/service-enquiries").data)
        self.assertEqual(convert_approved_service_quote(ident, 1, "retail"), work_id)
        self.assertEqual(self.owner_client.post(path, data={
            "csrf": "owner-csrf", "revision": "1", "customer_tier": "retail"
        }).headers["Location"], created.headers["Location"])
        with self.assertRaisesRegex(ValueError, "already has"):
            review_request(ident, 1, "quoted", "400.00", "Changed price")
        with self.assertRaisesRegex(ValueError, "already exists"):
            convert_approved_service_quote(ident, 1, "trade")

        self.login_customer(self.customer_client, self.customer_a)
        self.login_customer(self.other_client, self.customer_b)
        self.assertIsNone(customer_requests(self.customer_a)[0]["linked_status"])
        self.assertNotIn(b"HQ work status", self.customer_client.get("/customer/service-enquiries").data)
        change_work_status(work_id, "approved", expected_revision=0)
        self.assertEqual(read_work_record(work_id)["kind"], "job")
        own = self.customer_client.get("/customer/service-enquiries")
        self.assertIn(b"HQ work status: Approved", own.data)
        self.assertIn(b"Current owner-approved work amount", own.data)
        self.assertNotIn(ident.encode(), self.other_client.get("/customer/service-enquiries").data)
        self.assertEqual(sheet_sync_status(work_id)["state"], "not_linked")

    def test_parallel_conversion_replays_one_work_record(self):
        ident = create_request(self.customer_a, uuid.uuid4().hex, self.vehicle_a,
                               "retail", [SERVICE], "Synthetic enquiry")
        review_request(ident, 0, "quoted", "140.00", "Reviewed")
        with ThreadPoolExecutor(max_workers=3) as pool:
            ids = list(pool.map(lambda _: convert_approved_service_quote(
                ident, 1, "retail"), range(3)))
        self.assertEqual(len(set(ids)), 1)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_service_quote_work_links").fetchone()[0], 1)
            self.assertEqual(db.execute("""SELECT COUNT(*) FROM hq_service_quote_audit
                WHERE action='native_quote_created'""").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
