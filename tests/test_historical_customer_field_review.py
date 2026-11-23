"""Synthetic, owner-only historical Customers field review integration tests.

No external Sheets reads, canonical customer mutations, imports or live bookings.
"""
import copy
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
from hq_core_records import _schema
from hq_historical_customer_links import (
    source_stamp, stage_customer_candidate, withdraw_customer_candidate,
)
from hq_historical_customer_field_review import _read_snapshot, save_field_review, _tables
from hq_customer_account_types import _tables as tier_tables
from hq_customer_postcodes import save_postcode

ROOT = Path(__file__).resolve().parents[1]


def snapshots():
    jobs=[["Job ID","Date","Customer","Customer Type","Vehicle/Reg","Service 1"]
          +[""]*10+["Quoted Price","Status"],
          ["J0123","","Synthetic Owner Customer","Trader","","Window Tinting"]
          +[""]*10+[125,"Quoted"]]
    customers=[["Customer","Type","Postcode"],
               ["Synthetic Owner Customer","Trader","ZZ0 0AA"]]
    def data(j,p):
        return {"valueRanges":[
            {"range":"Jobs!A4:AZ1000","values":j},
            {"range":"Customers!A4:AF1000","values":p},
        ]}
    return data(jobs,customers), data(copy.deepcopy(jobs),copy.deepcopy(customers))


class CustomerFieldReviewTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ, {
            "HQ_DATA_DIR":tmp.name,
            "HQ_SESSION_SECRET":"synthetic-customer-field-review-secret",
            "GOOGLE_CLIENT_ID":"synthetic",
            "GOOGLE_CLIENT_SECRET":"synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        ctx=app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.customer=uuid.uuid4().hex
        self.formula,self.computed=snapshots()
        self.original=copy.deepcopy((self.formula,self.computed))
        with connect() as db:
            _schema(db)
            tier_tables(db)
            db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                       (self.customer,None,"Synthetic Owner Customer",
                        "owner@example.invalid","", "2026-01-01"))
            db.execute("""INSERT INTO hq_customer_account_types
                VALUES(?,?,?,?,?)""",(self.customer,"trade",
                "trade_evidence_reviewed",1,"2026-01-01"))
        self.candidate=stage_customer_candidate(
            formula=self.formula,computed=self.computed,sheet_row=5,
            expected_source_stamp=source_stamp(self.formula,self.computed,5),
            customer_id=self.customer,basis="name_crosscheck",
            operation_id=uuid.uuid4().hex,owner_identity="synthetic-owner")
        self.client=app.test_client()
        self.url="/hq/migration/customers/"+self.candidate+"/fields"

    def login(self):
        with self.client.session_transaction() as sess,auth_db() as db:
            sess.update(hq_sid=issue_session(db,"synthetic-owner"),
                        google_sub="synthetic-owner",csrf="test-csrf")

    def data(self,**overrides):
        data={"csrf":"test-csrf","revision":"0","operation_id":"a"*32,
              "name_decision":"candidate_agreement",
              "account_type_decision":"candidate_agreement",
              "postcode_decision":"not_mapped"}
        data.update(overrides)
        return data

    def live_source(self):
        return (patch.dict(app.extensions,
                {"hq_sheets_connection":lambda:("fixture",9999999999)}),
                patch("hq_historical_customer_links._read_live",
                      return_value=(self.formula,self.computed)))

    def test_owner_ui_save_reload_idempotency_and_unmodified_source(self):
        self.assertEqual(self.client.get(self.url).status_code,302)
        self.login()
        a,b=self.live_source()
        with a,b as read:
            page=self.client.get(self.url)
            self.assertEqual(page.status_code,200)
            self.assertIn(b"Synthetic Owner Customer",page.data)
            self.assertIn(b"Not mapped in core HQ customer",page.data)
            self.assertIn("no-store",page.headers["Cache-Control"])
            self.assertEqual(self.client.post(self.url,data=self.data(csrf="bad")).status_code,403)
            self.assertEqual(self.client.post(self.url,data=self.data(revision="x")).status_code,400)
            saved=self.client.post(self.url,data=self.data())
            self.assertEqual(saved.status_code,303)
            self.assertEqual(self.client.post(self.url,data=self.data()).status_code,303)
            self.assertIn(b"field review was saved",
                          self.client.get(self.url+"?saved=1").data)
            self.assertEqual(read.call_count,4)
        with connect() as db:
            result=db.execute("""SELECT * FROM hq_historical_customer_field_reviews
                WHERE candidate_id=?""",(self.candidate,)).fetchone()
            self.assertEqual((result["revision"],result["name_decision"],
                result["account_type_decision"],result["postcode_decision"]),
                (1,"candidate_agreement","candidate_agreement","not_mapped"))
            self.assertEqual(db.execute("""SELECT count(*) FROM
                hq_historical_customer_field_events""").fetchone()[0],1)
            self.assertEqual(db.execute("""SELECT count(*) FROM
                hq_historical_customer_field_operations""").fetchone()[0],1)
            values=db.execute("SELECT name,email FROM hq_customers WHERE id=?",
                              (self.customer,)).fetchone()
            self.assertEqual(tuple(values),("Synthetic Owner Customer","owner@example.invalid"))
            self.assertNotIn("Synthetic Owner Customer",str(dict(result)))
            self.assertNotIn("ZZ0 0AA",str(dict(result)))
            self.assertIsNone(db.execute("""SELECT 1 FROM sqlite_master
                WHERE name='hq_historical_customer_imports'""").fetchone())
        self.assertEqual((self.formula,self.computed),self.original)

    def test_conflict_stale_revision_and_valid_second_version(self):
        self.login()
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.post(self.url,data=self.data()).status_code,303)
            self.assertEqual(self.client.post(self.url,data=self.data(
                postcode_decision="requires_followup")).status_code,409)
            self.assertEqual(self.client.post(self.url,data=self.data(
                operation_id="b"*32,postcode_decision="requires_followup")).status_code,409)
            self.assertEqual(self.client.post(self.url,data=self.data(
                operation_id="c"*32,revision="1",
                postcode_decision="requires_followup")).status_code,303)
            self.assertEqual(self.client.post(self.url,data=self.data(
                operation_id="d"*32,revision="2",
                postcode_decision="candidate_agreement")).status_code,409)
        with connect() as db:
            events=list(db.execute("""SELECT revision FROM hq_historical_customer_field_events
                ORDER BY revision"""))
            self.assertEqual([row[0] for row in events],[1,2])

    def test_source_native_and_tier_drift_are_fail_closed(self):
        self.login()
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.post(self.url,data=self.data()).status_code,303)
            with connect() as db:
                db.execute("UPDATE hq_customer_account_types SET tier='retail',revision=2 WHERE customer_id=?",
                           (self.customer,))
            page=self.client.get(self.url)
            self.assertEqual(page.status_code,200)
            self.assertIn(b"account classification changed",page.data)
            self.assertEqual(self.client.post(self.url,data=self.data(
                revision="1",operation_id="e"*32)).status_code,409)
        with connect() as db:
            db.execute("UPDATE hq_customers SET phone='07700 900000' WHERE id=?",
                       (self.customer,))
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.get(self.url).status_code,409)
        with connect() as db:
            db.execute("UPDATE hq_customers SET phone='' WHERE id=?",
                       (self.customer,))
        formula,computed=snapshots()
        formula["valueRanges"][1]["values"][1][2]="ZZ1 1BB"
        computed["valueRanges"][1]["values"][1][2]="ZZ1 1BB"
        self.formula,self.computed=formula,computed
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.get(self.url).status_code,409)
        with connect() as db:
            self.assertEqual(db.execute("""SELECT count(*) FROM
                hq_historical_customer_field_events""").fetchone()[0],1)

    def test_native_postcode_visible_to_owner_but_not_verified_match(self):
        save_postcode(self.customer,"SW1A 1AA",0,"f"*32,"synthetic-owner")
        self.login()
        a,b=self.live_source()
        with a,b:
            page=self.client.get(self.url)
            self.assertEqual(page.status_code,200)
            self.assertIn(b"SW1A 1AA",page.data)
            self.assertIn(b"owner-recorded, not independently verified",page.data)
            self.assertEqual(self.client.post(self.url,data=self.data(
                postcode_decision="candidate_agreement")).status_code,409)
        self.assertEqual((self.formula,self.computed),self.original)

    def test_withdrawal_and_security_gates(self):
        self.login()
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.get(self.url+"nonsense").status_code,404)
            self.assertEqual(self.client.post(self.url,data=self.data(
                name_decision="invalid")).status_code,409)
            self.assertEqual(self.client.post(self.url,data=self.data(
                account_type_decision="not_mapped")).status_code,303)
            withdraw_customer_candidate(self.candidate,0,"synthetic-owner")
            self.assertEqual(self.client.get(self.url).status_code,409)
            self.assertEqual(self.client.post(self.url,data=self.data(
                operation_id="f"*32,revision="1")).status_code,409)


if __name__=="__main__":
    unittest.main()
