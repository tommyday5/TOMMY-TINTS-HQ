"""Owner-only creditor register, using isolated synthetic SQLite data."""
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
from hq_creditor_cases import snapshot

ROOT = Path(__file__).resolve().parents[1]
URL = "/hq/finance/creditors"


class CreditorCaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name,
            "HQ_SESSION_SECRET": "synthetic-creditor-secret",
            "GOOGLE_CLIENT_ID": "synthetic",
            "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.client = app.test_client()
        self.identifier = uuid.uuid4().hex

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        google_sub="synthetic-owner", csrf="test-csrf")

    def fields(self, **overrides):
        data = dict(id=self.identifier, operation=uuid.uuid4().hex, revision="0",
                    csrf="test-csrf", confirm="yes", creditor="Synthetic Creditor",
                    reference="CASE-001", scope="business", status="disputed",
                    inclusion="unknown", claimed_balance="125.50", current_monthly="",
                    proposed_monthly="0", committed_monthly="", review_date="2026-12-01",
                    last_confirmed="", evidence="Fixture-1", notes="Synthetic only")
        data.update(overrides)
        return data

    def test_private_ui_save_reload_and_versioned_archive(self):
        self.assertEqual(self.client.get(URL).status_code, 302)
        self.assertEqual(self.client.post(URL, data=self.fields()).status_code, 302)
        self.login()
        first = self.fields()
        self.assertEqual(self.client.post(URL, data=first).status_code, 303)
        self.assertEqual(self.client.post(URL, data=first).status_code, 303)
        # Separate connection and browser client reload persisted record.
        records, count, case, history, rev = snapshot(self.identifier)
        self.assertEqual((count,rev,len(history)), (1,1,1))
        self.assertEqual(case["claimed_balance"],12550)
        self.assertIsNone(case["current_monthly"])
        self.assertEqual(case["proposed_monthly"],0)
        page = self.client.get(URL+"?case="+self.identifier)
        self.assertEqual(page.status_code,200)
        self.assertEqual(page.headers["Cache-Control"],"no-store")
        self.assertIn(b"125.50",page.data)
        self.assertIn(b"Synthetic Creditor",page.data)
        self.assertIn(b"Revision 1",page.data)
        edit = self.fields(revision="1",claimed_balance="100",status="hold_requested")
        self.assertEqual(self.client.post(URL,data=edit).status_code,303)
        self.assertEqual(self.client.post(URL,data=self.fields(revision="1")).status_code,409)
        archive = self.fields(revision="2",claimed_balance="100",archived="yes")
        self.assertEqual(self.client.post(URL,data=archive).status_code,303)
        _,_,case,history,rev = snapshot(self.identifier)
        self.assertTrue(case["archived"])
        self.assertEqual(rev,3)
        self.assertEqual(history[-1]["state"]["claimed_balance"],12550)
        self.assertEqual(self.client.post(URL,data=self.fields(revision="3",claimed_balance="100")).status_code,303)
        self.assertFalse(snapshot(self.identifier)[2]["archived"])
        # Case observations do not create any manual bank transactions or bills.
        with connect() as db:
            for table in ("finance_transactions","finance_bills","finance_plan"):
                if db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0],0)

    def test_validation_duplicate_csrf_and_replay_conflict(self):
        self.login()
        self.assertEqual(self.client.post(URL,data=self.fields(csrf="wrong")).status_code,403)
        self.assertEqual(self.client.post(URL,data=self.fields(confirm="")).status_code,400)
        for invalid in ("1.001","-1","1e3","NaN"):
            self.assertEqual(self.client.post(URL,data=self.fields(claimed_balance=invalid)).status_code,409)
        self.assertEqual(self.client.post(URL,data=self.fields(review_date="2026-02-30")).status_code,409)
        first=self.fields()
        self.assertEqual(self.client.post(URL,data=first).status_code,303)
        self.assertEqual(self.client.post(URL,data=dict(first,claimed_balance="999")).status_code,409)
        self.assertEqual(self.client.post(URL,data=self.fields(id=uuid.uuid4().hex,scope="personal")).status_code,409)
        self.assertEqual(self.client.post(URL,data=self.fields(id=uuid.uuid4().hex,reference="")).status_code,409)
        self.assertEqual(snapshot(self.identifier)[1],1)
        self.assertNotIn(b"Synthetic Creditor",app.test_client().get(URL).data)
        self.assertNotIn(b"Synthetic Creditor",app.test_client().get("/book").data)
