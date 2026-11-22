"""Synthetic portal → native HQ availability inbox → tentative customer offer."""
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import change_work_status, set_draft_financials
from hq_customer_portal import issue_customer_invite
from hq_native_work import create_native_work, native_work_record
from hq_appointment_requests import (
    submit_availability_request, customer_availability_snapshot,
    owner_availability_snapshot, owner_decide_availability, withdraw_availability_request
)

ROOT = Path(__file__).resolve().parents[1]


class AvailabilityRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.tmp.name,
            "HQ_SESSION_SECRET": "synthetic-availability-secret",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
            "GOOGLE_CLIENT_ID": "synthetic", "GOOGLE_CLIENT_SECRET": "synthetic",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner = app.test_client()
        self.customer = app.test_client()
        self.other = app.test_client()
        self.work_id = self.create_work("Portal Person", "person@example.invalid", "AA21 AAA")
        self.customer_id = native_work_record(self.work_id)["customer_id"]
        self.other_work = self.create_work("Other Customer", "other@example.invalid", "BB21 BBB")
        self.other_id = native_work_record(self.other_work)["customer_id"]
        self.day = (datetime.now(ZoneInfo("Europe/London")).date()+timedelta(days=10)).isoformat()
        self.next_day = (datetime.now(ZoneInfo("Europe/London")).date()+timedelta(days=11)).isoformat()
        self.approve(self.work_id)

    def create_work(self, name, email, reg):
        with app.app_context():
            return create_native_work({
                "operation_id":uuid.uuid4().hex,"customer_id":"","vehicle_id":"",
                "customer_name":name,"email":email,
                "phone":"07700 900111" if reg=="AA21 AAA" else "07700 900112",
                "make":"Hyundai","model":"i30","model_year":"2021",
                "registration":reg,"kind":"quote","service":"Bodywork & Paint",
                "customer_tier":"retail","notes":"PRIVATE COST DATA 9000",
                "preferred_date":"","preferred_time":"",
            })

    def approve(self, work_id):
        set_draft_financials(work_id,35000,0,expected_revision=0)
        change_work_status(work_id,"ready_for_review",expected_revision=1)
        change_work_status(work_id,"approved",expected_revision=2)

    def owner_login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db,"synthetic-owner"),
                        csrf="availability-owner-csrf",google_sub="synthetic-owner")

    def signin(self, client, customer_id):
        code = issue_customer_invite(customer_id,uuid.uuid4().hex)
        self.assertEqual(client.get("/customer/login").status_code,200)
        with client.session_transaction() as sess:
            csrf = sess["customer_portal_login_csrf"]
        response=client.post("/customer/login",data={"csrf":csrf,"code":code})
        self.assertEqual(response.status_code,303)
        with client.session_transaction() as sess:
            return sess["customer_portal_csrf"]

    def payload(self, csrf, operation_id=None, **extras):
        data={"csrf":csrf,"action":"request",
              "work_id":self.work_id,"operation_id":operation_id or uuid.uuid4().hex,
              "date":self.day,"time":"11:00",
              "note":"Please check before promising a slot"}
        data.update(extras)
        return data

    def test_private_customer_to_owner_tentative_offer_and_withdrawal(self):
        self.assertEqual(self.customer.get("/customer/availability").status_code,302)
        self.assertEqual(self.owner.get("/hq/availability-requests").status_code,302)
        self.owner_login()
        customer_csrf=self.signin(self.customer,self.customer_id)
        other_csrf=self.signin(self.other,self.other_id)
        self.assertIn(b"/customer/availability",self.customer.get("/customer/account").data)
        page=self.customer.get("/customer/availability")
        self.assertEqual(page.status_code,200)
        self.assertIn(b"AA21 AAA",page.data)
        self.assertNotIn(b"BB21 BBB",page.data)
        self.assertNotIn(b"PRIVATE COST DATA",page.data)
        self.assertIn("no-store",page.headers["Cache-Control"])
        self.assertIn("no-referrer",page.headers["Referrer-Policy"])
        self.assertEqual(self.customer.post("/customer/availability",data=self.payload("wrong")).status_code,403)

        operation=uuid.uuid4().hex
        sent=self.payload(customer_csrf,operation_id=operation)
        self.assertEqual(self.customer.post("/customer/availability",data=sent).status_code,303)
        self.assertEqual(self.customer.post("/customer/availability",data=sent).status_code,303)
        records=owner_availability_snapshot()
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]["work_id"],self.work_id)
        self.assertEqual(records[0]["status"],"requested")
        self.assertEqual(records[0]["revision"],0)
        ref=records[0]["id"]
        self.assertEqual(len(customer_availability_snapshot(self.customer_id)[1]),1)
        self.assertEqual(len(customer_availability_snapshot(self.other_id)[1]),0)
        self.assertNotIn(ref.encode(),self.other.get("/customer/availability").data)
        self.assertEqual(self.other.post("/customer/availability",data=self.payload(other_csrf)).status_code,409)

        response=self.owner.get("/hq/availability-requests")
        self.assertEqual(response.status_code,200)
        self.assertIn(b"Portal Person",response.data)
        self.assertIn(b"Please check",response.data)
        self.assertEqual(self.owner.post("/hq/availability-requests",data={
            "csrf":"wrong","request_id":ref,"revision":"0","decision":"offered",
            "date":self.next_day,"time":"13:00",
        }).status_code,403)
        decision={"csrf":"availability-owner-csrf","request_id":ref,
                  "revision":"0","decision":"offered",
                  "date":self.next_day,"time":"13:00",
                  "message":"We can tentatively offer this date; not yet confirmed."}
        self.assertEqual(self.owner.post("/hq/availability-requests",data=decision).status_code,303)
        self.assertEqual(self.owner.post("/hq/availability-requests",data=decision).status_code,303)
        customer_page=self.customer.get("/customer/availability")
        self.assertIn(b"Tentative offer",customer_page.data)
        self.assertIn(self.next_day.encode(),customer_page.data)
        self.assertIn(b"not yet confirmed",customer_page.data)
        self.assertNotIn(b"PRIVATE COST DATA",customer_page.data)
        self.assertNotIn(self.next_day.encode(),self.other.get("/customer/availability").data)
        self.assertEqual(self.owner.post("/hq/availability-requests",data={
            **decision,"message":"Different stale answer",
        }).status_code,409)
        self.assertEqual(self.customer.post("/customer/availability",data={
            "csrf":customer_csrf,"action":"withdraw","request_id":ref,"revision":"1",
        }).status_code,303)
        self.assertEqual(owner_availability_snapshot()[0]["status"],"withdrawn")
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_portal_availability_events").fetchone()[0],3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_portal_availability_requests").fetchone()[0],1)
        self.assertNotIn(b"Confirmed booking",self.customer.get("/customer/availability").data)

    def test_validation_isolation_draft_and_revision_conflicts(self):
        self.owner_login()
        csrf=self.signin(self.customer,self.customer_id)
        other_csrf=self.signin(self.other,self.other_id)
        self.assertEqual(self.customer.post("/customer/availability",data=self.payload(
            csrf,work_id=self.other_work)).status_code,409)
        self.assertEqual(self.customer.post("/customer/availability",data=self.payload(
            csrf,date="2020-01-01")).status_code,409)
        self.assertEqual(self.customer.post("/customer/availability",data=self.payload(
            csrf,time="04:00")).status_code,409)
        self.assertEqual(self.customer.post("/customer/availability",data=self.payload(
            csrf,note="x"*501)).status_code,409)
        self.assertEqual(self.other.post("/customer/availability",data=self.payload(
            other_csrf,work_id=self.other_work)).status_code,409)  # Still draft.
        op=uuid.uuid4().hex
        self.assertEqual(self.customer.post("/customer/availability",data=self.payload(
            csrf,operation_id=op)).status_code,303)
        self.assertEqual(self.customer.post("/customer/availability",data=self.payload(
            csrf,operation_id=op,time="12:00")).status_code,409)
        self.assertEqual(self.customer.post("/customer/availability",data=self.payload(
            csrf)).status_code,409)  # No parallel active slot for one work record.
        ref=owner_availability_snapshot()[0]["id"]
        self.assertEqual(self.customer.post("/customer/availability",data={
            "csrf":csrf,"action":"withdraw","request_id":uuid.uuid4().hex,"revision":"0",
        }).status_code,404)
        self.assertEqual(self.customer.post("/customer/availability",data={
            "csrf":csrf,"action":"withdraw","request_id":ref,"revision":"8",
        }).status_code,409)
        self.assertEqual(self.other.post("/customer/availability",data={
            "csrf":other_csrf,"action":"withdraw","request_id":ref,"revision":"0",
        }).status_code,404)
        self.assertEqual(self.owner.post("/hq/availability-requests",data={
            "csrf":"availability-owner-csrf","decision":"declined",
            "request_id":ref,"revision":"0",
        }).status_code,303)
        self.assertEqual(self.owner.post("/hq/availability-requests",data={
            "csrf":"availability-owner-csrf","decision":"offered",
            "request_id":ref,"revision":"0","date":self.day,"time":"11:00",
        }).status_code,409)
        self.assertEqual(owner_availability_snapshot()[0]["status"],"declined")
        self.assertEqual(self.customer.post("/customer/availability",data=self.payload(
            csrf)).status_code,303)  # New request after owner declined the previous one.
        self.assertEqual(len(owner_availability_snapshot()),2)


if __name__=="__main__":
    unittest.main()
