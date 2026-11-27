"""Synthetic owner UI, conservation, persistent history and concurrency tests."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from customer_request_store import connect
from hq_native_finance import schema

ROOT = Path(__file__).resolve().parents[1]

class FinanceSplitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"HQ_DATA_DIR":tmp.name,
            "HQ_SESSION_SECRET":"synthetic-split-test-secret",
            "GOOGLE_CLIENT_ID":"synthetic","GOOGLE_CLIENT_SECRET":"synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid"})
        env.start()
        self.addCleanup(env.stop)
        self.client=app.test_client()
        self.ref="a"*32
        self.url="/hq/finance/transactions/"+self.ref+"/split"
        with connect() as db:
            schema(db)
            db.execute("INSERT INTO finance_transactions VALUES(?,?,?,?,?,?,?)",
                       (self.ref,"2026-01-01","Synthetic mixed purchase",-10000,"unclassified",0,1))

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db,"synthetic-owner"),
                        google_sub="synthetic-owner",csrf="test-csrf")

    def form(self, **kw):
        return dict({"csrf":"test-csrf","revision":"1","action":"save",
                "category_0":"materials","amount_0":"-70.00",
                "category_1":"fuel","amount_1":"-30.00"},**kw)

    def test_owner_split_and_reversal(self):
        self.assertEqual(self.client.get(self.url).status_code,302)
        self.login()
        page=self.client.get(self.url)
        self.assertEqual(page.status_code,200)
        self.assertIn(b"Save split classification",page.data)
        self.assertIn("no-store",page.headers["Cache-Control"])
        self.assertEqual(self.client.post(self.url,data=self.form(csrf="bad")).status_code,403)
        self.assertEqual(self.client.post(self.url,data=self.form(amount_1="-29.99")).status_code,400)
        self.assertEqual(self.client.post(self.url,data=self.form(amount_0="70.00",amount_1="-170.00")).status_code,400)
        self.assertEqual(self.client.post(self.url,data=self.form()).status_code,303)
        self.assertEqual(self.client.post(self.url,data=self.form()).status_code,303)
        self.assertEqual(self.client.post(self.url,data=self.form(amount_0="-60",amount_1="-40")).status_code,409)
        page=self.client.get(self.url)
        self.assertIn(b'Withdraw split',page.data)
        self.assertIn(b'value="-70.00"',page.data)
        with connect() as db:
            p=db.execute("SELECT * FROM finance_transactions WHERE id=?",(self.ref,)).fetchone()
            self.assertEqual((p["amount"],p["category"],p["reviewed"],p["revision"]),(-10000,"unclassified",0,2))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_split_events").fetchone()[0],1)
        payload=dict(kind="transaction",id=self.ref,revision=2,day="2026-01-01",
                     description="Synthetic mixed purchase",amount=-9000,category="fuel",reviewed=True)
        self.assertEqual(self.client.post("/api/hq/finance/native",json=payload,
            headers={"X-HQ-CSRF":"test-csrf"}).status_code,409)
        self.assertEqual(self.client.post(self.url,data=self.form(revision="2",action="withdraw")).status_code,303)
        page=self.client.get(self.url)
        self.assertIn(b"Revision 3",page.data)
        self.assertIn(b"Revision 2",page.data)
        self.assertNotIn(b"Active split saved",page.data)
        payload["revision"]=3
        self.assertEqual(self.client.post("/api/hq/finance/native",json=payload,
            headers={"X-HQ-CSRF":"test-csrf"}).status_code,200)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_split_events").fetchone()[0],2)

    def test_monthly_report_conserves_cash_and_follows_current_split(self):
        from hq_finance_splits import classification_report
        url="/hq/finance/classifications?month=2026-01"
        self.assertEqual(self.client.get(url).status_code,302)
        self.login()
        with connect() as db:
            for ref, day, label, amount, cat in [
                ("b"*32,"2026-01-02","Synthetic transfer",25000,"internal_transfer"),
                ("c"*32,"2026-01-03","Synthetic income",5000,"customer_income"),
                ("d"*32,"2026-02-01","Outside month",-12300,"fuel")]:
                db.execute("INSERT INTO finance_transactions VALUES(?,?,?,?,?,?,?)",
                           (ref,day,label,amount,cat,1,1))
        self.assertEqual(self.client.post(self.url,data=self.form()).status_code,303)
        report=classification_report("2026-01")
        self.assertEqual((len(report["entries"]),report["split_count"],report["incoming"],
                          report["outgoing"],report["unclassified"]),(3,1,30000,10000,0))
        self.assertEqual(dict(report["totals"]),{
            "materials":{"incoming":0,"outgoing":7000},
            "fuel":{"incoming":0,"outgoing":3000},
            "internal_transfer":{"incoming":25000,"outgoing":0},
            "customer_income":{"incoming":5000,"outgoing":0}})
        page=self.client.get(url)
        self.assertEqual(page.status_code,200)
        self.assertIn("no-store",page.headers["Cache-Control"])
        self.assertIn(b"Edit / withdraw split",page.data)
        self.assertIn(b"Active split proposal",page.data)
        self.assertIn(b"70.00",page.data)
        self.assertNotIn(b"Outside month",page.data)
        self.assertEqual(self.client.get("/hq/finance/classifications?month=2026-13").status_code,400)
        self.assertIn(b"Actual bank activity is unknown",self.client.get(
            "/hq/finance/classifications?month=2026-03").data)
        self.assertEqual(self.client.post(self.url,data=self.form(revision="2",
            amount_0="-60",amount_1="-40")).status_code,303)
        self.assertEqual(dict(classification_report("2026-01")["totals"])["fuel"]["outgoing"],4000)
        self.assertEqual(self.client.post(self.url,data=self.form(revision="3",action="withdraw")).status_code,303)
        report=classification_report("2026-01")
        self.assertEqual((report["outgoing"],report["split_count"],report["unclassified"]),(10000,0,10000))
        self.assertNotIn("fuel",dict(report["totals"]))
        self.assertIn(b"Create split / view history",self.client.get(url).data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0],4)

    def test_monthly_report_withholds_inconsistent_active_split(self):
        self.login()
        self.assertEqual(self.client.post(self.url,data=self.form()).status_code,303)
        with connect() as db:
            db.execute("UPDATE finance_transactions SET revision=revision+1 WHERE id=?",(self.ref,))
        page=self.client.get("/hq/finance/classifications?month=2026-01")
        self.assertEqual(page.status_code,400)
        self.assertNotIn(b"Effective category totals",page.data)

    def test_exact_classification_preserves_parent_amount_and_history(self):
        from hq_finance_splits import classification_report
        url=self.url.replace("/split","/classify")
        self.assertEqual(self.client.get(url).status_code,302)
        self.login()
        page=self.client.get(url)
        self.assertEqual(page.status_code,200)
        self.assertIn("no-store",page.headers["Cache-Control"])
        self.assertIn(b"Save classification",page.data)
        self.assertIn(url.encode(),self.client.get("/hq/finance/classifications?month=2026-01").data)
        form=dict(csrf="test-csrf",revision="1",category="fuel",reviewed="yes",amount="9999")
        self.assertEqual(self.client.post(url,data={**form,"csrf":"bad"}).status_code,403)
        self.assertEqual(self.client.post(url,data={**form,"category":"unclassified"}).status_code,400)
        self.assertEqual(self.client.post(url,data={**form,"category":"external_funding"}).status_code,400)
        self.assertEqual(self.client.post(url,data=form).status_code,303)
        self.assertEqual(self.client.post(url,data=form).status_code,303)
        self.assertEqual(self.client.post(url,data={**form,"category":"materials"}).status_code,409)
        page=self.client.get(url)
        self.assertIn(b'value="fuel" selected',page.data)
        self.assertIn(b'Revision 2',page.data)
        with connect() as db:
            row=db.execute("SELECT * FROM finance_transactions WHERE id=?",(self.ref,)).fetchone()
            self.assertEqual((row["amount"],row["day"],row["description"],row["category"],row["reviewed"],row["revision"]),
                (-10000,"2026-01-01","Synthetic mixed purchase","fuel",1,2))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_transaction_history WHERE transaction_id=?",(self.ref,)).fetchone()[0],2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_edit_audit WHERE entity='transaction_classification'").fetchone()[0],1)
        report=classification_report("2026-01")
        self.assertEqual((report["outgoing"],report["unclassified"]),(10000,0))
        self.assertEqual(self.client.post(self.url,data=self.form(revision="2")).status_code,303)
        self.assertNotIn(b"Save classification",self.client.get(url).data)
        self.assertEqual(self.client.post(url,data={**form,"revision":"3"}).status_code,409)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0],1)
