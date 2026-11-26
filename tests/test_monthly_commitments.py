"""Synthetic owner workflow for native planned monthly commitments."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_monthly_commitments import due_date

URL = "/hq/finance/monthly-commitments"
API = "/api/hq/finance/native"


class MonthlyCommitmentTests(unittest.TestCase):
    def test_calendar_and_invalid_month(self):
        self.assertEqual(due_date("2028-02",31),"2028-02-29")
        self.assertEqual(due_date("2027-02",31),"2027-02-28")
        self.assertEqual(due_date("2027-04",31),"2027-04-30")
        self.assertEqual(due_date("2027-12",15),"2027-12-15")
        for invalid in ("2027-13","0000-01","2027-1","",None):
            with self.assertRaises(ValueError):
                due_date(invalid,31)

    def test_owner_generation_revision_and_native_bill_persistence(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp, patch.dict(os.environ,{
            "HQ_DATA_DIR":tmp,"HQ_SESSION_SECRET":"synthetic-monthly-commitments",
            "GOOGLE_CLIENT_ID":"synthetic","GOOGLE_CLIENT_SECRET":"synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid"}), app.app_context():
            client=app.test_client()
            self.assertEqual(client.get(URL).status_code,302)
            with client.session_transaction() as sess, auth_db() as db:
                sess.update(hq_sid=issue_session(db,"synthetic-owner"),google_sub="synthetic-owner",csrf="test-csrf")
            self.assertEqual(client.get(URL).status_code,200)
            ref="a"*32
            base=dict(csrf="test-csrf",id=ref,revision="0",name="Synthetic rent",
                      amount="125.30",due_day="31",action="save")
            self.assertEqual(client.post(URL,data=dict(base,csrf="wrong")).status_code,403)
            self.assertEqual(client.post(URL,data=dict(base,amount="1.001")).status_code,400)
            self.assertEqual(client.post(URL,data=base).status_code,303)
            self.assertEqual(client.post(URL,data=base).status_code,409)
            self.assertEqual(client.post(URL,data=dict(base,id="b"*32,name=" SYNTHETIC   RENT ")).status_code,409)
            self.assertEqual(len(client.get(API).get_json()["bills"]),0)
            preview=dict(csrf="test-csrf",id=ref,revision="1",month="2027-02",action="preview")
            res=client.post(URL,data=preview)
            self.assertEqual(res.status_code,200)
            self.assertIn(b"Approve and create planned bill",res.data)
            self.assertIn(b"2027-02-28",res.data)
            self.assertEqual(len(client.get(API).get_json()["bills"]),0)
            confirm=dict(preview,action="generate",approved="yes",due="2027-02-28",amount_pence="12530")
            self.assertEqual(client.post(URL,data=dict(confirm,approved="no")).status_code,400)
            self.assertEqual(client.post(URL,data=dict(confirm,amount_pence="1")).status_code,409)
            self.assertEqual(client.post(URL,data=confirm).status_code,303)
            self.assertEqual(client.post(URL,data=confirm).status_code,409)
            bills=client.get(API).get_json()["bills"]
            self.assertEqual(len(bills),1)
            bill=bills[0]
            self.assertEqual((bill["name"],bill["amount"],bill["due"],bill["status"]),
                             ("Synthetic rent",12530,"2027-02-28","planned"))
            self.assertEqual(client.post(URL,data=dict(base,revision="1",amount="130.50")).status_code,303)
            self.assertEqual(client.post(URL,data=dict(confirm,month="2027-03",due="2027-03-31")).status_code,409)
            # Existing bill stays unchanged after schedule edits, including archive.
            self.assertEqual(client.get(API).get_json()["bills"][0]["amount"],12530)
            headers={"X-HQ-CSRF":"test-csrf"}
            duplicate=dict(kind="bill",id="d"*32,revision=0,name="synthetic rent",amount=100,due="2027-03-15")
            self.assertEqual(client.post(API,json=duplicate,headers=headers).status_code,200)
            self.assertEqual(client.post(URL,data=dict(preview,revision="2",month="2027-03")).status_code,409)
            self.assertEqual(client.post(URL,data=dict(base,revision="2",amount="130.50",archived="1")).status_code,303)
            self.assertEqual(client.post(URL,data=dict(preview,revision="3",month="2027-04")).status_code,409)
            # New client and SQLite connection see original occurrence and revisions.
            again=app.test_client()
            with again.session_transaction() as sess, auth_db() as db:
                sess.update(hq_sid=issue_session(db,"synthetic-owner"),google_sub="synthetic-owner",csrf="test-csrf")
            page=again.get(URL)
            self.assertEqual(page.status_code,200)
            self.assertEqual(page.headers["Cache-Control"],"no-store")
            self.assertIn(b"Originally generated",page.data)
            self.assertIn(b"Archived",page.data)
            self.assertIn(b"125.30",page.data)
            self.assertEqual(again.post(URL,data=dict(base,revision="3",amount="130.50")).status_code,303)
            self.assertEqual(again.post(URL,data=dict(preview,revision="4")).status_code,409)
            snapshot=again.get(API).get_json()
            self.assertFalse(snapshot["bank_verified"])
            self.assertIsNone(snapshot["received_income"])
            with connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_monthly_occurrences").fetchone()[0],1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_monthly_schedule_events").fetchone()[0],4)
            self.assertNotIn(b"Synthetic rent",app.test_client().get("/book").data)
