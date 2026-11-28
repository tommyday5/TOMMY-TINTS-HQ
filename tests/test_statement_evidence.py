"""Synthetic statement staging, explicit approval, conflict and history tests."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_statement_evidence import prepare, save, withdraw, listing

MAPPING=dict(transaction_id="id",amount_pence="pence",currency="ccy",booked_at="date")
CSV="id,pence,ccy,date\nsynthetic-a,500,GBP,2026-11-01T12:00:00Z\nsynthetic-b,-100,GBP,2026-11-02T12:00:00Z\n"

class StatementEvidenceTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ,{"HQ_DATA_DIR":tmp.name,
            "HQ_SESSION_SECRET":"synthetic-statement-test-secret",
            "GOOGLE_CLIENT_ID":"synthetic","GOOGLE_CLIENT_SECRET":"synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid"})
        env.start()
        self.addCleanup(env.stop)
        ctx=app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.client=app.test_client()

    def preview(self, csv=CSV, closing=1400, start="2026-11-01", end="2026-11-30"):
        return prepare(csv,MAPPING,"synthetic-account",start,end,1000,closing)

    def test_save_replay_overlap_withdraw_and_conflict(self):
        p=self.preview()
        self.assertTrue(p["manifest"]["rollforward"]["arithmetically_consistent"])
        self.assertFalse(p["manifest"]["rollforward"]["bank_balance_verified"])
        self.assertEqual(listing(),[])
        with self.assertRaises(ValueError): save(p,"wrong","owner")
        bad=self.preview(closing=1401)
        with self.assertRaises(ValueError): save(bad,bad["stamp"],"owner")
        ref=save(p,p["stamp"],"owner")
        self.assertEqual(save(p,p["stamp"],"owner"),ref)
        q=self.preview(end="2026-11-29")
        with self.assertRaises(ValueError): save(q,q["stamp"],"owner")
        withdraw(ref,"Synthetic review","owner")
        withdraw(ref,"Synthetic review","owner")
        with self.assertRaises(ValueError): save(p,p["stamp"],"owner")
        changed=self.preview(csv=CSV.replace(",500,",",501,"),closing=1401)
        with self.assertRaises(ValueError): save(changed,changed["stamp"],"owner")
        self.assertEqual(len(listing()),1)
        self.assertEqual(listing()[0]["withdrawal_reason"],"Synthetic review")
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_statement_events").fetchone()[0],2)

    def test_reject_duplicates_and_outside_period(self):
        with self.assertRaises(ValueError):
            self.preview(csv=CSV+CSV.splitlines()[1]+"\n")
        with self.assertRaises(ValueError): self.preview(start="2026-11-02")

    def test_owner_http_exact_approval_and_private_history(self):
        url="/hq/finance/statement-evidence"
        self.assertEqual(self.client.get(url).status_code,302)
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db,"synthetic-owner"),google_sub="synthetic-owner",csrf="statement-csrf")
        form=dict(csrf="statement-csrf",action="preview",account="synthetic-account",
            start="2026-11-01",end="2026-11-30",opening="1000",closing="1400",
            csv_text=CSV,mapping=json.dumps(MAPPING))
        self.assertEqual(self.client.post(url,data=dict(form,csrf="bad")).status_code,403)
        response=self.client.post(url,data=form)
        self.assertEqual(response.status_code,200)
        self.assertIn(self.preview()["stamp"].encode(),response.data)
        self.assertEqual(listing(),[])
        form.update(action="save",stamp=self.preview()["stamp"])
        self.assertEqual(self.client.post(url,data=form).status_code,400)
        self.assertEqual(self.client.post(url,data=dict(form,confirm="yes",closing="1401")).status_code,400)
        self.assertEqual(self.client.post(url,data=dict(form,confirm="yes")).status_code,200)
        fresh=self.client.get(url)
        self.assertEqual(fresh.status_code,200)
        self.assertIn("no-store",fresh.headers["Cache-Control"])
        self.assertIn(b"synthetic-account",fresh.data)
        self.assertIn(b"not bank verified",fresh.data)
        self.assertIn(b"/hq/finance/statement-evidence",self.client.get("/hq/finance/command").data)
        self.assertEqual(len(listing()),1)
        self.assertEqual(self.client.post(url,data=dict(csrf="statement-csrf",action="withdraw",
            batch=self.preview()["stamp"],reason="Synthetic correction",confirm="yes")).status_code,200)
        self.assertIn(b"Synthetic correction",self.client.get(url).data)
