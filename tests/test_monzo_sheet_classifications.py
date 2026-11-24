"""Synthetic source and owner form tests; no real bank or Sheets traffic."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_native_finance import schema
from hq_monzo_sheet_classifications import HEADER,SOURCE,parse_feed,LEGACY_SOURCE,LEGACY_HEADER,parse_legacy,attach_legacy

ROOT = Path(__file__).resolve().parents[1]


def source(*rows):
    return {"range":SOURCE,"values":[HEADER,*rows]}


def example(ref="tx_synthetic_1", amount=-100.25, category=None):
    return [ref,46028,0.5,"card","Synthetic shop","",category,amount,"GBP"]


class MonzoSheetReviewTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ,{"HQ_DATA_DIR":tmp.name,
            "HQ_SESSION_SECRET":"synthetic-monzo-test",
            "GOOGLE_CLIENT_ID":"synthetic","GOOGLE_CLIENT_SECRET":"synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid"})
        env.start();self.addCleanup(env.stop)
        self.client=app.test_client()
        self.url="/hq/finance/monzo-classifications"
        self.rows=[example()]
        self.legacy={"tx_synthetic_1":("Legacy materials","Synthetic confirmed source")}
        provider=patch.dict(app.extensions,{"hq_sheets_connection":lambda:("fixture-token",10**12)})
        provider.start();self.addCleanup(provider.stop)
        reader=patch("hq_monzo_sheet_classifications.read_feed",side_effect=lambda _:self.feed())
        self.reader=reader.start();self.addCleanup(reader.stop)
        with connect() as db:
            schema(db)

    def feed(self):
        return attach_legacy(parse_feed(source(*self.rows)),self.legacy)

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db,"synthetic-owner"),google_sub="synthetic-owner",csrf="test-csrf")

    def test_schema_and_exact_units(self):
        entry=parse_feed(source(example()))["tx_synthetic_1"]
        self.assertEqual(entry["pence"],-10025)
        self.assertIsNone(entry["category"])
        for rows in [(example(),example()),(example(amount=1.001),),
                     (example(amount="100"),),(["tx_bad"],)]:
            with self.assertRaises(ValueError):
                parse_feed(source(*rows))
        with self.assertRaises(ValueError):
            parse_feed({"range":SOURCE,"values":[["Changed heading"]]})

    def test_owner_form_history_and_no_cash_changes(self):
        self.assertEqual(self.client.get(self.url).status_code,302)
        self.login()
        self.assertEqual(self.client.get(self.url).status_code,200)
        self.reader.assert_not_called()
        self.assertEqual(self.client.post(self.url,data={"action":"load","csrf":"bad"}).status_code,403)
        self.reader.assert_not_called()
        page=self.client.post(self.url,data={"action":"load","csrf":"test-csrf"})
        self.assertEqual(page.status_code,200)
        self.assertIn(b"Synthetic shop",page.data)
        self.assertIn(b"no-store",page.headers["Cache-Control"].encode())
        evidence=self.feed()["tx_synthetic_1"]["evidence"]
        legacy_evidence=self.feed()["tx_synthetic_1"]["legacy_evidence"]
        self.assertIn(b"Legacy materials",page.data)
        self.assertIn(b"Synthetic confirmed source",page.data)
        form=dict(action="save",csrf="test-csrf",ref="tx_synthetic_1",revision="0",
                  evidence=evidence,legacy_evidence=legacy_evidence,category="materials",confirm="yes")
        self.assertEqual(self.client.post(self.url,data=dict(form,confirm="")).status_code,400)
        self.assertEqual(self.client.post(self.url,data=dict(form,category="customer_income")).status_code,400)
        self.assertEqual(self.client.post(self.url,data=form).status_code,200)
        self.assertEqual(self.client.post(self.url,data=form).status_code,200)
        self.assertEqual(self.client.post(self.url,data=dict(form,category="fuel")).status_code,409)
        # Reordering source rows must not affect identity.
        self.rows.insert(0,example("tx_synthetic_2",amount=20))
        page=self.client.post(self.url,data=dict(form,category="fuel",revision="1"))
        self.assertEqual(page.status_code,200)
        self.assertIn(b"Saved revision 2",page.data)
        # New browser request reads previously saved classification from SQLite.
        page=self.client.post(self.url,data={"action":"load","csrf":"test-csrf"})
        self.assertIn(b"Revision 1",page.data)
        self.assertIn(b"Revision 2",page.data)
        self.assertEqual(self.client.post(self.url,data=dict(form,category="unclassified",revision="2")).status_code,200)
        # Changed amount with same ID cannot inherit the old classification.
        self.rows[1]=example(amount=-101)
        self.assertEqual(self.client.post(self.url,data=dict(form,category="fuel",revision="3")).status_code,409)
        page=self.client.post(self.url,data={"action":"load","csrf":"test-csrf"})
        self.assertIn(b"Source changed since review",page.data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM monzo_sheet_classification_events").fetchone()[0],3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0],0)

    def test_legacy_override_join_and_changed_context(self):
        legacy=parse_legacy({"range":LEGACY_SOURCE,"values":[LEGACY_HEADER,
            ["tx_other","Other"],[],["tx_synthetic_1","Legacy fuel","Confirmed"]]})
        joined=attach_legacy(parse_feed(source(example())),legacy)["tx_synthetic_1"]
        self.assertEqual(joined["legacy_category"],"Legacy fuel")
        for rows in [
            [["tx_other","A"],["tx_other","B"]],
            [["","Missing ID"]], [["bad id","A"]]
        ]:
            with self.assertRaises(ValueError):
                parse_legacy({"range":LEGACY_SOURCE,"values":[LEGACY_HEADER,*rows]})
        self.login()
        old=self.feed()["tx_synthetic_1"]
        form=dict(action="save",csrf="test-csrf",ref=old["ref"],revision="0",
                  evidence=old["evidence"],legacy_evidence=old["legacy_evidence"],
                  category="materials",confirm="yes")
        self.legacy["tx_synthetic_1"]=("Legacy fuel","New evidence")
        self.assertEqual(self.client.post(self.url,data=form).status_code,409)
        current=self.feed()["tx_synthetic_1"]
        form["legacy_evidence"]=current["legacy_evidence"]
        self.assertEqual(self.client.post(self.url,data=form).status_code,200)
        self.legacy.clear()
        page=self.client.post(self.url,data={"action":"load","csrf":"test-csrf"})
        self.assertIn(b"Existing Sheets override differs",page.data)
        self.assertIn(b"No existing Sheets override matched",page.data)
        self.assertIn(b"Sheets override at review: Legacy fuel",page.data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0],0)
            row=db.execute("SELECT category FROM monzo_sheet_classification_legacy_evidence").fetchone()
            self.assertEqual(row[0],"Legacy fuel")


    def test_invoice_candidate_save_reload_withdraw_and_no_receipt(self):
        import re
        from hq_receivables import save as save_invoice
        from hq_core_records import _schema
        url="/hq/finance/monzo-invoice-match"
        self.assertEqual(self.client.get(url).status_code,302)
        with connect() as db:
            _schema(db)
            db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                       ("a"*32,None,"Synthetic invoice customer","","","2026-11-22"))
        invoice=dict(id="b"*32,operation_id="c"*32,revision=0,
                     customer_id="a"*32,work_id=None,invoice_ref="TEST-MONZO-1",
                     issue_date="2026-11-22",due_date="2026-12-10",amount_pence=35000,
                     status="issued",archived=False,memo="")
        save_invoice(invoice)
        self.rows=[example(amount=100.25),example("tx_synthetic_2",amount=300),
                   example("tx_negative",amount=-5)]
        self.login()
        form=dict(action="review",csrf="test-csrf",ref="tx_synthetic_1",
                  invoice_ref="TEST-MONZO-1")
        self.assertEqual(self.client.post(url,data=dict(form,csrf="bad")).status_code,403)
        self.assertEqual(self.client.post(url,data=dict(form,ref="tx_negative")).status_code,400)
        page=self.client.post(url,data=form)
        self.assertEqual(page.status_code,200)
        self.assertIn(b"Synthetic invoice customer",page.data)
        self.assertIn(b"Candidate amount",page.data)
        def manifest(page):
            return re.search(rb'name="manifest" value="([a-f0-9]{64})"',page.data)[1].decode()
        proposal=dict(form,action="save",revision="0",manifest=manifest(page),confirm="yes")
        self.assertEqual(self.client.post(url,data=dict(proposal,confirm="")).status_code,400)
        # Invoice revision changes after review; require a new preview.
        save_invoice(dict(invoice,revision=1,operation_id="d"*32,memo="changed"))
        self.assertEqual(self.client.post(url,data=proposal).status_code,409)
        proposal["manifest"]=manifest(self.client.post(url,data=form))
        self.assertEqual(self.client.post(url,data=proposal).status_code,200)
        self.assertEqual(self.client.post(url,data=proposal).status_code,200)
        loaded=self.client.get(url+"?ref=tx_synthetic_1")
        self.assertIn(b"Revision 1",loaded.data)
        self.assertIn(b"Withdraw saved candidate",loaded.data)
        self.assertEqual(self.client.post(url,data=dict(form,ref="tx_synthetic_2")).status_code,400)
        self.assertEqual(self.client.post(url,data=form).status_code,409)
        # Withdrawal remains available even if Sheets is temporarily inaccessible.
        self.reader.side_effect=AssertionError("No source read expected on withdrawal")
        withdraw=dict(proposal,action="withdraw",revision="1")
        self.assertEqual(self.client.post(url,data=withdraw).status_code,200)
        self.assertEqual(self.client.post(url,data=withdraw).status_code,200)
        self.reader.side_effect=lambda _:self.feed()
        self.assertEqual(self.client.post(url,data=dict(form,ref="tx_synthetic_2")).status_code,200)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_monzo_invoice_match_events").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocations").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT amount_pence FROM hq_receivables").fetchone()[0],35000)


    def test_native_monzo_split_shared_history_and_exact_amount(self):
        self.login()
        entry=self.feed()["tx_synthetic_1"]
        form=dict(action="split",csrf="test-csrf",ref=entry["ref"],revision="0",
                  evidence=entry["evidence"],legacy_evidence=entry["legacy_evidence"],
                  confirm="yes",category_0="materials",amount_0="-70.00",
                  category_1="fuel",amount_1="-30.25")
        self.assertEqual(self.client.post(self.url,data=dict(form,csrf="bad")).status_code,403)
        self.assertEqual(self.client.post(self.url,data=dict(form,confirm="")).status_code,400)
        self.assertEqual(self.client.post(self.url,data=dict(form,amount_1="-30.24")).status_code,400)
        self.assertEqual(self.client.post(self.url,data=dict(form,amount_1="30.25")).status_code,400)
        self.assertEqual(self.client.post(self.url,data=dict(form,category_1="customer_income")).status_code,400)
        page=self.client.post(self.url,data=form)
        self.assertEqual(page.status_code,200)
        self.assertIn(b"Current proposal is split",page.data)
        self.assertIn(b'value="-70.00"',page.data)
        self.assertEqual(self.client.post(self.url,data=form).status_code,200)
        self.assertEqual(self.client.post(self.url,data=dict(form,category_1="materials")).status_code,409)
        single={k:v for k,v in form.items() if not k.startswith(("category_","amount_"))}
        single.update(action="save",category="fuel")
        self.assertEqual(self.client.post(self.url,data=single).status_code,409)
        # New request loads the stored lines. Legacy override edits invalidate a stale save.
        loaded=self.client.post(self.url,data=dict(action="load",csrf="test-csrf"))
        self.assertIn(b'value="-30.25"',loaded.data)
        self.legacy[entry["ref"]]=("Changed category","New evidence")
        self.assertEqual(self.client.post(self.url,data=dict(form,revision="1")).status_code,409)
        current=self.feed()[entry["ref"]]
        revised=dict(form,revision="1",legacy_evidence=current["legacy_evidence"],
                     amount_0="-60",amount_1="-40.25")
        self.assertEqual(self.client.post(self.url,data=revised).status_code,200)
        self.assertEqual(self.client.post(self.url,data=dict(single,revision="2",
            legacy_evidence=current["legacy_evidence"],category="unclassified")).status_code,200)
        loaded=self.client.post(self.url,data=dict(action="load",csrf="test-csrf"))
        self.assertIn(b"Saved revision 3",loaded.data)
        self.assertIn(b"Revision 1",loaded.data)
        self.assertNotIn(b"Current proposal is split",loaded.data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM monzo_sheet_classification_events").fetchone()[0],3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM monzo_sheet_classification_split_lines").fetchone()[0],4)
            self.assertEqual(db.execute("SELECT SUM(pence) FROM monzo_sheet_classification_split_lines WHERE revision=2").fetchone()[0],-10025)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0],0)


    def test_bounded_invoice_candidate_amount_and_unmatched_source(self):
        import re
        from hq_receivables import save as save_invoice
        from hq_core_records import _schema
        with connect() as db:
            _schema(db)
            db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                       ("a"*32,None,"Synthetic partial customer","","","2026-11-22"))
        save_invoice(dict(id="b"*32,operation_id="c"*32,revision=0,
                     customer_id="a"*32,work_id=None,invoice_ref="TEST-PARTIAL-1",
                     issue_date="2026-11-22",due_date="2026-12-10",amount_pence=7500,
                     status="issued",archived=False,memo=""))
        self.rows=[example(amount=100.25)]
        self.login()
        url="/hq/finance/monzo-invoice-match"
        form=dict(action="review",csrf="test-csrf",ref="tx_synthetic_1",
                  invoice_ref="TEST-PARTIAL-1")
        for amount in ("","0","-5","75.001","75.01","100.26","1e1","NaN"):
            self.assertEqual(self.client.post(url,data=dict(form,candidate_amount=amount)).status_code,400)
        page=self.client.post(url,data=dict(form,candidate_amount="75.00"))
        self.assertEqual(page.status_code,200)
        self.assertIn("Unmatched portion of source £25.25".encode(),page.data)
        manifest=re.search(rb'name="manifest" value="([a-f0-9]{64})"',page.data)[1].decode()
        proposal=dict(form,action="save",revision="0",manifest=manifest,confirm="yes",candidate_amount="75.00")
        self.assertEqual(self.client.post(url,data=dict(proposal,candidate_amount="74.00")).status_code,409)
        self.assertEqual(self.client.post(url,data=proposal).status_code,200)
        self.assertEqual(self.client.post(url,data=proposal).status_code,200)
        self.assertEqual(self.client.post(url,data=dict(proposal,candidate_amount="74.00")).status_code,409)
        loaded=self.client.get(url+"?ref=tx_synthetic_1")
        self.assertIn("£75.00".encode(),loaded.data)
        self.assertEqual(self.client.post(url,data=dict(form,candidate_amount="25.25")).status_code,409)
        with connect() as db:
            self.assertEqual(db.execute("SELECT amount_pence FROM hq_monzo_invoice_match_events").fetchone()[0],7500)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_monzo_invoice_match_events").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocations").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0],0)
