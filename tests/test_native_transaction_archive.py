"""Synthetic lifecycle tests: archive is not deletion or bank reconciliation."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_finance_splits import classification_report, save, StaleSplit

URL = "/api/hq/finance/native"


class NativeTransactionArchiveTests(unittest.TestCase):
    def test_archive_restore_preserves_values_and_totals(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp, patch.dict(os.environ,{
            "HQ_DATA_DIR":tmp,"HQ_SESSION_SECRET":"synthetic-native-archive",
            "GOOGLE_CLIENT_ID":"synthetic","GOOGLE_CLIENT_SECRET":"synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid"}), app.app_context():
            client = app.test_client()
            ref = "c"*32
            route = "/hq/finance/transactions/"+ref+"/archive"
            self.assertEqual(client.get(route).status_code,302)
            with client.session_transaction() as sess, auth_db() as db:
                sess.update(hq_sid=issue_session(db,"synthetic-owner"),google_sub="synthetic-owner",csrf="test-csrf")
            headers = {"X-HQ-CSRF":"test-csrf"}
            original = dict(kind="transaction",id=ref,revision=0,day="2026-11-01",
                            description="Synthetic duplicate",amount=-1234,category="materials",reviewed=True)
            self.assertEqual(client.post(URL,json=original,headers=headers).status_code,200)
            lines=[dict(category="materials",pence=-1000),dict(category="fuel",pence=-234)]
            save(ref,1,"save",lines,"synthetic-owner")
            archive=dict(csrf="test-csrf",revision="2",action="archive",reason="Duplicate manual entry")
            self.assertEqual(client.post(route,data=archive).status_code,409)
            save(ref,2,"withdraw",[],"synthetic-owner")
            archive["revision"]="3"
            self.assertEqual(client.post(route,data=dict(archive,csrf="wrong")).status_code,403)
            self.assertEqual(client.post(route,data=dict(archive,reason="")).status_code,400)
            self.assertEqual(client.post(route,data=archive).status_code,303)
            self.assertEqual(client.post(route,data=archive).status_code,409)
            self.assertEqual(client.post(URL,json=dict(original,revision=4),headers=headers).status_code,409)
            with self.assertRaises(StaleSplit):
                save(ref,4,"save",lines,"synthetic-owner")
            again=app.test_client()
            with again.session_transaction() as sess, auth_db() as db:
                sess.update(hq_sid=issue_session(db,"synthetic-owner"),google_sub="synthetic-owner",csrf="test-csrf")
            data=again.get(URL).get_json()
            self.assertEqual(data["transaction_count"],0)
            self.assertEqual(data["archived_transaction_count"],1)
            self.assertEqual(data["archived_transactions"][0]["amount"],-1234)
            self.assertEqual(classification_report("2026-11")["outgoing"],0)
            self.assertEqual(classification_report("2026-11")["archived_count"],1)
            self.assertIn(b"Restore to native totals",again.get(route).data)
            self.assertIn(b"1 archived manual entries",again.get("/hq/finance/classifications?month=2026-11").data)
            self.assertEqual(again.post(route,data=dict(archive,revision="4",action="restore",reason="Duplicate check cleared")).status_code,303)
            self.assertEqual(classification_report("2026-11")["outgoing"],1234)
            data=again.get(URL).get_json()
            self.assertEqual(data["transaction_count"],1)
            self.assertEqual(data["archived_transaction_count"],0)
            self.assertIsNone(data["received_income"])
            self.assertFalse(data["bank_verified"])
            with connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_transactions").fetchone()[0],1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_transaction_archive").fetchone()[0],2)
            self.assertNotIn(b"Synthetic duplicate",app.test_client().get("/book").data)
