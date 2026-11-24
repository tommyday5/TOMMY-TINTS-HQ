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
