"""Synthetic creditor evidence route and cross-workflow guards."""
import os
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_creditor_cases import save_case, snapshot
from hq_monzo_sheet_classifications import save as classify, parse_feed, attach_legacy, HEADER, SOURCE, Conflict
from hq_monzo_invoice_matches import require_source_not_settled
from hq_creditor_transaction_matches import active

URL = "/hq/finance/creditor-transaction-match"


class CreditorTransactionTests(unittest.TestCase):
    def test_exact_review_replay_stale_case_source_and_withdrawal(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp, patch.dict(os.environ,{
            "HQ_DATA_DIR":tmp,"HQ_SESSION_SECRET":"synthetic-creditor-match",
            "GOOGLE_CLIENT_ID":"synthetic","GOOGLE_CLIENT_SECRET":"synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid"}), app.app_context():
            client=app.test_client()
            self.assertEqual(client.get(URL).status_code,302)
            with client.session_transaction() as sess, auth_db() as db:
                sess.update(hq_sid=issue_session(db,"synthetic-owner"),google_sub="synthetic-owner",csrf="test-csrf")
            case="a"*32
            fields=dict(creditor="Synthetic creditor",reference="TEST-1",scope="business",
                status="disputed",inclusion="unknown",claimed_balance="125.50")
            save_case(case,uuid.uuid4().hex,0,fields,"synthetic-owner")
            ref="tx_creditor_synthetic"
            def make_entry(amount=-25.50):
                return attach_legacy(parse_feed({"range":SOURCE,"values":[HEADER,
                    [ref,46028,0.5,"transfer","Synthetic outflow","",None,amount,"GBP"]]}),{})[ref]
            entry=make_entry()
            form=dict(action="review",csrf="test-csrf",ref=ref,case=case,evidence_ref="Statement fixture")
            def approved(page):
                return dict(form,action="save",confirm="yes",
                    manifest=re.search(rb'name="manifest" value="([0-9a-f]{64})"',page.data).group(1).decode(),
                    operation=re.search(rb'name="operation" value="([0-9a-f]{32})"',page.data).group(1).decode())
            with patch("hq_monzo_invoice_matches.current_entry",return_value=entry) as reader:
                self.assertEqual(client.get(URL+"?case="+case).status_code,200)
                reader.assert_not_called()
                self.assertEqual(client.post(URL,data=dict(form,csrf="wrong")).status_code,403)
                self.assertEqual(client.post(URL,data=form).status_code,409)
                classify(entry,entry["evidence"],0,"creditor_payment","synthetic-owner",entry["legacy_evidence"])
                page=client.post(URL,data=form)
                self.assertEqual(page.status_code,200)
                self.assertIn(b"25.50",page.data)
                self.assertEqual(page.headers["Cache-Control"],"no-store")
                stale=approved(page)
                save_case(case,uuid.uuid4().hex,1,dict(fields,notes="new revision"),"synthetic-owner")
                self.assertEqual(client.post(URL,data=stale).status_code,409)
                current=approved(client.post(URL,data=form))
                reader.return_value=make_entry(-26)
                self.assertEqual(client.post(URL,data=current).status_code,409)
                reader.return_value=entry
                self.assertEqual(client.post(URL,data=dict(current,confirm="")).status_code,400)
                self.assertEqual(client.post(URL,data=current).status_code,200)
                self.assertEqual(client.post(URL,data=current).status_code,200)
                self.assertEqual(client.post(URL,data=dict(current,evidence_ref="Changed")).status_code,409)
                self.assertEqual(client.post(URL,data=form).status_code,409)
                with connect() as db:
                    head=active(db,ref)
                    self.assertIsNotNone(head)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_creditor_transaction_events").fetchone()[0],1)
                    with self.assertRaises(Conflict):
                        require_source_not_settled(db,ref)
                    for table in ("finance_transactions","finance_bills","hq_receivable_allocations"):
                        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():
                            self.assertEqual(db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0],0)
                self.assertEqual(snapshot(case)[2]["claimed_balance"],12550)
                self.assertEqual(snapshot(case)[2]["revision"],2)
                self.assertIn(b"Active proposal",client.get(URL+"?case="+case).data)
                reader.reset_mock()
                withdraw=dict(action="withdraw",csrf="test-csrf",ref=ref,case=case,
                              manifest=head["manifest"],operation=uuid.uuid4().hex,
                              event=str(head["id"]),confirm="yes")
                self.assertEqual(client.post(URL,data=withdraw).status_code,200)
                self.assertEqual(client.post(URL,data=withdraw).status_code,200)
                reader.assert_not_called()
                with connect() as db:
                    self.assertIsNone(active(db,ref))
                    require_source_not_settled(db,ref)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_creditor_transaction_events").fetchone()[0],2)
                self.assertNotIn(b"Synthetic creditor",app.test_client().get("/book").data)
