"""Synthetic native finance transaction history regression."""
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
from hq_finance_splits import classification_report

URL = "/api/hq/finance/native"


class NativeTransactionHistoryTests(unittest.TestCase):
    def test_owner_edit_history_and_legacy_capture(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp, patch.dict(os.environ,{
            "HQ_DATA_DIR":tmp,"HQ_SESSION_SECRET":"synthetic-native-history",
            "GOOGLE_CLIENT_ID":"synthetic","GOOGLE_CLIENT_SECRET":"synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid"}), app.app_context():
            client = app.test_client()
            self.assertEqual(client.get(URL).status_code,302)
            with client.session_transaction() as sess, auth_db() as db:
                sess.update(hq_sid=issue_session(db,"synthetic-owner"),google_sub="synthetic-owner",csrf="test-csrf")
            headers = {"X-HQ-CSRF":"test-csrf"}
            ref = "a"*32
            original = dict(kind="transaction",id=ref,revision=0,day="2026-11-01",
                            description="Synthetic materials",amount=-1234,category="materials",reviewed=True)
            self.assertEqual(client.post(URL,json=original,headers=headers).status_code,200)
            updated = dict(original,revision=1,amount=-1456,category="equipment",description="Synthetic corrected")
            self.assertEqual(client.post(URL,json=updated,headers=headers).status_code,200)
            self.assertEqual(client.post(URL,json=updated,headers=headers).status_code,409)
            self.assertEqual(client.post(URL,json=dict(updated,revision=2,amount="bad"),headers=headers).status_code,400)
            # Represent a pre-feature transaction with no captured history.
            legacy="b"*32
            with connect() as db:
                schema(db)
                db.execute("INSERT INTO finance_transactions VALUES(?,?,?,?,?,?,?)",
                           (legacy,"2026-11-02","Synthetic legacy",1000,"unclassified",0,4))
            self.assertEqual(client.post(URL,json=dict(original,id=legacy,revision=4,day="2026-11-02",
                description="Synthetic legacy corrected",amount=1000,category="customer_income"),headers=headers).status_code,200)
            # New client/session and connection, not merely in-memory form state.
            again = app.test_client()
            with again.session_transaction() as sess, auth_db() as db:
                sess.update(hq_sid=issue_session(db,"synthetic-owner"),google_sub="synthetic-owner",csrf="test-csrf")
            response = again.get(URL)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers["Cache-Control"],"no-store")
            data=response.get_json()
            history={(h["transaction_id"],h["revision"]):h for h in data["transaction_history"]}
            self.assertEqual(set(history),{(ref,1),(ref,2),(legacy,4),(legacy,5)})
            self.assertEqual(history[(ref,1)]["snapshot"]["amount"],-1234)
            self.assertEqual(history[(ref,1)]["snapshot"]["category"],"materials")
            self.assertEqual(history[(ref,2)]["snapshot"]["amount"],-1456)
            self.assertEqual(history[(legacy,4)]["capture_kind"],"before_edit")
            self.assertEqual(history[(legacy,4)]["snapshot"]["description"],"Synthetic legacy")
            self.assertEqual(history[(legacy,5)]["capture_kind"],"saved")
            self.assertFalse(data["bank_verified"])
            self.assertIsNone(data["received_income"])
            report=classification_report("2026-11")
            self.assertEqual(report["incoming"],1000)
            self.assertEqual(report["outgoing"],1456)
            self.assertIn(b"Transaction edit history",again.get("/hq/finance/command").data)
            self.assertNotIn(b"Synthetic corrected",app.test_client().get("/book").data)
