"""Synthetic native monthly target planning with no live provider or Sheets data."""
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_native_finance import schema
from hq_finance_splits import save
from hq_monthly_target import current_month_report


class MonthlyTargetTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"HQ_DATA_DIR":tmp.name,
            "HQ_SESSION_SECRET":"synthetic-target-test-secret",
            "GOOGLE_CLIENT_ID":"synthetic","GOOGLE_CLIENT_SECRET":"synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid"})
        env.start()
        self.addCleanup(env.stop)
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.client = app.test_client()
        self.today = date(2026,11,28)

    def add(self, ref, amount, category, reviewed=1, day="2026-11-01"):
        with connect() as db:
            schema(db)
            db.execute("INSERT INTO finance_transactions VALUES(?,?,?,?,?,?,?)",
                       (ref*32,day,"Synthetic only",amount,category,reviewed,1))

    def test_target_uses_only_reviewed_customer_flow_and_preserves_unknowns(self):
        empty = current_month_report(self.today)
        self.assertIsNone(empty["target"])
        self.assertIsNone(empty["net"])
        self.assertIsNone(empty["planned_month"])
        with connect() as db:
            schema(db)
            db.execute("INSERT INTO finance_plan VALUES(1,NULL,NULL,610000,1)")
            for ref, due, amount, status, archived in [
                ("a","2026-11-30",8000,"planned",0),
                ("b","2026-10-31",1000,"planned",0),
                ("c","2026-11-10",2000,"reported_paid",0),
                ("d","2026-11-20",9900,"cancelled",0),
                ("e","2026-11-20",9000,"planned",1)]:
                db.execute("INSERT INTO finance_bills VALUES(?,?,?,?,1,?,NULL,?)",
                           (ref*32,"Synthetic bill",amount,due,status,archived))
        self.add("a",100000,"customer_income")
        self.add("b",-1000,"customer_refund")
        self.add("c",70000,"internal_transfer")
        self.add("d",100000,"director_funding")
        self.add("e",10000,"customer_income",day="2026-11-29")
        self.add("f",10000,"unclassified",0)
        save("f"*32,1,"save",[dict(category="customer_income",pence=4000),
                              dict(category="director_funding",pence=6000)],"synthetic-owner")
        report = current_month_report(self.today)
        self.assertEqual((report["incoming"],report["refunds"],report["net"]),(104000,1000,103000))
        self.assertEqual((report["gap"],report["days"],report["daily"]),(507000,3,169000))
        self.assertEqual((report["planned_month"],report["overdue"],report["reported_paid"]),(8000,1000,2000))
        self.assertFalse(report["bank_verified"])
        self.add("1",1000,"customer_income",0)
        self.assertIsNone(current_month_report(self.today)["gap"])
        self.add("2",1000,"unclassified",1)
        self.assertEqual(current_month_report(self.today)["ambiguous"],1)

    def test_owner_http_and_native_saved_target(self):
        url = "/hq/finance/monthly-target"
        self.assertEqual(self.client.get(url).status_code,302)
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db,"synthetic-owner"),google_sub="synthetic-owner",csrf="target-csrf")
        response=self.client.post("/api/hq/finance/native",json=dict(kind="plan",revision=0,
            balance=None,target=610000),headers={"X-HQ-CSRF":"target-csrf"})
        self.assertEqual(response.status_code,200)
        page=self.client.get(url)
        self.assertEqual(page.status_code,200)
        self.assertIn("no-store",page.headers["Cache-Control"])
        self.assertIn(b"6,100.00",page.data)
        self.assertIn(b"Unavailable",page.data)
        self.assertIn(b"Native monthly target progress",page.data)
        self.assertIn(b"/hq/finance/monthly-target",self.client.get("/hq/finance/command").data)
