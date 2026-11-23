"""Synthetic Customers migration staging: no real provider, customer or Sheet writes."""
import os
import sqlite3
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
    review_rows, source_stamp, stage_customer_candidate, withdraw_customer_candidate,
    current_candidates, native_options,
)

ROOT = Path(__file__).resolve().parents[1]


def evidence():
    jobs = [["Job ID", "Date", "Customer", "Customer Type", "Vehicle/Reg",
             "Service 1"] + [""] * 10 + ["Quoted Price", "Status"],
            ["J0123", "", "Synthetic B", "Retail", "", "Window Tinting"] +
            [""] * 10 + [125, "Quoted"]]
    people = [["Customer", "Type", "Postcode"],
              ["Synthetic A", "Retail", "ZZ0 0ZZ"],
              ["Synthetic A", "Trade", "ZZ0 0ZZ"],
              ["Synthetic B", "Retail", "ZZ0 0YY"]]
    def source(j, c):
        return {"valueRanges": [
            {"range": "Jobs!A4:AZ1000", "values": j},
            {"range": "Customers!A4:AF1000", "values": c},
        ]}
    return source(jobs, people), source([r[:] for r in jobs],
                                       [r[:] for r in people])


class CustomerHistoricalCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.tmp.name,
            "HQ_SESSION_SECRET": "historical-customer-synthetic-secret",
            "GOOGLE_CLIENT_ID": "synthetic",
            "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.customer_id = uuid.uuid4().hex
        self.other_id = uuid.uuid4().hex
        with connect() as db:
            _schema(db)
            db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                       (self.customer_id, None, "Synthetic B",
                        "synthetic-b@example.invalid", "", "2026-01-01"))
            db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                       (self.other_id, None, "Different Name",
                        "other@example.invalid", "", "2026-01-01"))
        self.formula, self.computed = evidence()
        self.client = app.test_client()

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        google_sub="synthetic-owner", csrf="synthetic-csrf")

    def stage(self, customer_id=None, operation_id=None, **override):
        with app.app_context():
            args = dict(formula=self.formula, computed=self.computed,
                        sheet_row=7,
                        expected_source_stamp=source_stamp(self.formula,self.computed,7),
                        customer_id=customer_id or self.customer_id,
                        operation_id=operation_id or "a"*32,
                        basis="name_crosscheck", owner_identity="synthetic-owner")
            args.update(override)
            return stage_customer_candidate(**args)

    def test_read_only_preview_identifies_duplicates_and_does_not_create_db(self):
        self.assertEqual(len(review_rows(self.formula, self.computed)["rows"]), 3)
        report = review_rows(self.formula,self.computed)
        self.assertEqual(report["duplicate_rows"],2)
        self.assertEqual(report["rows"][0]["status"],"duplicate_name")
        self.assertEqual(report["rows"][2]["status"],"review")
        self.assertEqual(report["rows"][2]["row"],7)
        self.assertEqual(len(report["rows"][2]["source_stamp"]),64)
        self.assertFalse(current_candidates())
        with connect() as db:
            self.assertIsNone(db.execute("""SELECT name FROM sqlite_master
                WHERE name='hq_historical_customer_candidates'""").fetchone())
        with self.assertRaises(ValueError):
            review_rows(self.formula,self.computed,page_index=1)
        with self.assertRaises(ValueError):
            source_stamp(self.formula,self.computed,5)

    def test_staging_is_idempotent_audited_and_leaves_native_data_intact(self):
        candidate=self.stage()
        self.assertEqual(self.stage(),candidate)
        self.assertEqual(len(current_candidates()),1)
        with connect() as db:
            row=db.execute("""SELECT * FROM hq_historical_customer_candidates
                WHERE id=?""",(candidate,)).fetchone()
            self.assertEqual((row["sheet_row"],row["customer_id"],row["basis"]),
                             (7,self.customer_id,"name_crosscheck"))
            self.assertNotIn("Synthetic B",str(dict(row)))
            self.assertNotIn("example.invalid",str(dict(row)))
            self.assertEqual(len(row["source_stamp"]),64)
            self.assertEqual(len(row["native_stamp"]),64)
            self.assertEqual(db.execute("""SELECT COUNT(*) FROM
                hq_historical_customer_candidate_events WHERE candidate_id=?""",
                (candidate,)).fetchone()[0],1)
            customer=db.execute("SELECT name,email FROM hq_customers WHERE id=?",
                                (self.customer_id,)).fetchone()
            self.assertEqual(tuple(customer),("Synthetic B","synthetic-b@example.invalid"))

    def test_duplicate_collision_mismatch_and_stale_source_are_blocked(self):
        self.stage()
        with self.assertRaisesRegex(ValueError,"Collision"):
            self.stage(operation_id="b"*32)
        with self.assertRaisesRegex(ValueError,"different evidence"):
            self.stage(operation_id="a"*32, basis="owner_document_review")
        with self.assertRaisesRegex(ValueError,"ambiguous"):
            self.stage(customer_id=self.other_id,operation_id="c"*32)
        with self.assertRaisesRegex(ValueError,"Duplicate"):
            self.stage(sheet_row=5,operation_id="d"*32)
        new_formula,new_computed=evidence()
        new_formula["valueRanges"][1]["values"][3][1]="Trade"
        new_computed["valueRanges"][1]["values"][3][1]="Trade"
        with self.assertRaisesRegex(ValueError,"Source changed"):
            self.stage(formula=new_formula,computed=new_computed,
                       operation_id="f"*32)
        with connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_historical_customer_candidates"
            ).fetchone()[0],1)

    def test_withdraw_retains_history_and_allows_restaging(self):
        cid=self.stage()
        with app.app_context():
            withdraw_customer_candidate(cid,0,"synthetic-owner")
            withdraw_customer_candidate(cid,0,"synthetic-owner")
        self.assertEqual(current_candidates(),[])
        with connect() as db:
            actions=[r[0] for r in db.execute("""SELECT action FROM
                hq_historical_customer_candidate_events WHERE candidate_id=?
                ORDER BY revision""",(cid,))]
        self.assertEqual(actions,["proposed","withdrawn"])
        replacement=self.stage(operation_id="b"*32)
        self.assertNotEqual(cid,replacement)

    def test_owner_document_basis_is_review_only_not_verified_identity(self):
        cid=self.stage(customer_id=self.other_id,basis="owner_document_review")
        self.assertEqual(current_candidates()[0]["id"],cid)
        self.assertEqual(current_candidates()[0]["basis"],"owner_document_review")
        with connect() as db:
            self.assertIsNone(db.execute("""SELECT 1 FROM sqlite_master
                WHERE name='hq_historical_customer_imports'""").fetchone())

    def test_native_contact_change_breaks_idempotent_reuse(self):
        self.stage()
        with connect() as db:
            db.execute("UPDATE hq_customers SET phone=? WHERE id=?",
                       ("07700 900000",self.customer_id))
        with self.assertRaisesRegex(ValueError,"different evidence"):
            self.stage()

    def test_route_owner_only_fresh_sheet_read_csrf_and_no_data_leak_on_get(self):
        self.assertEqual(self.client.get("/hq/migration/customers").status_code,302)
        self.login()
        with patch("hq_historical_customer_links._read_live") as read:
            page=self.client.get("/hq/migration/customers")
            self.assertEqual(page.status_code,200)
            self.assertIn("no-store",page.headers["Cache-Control"])
            self.assertIn("noindex",page.headers["X-Robots-Tag"])
            self.assertNotIn(b"Synthetic B",page.data)
            read.assert_not_called()
        self.assertEqual(self.client.post("/hq/migration/customers",data={
            "action":"compare","csrf":"bad"}).status_code,403)
        self.assertEqual(self.client.post("/hq/migration/customers",data={
            "action":"invalid","csrf":"synthetic-csrf"}).status_code,400)
        with (patch.dict(app.extensions,{"hq_sheets_connection":lambda:("fixture",9999999999)}),
              patch("hq_historical_customer_links._read_live",
                    return_value=(self.formula,self.computed)) as read):
            preview=self.client.post("/hq/migration/customers",data={
                "action":"compare","csrf":"synthetic-csrf"})
            self.assertEqual(preview.status_code,200)
            self.assertIn(b"Synthetic B",preview.data)
            self.assertIn(b"duplicate_name",preview.data)
            self.assertEqual(read.call_count,1)
            with app.app_context():
                stamp=source_stamp(self.formula,self.computed,7)
            stage=self.client.post("/hq/migration/customers",data={
                "action":"stage","csrf":"synthetic-csrf","sheet_row":"7","page":"0",
                "source_stamp":stamp,"customer_id":self.customer_id,
                "basis":"name_crosscheck","operation_id":"d"*32})
            self.assertEqual(stage.status_code,303)
        with patch("hq_historical_customer_links._read_live") as read:
            saved=self.client.get("/hq/migration/customers")
            self.assertIn(self.customer_id.encode(),saved.data)
            self.assertNotIn(b"Synthetic B",saved.data)
            read.assert_not_called()
            stop=self.client.post("/hq/migration/customers",data={
                "action":"withdraw","csrf":"synthetic-csrf",
                "candidate_id":current_candidates()[0]["id"],"revision":"0"})
            self.assertEqual(stop.status_code,303)
            read.assert_not_called()


if __name__=="__main__":
    unittest.main()
