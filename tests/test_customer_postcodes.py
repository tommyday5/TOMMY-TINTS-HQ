"""Synthetic native customer postcode integration and backup-copy tests."""
import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import _schema
from hq_customer_archive import set_archived
from hq_customer_portal import issue_customer_invite, redeem_customer_invite
from hq_customer_postcodes import normalize_postcode, save_postcode, snapshot

ROOT = Path(__file__).resolve().parents[1]


class CustomerPostcodeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR":tmp.name,
            "HQ_SESSION_SECRET":"synthetic-postcode-fixture-secret",
            "GOOGLE_CLIENT_ID":"synthetic",
            "GOOGLE_CLIENT_SECRET":"synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        ctx=app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.customer=uuid.uuid4().hex
        self.other=uuid.uuid4().hex
        with closing(connect()) as db, db:
            _schema(db)
            for ident in (self.customer,self.other):
                db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                           (ident,None,"Synthetic Customer","","","2026-01-01"))
        self.client=app.test_client()
        self.url="/hq/customers/"+self.customer+"/postcode"

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db,"synthetic-owner"),
                        google_sub="synthetic-owner",csrf="test-csrf")

    def data(self, **changes):
        data={"csrf":"test-csrf","revision":"0","operation_id":"a"*32,
              "postcode":"sw1a1aa"}
        data.update(changes)
        return data

    def test_owner_create_reload_change_clear_and_audit(self):
        self.assertEqual(self.client.get(self.url).status_code,302)
        self.assertEqual(self.client.post(self.url,data=self.data()).status_code,302)
        self.login()
        page=self.client.get(self.url)
        self.assertEqual(page.status_code,200)
        self.assertIn(b"Not recorded",page.data)
        self.assertIn("no-store",page.headers["Cache-Control"])
        self.assertEqual(self.client.post(self.url,data=self.data(csrf="bad")).status_code,403)
        self.assertEqual(self.client.post(self.url,data=self.data(postcode="not a code")).status_code,409)
        self.assertEqual(self.client.post(self.url,data=self.data()).status_code,303)
        self.assertEqual(self.client.post(self.url,data=self.data()).status_code,303)
        self.assertIn(b"SW1A 1AA",self.client.get(self.url).data)
        self.assertEqual(self.client.post(self.url,data=self.data(postcode="EC1A 1BB")).status_code,409)
        self.assertEqual(self.client.post(self.url,data=self.data(operation_id="b"*32)).status_code,409)
        self.assertEqual(self.client.post(self.url,data=self.data(operation_id="c"*32,
            revision="1",postcode="EC1A 1BB")).status_code,303)
        self.assertEqual(self.client.post(self.url,data=self.data(operation_id="d"*32,
            revision="2",postcode="")).status_code,303)
        _,status,history=snapshot(self.customer)
        self.assertEqual((status["postcode"],status["revision"]),("",3))
        self.assertEqual([r["revision"] for r in history],[3,2,1])
        self.assertEqual(snapshot(self.other)[1]["revision"],0)
        with closing(connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hq_customer_postcode_events").fetchone()[0],3)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_customer_postcode_operations").fetchone()[0],3)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_customers").fetchone()[0],2)

    def test_archived_customer_and_synthetic_backup_reopen(self):
        self.login()
        self.assertEqual(self.client.post(self.url,data=self.data()).status_code,303)
        set_archived(self.customer,True,0,"b"*32,"synthetic-owner")
        self.assertEqual(self.client.post(self.url,data=self.data(operation_id="c"*32,
            revision="1",postcode="EC1A 1BB")).status_code,409)
        self.assertIn(b"Restore the archived customer",self.client.get(self.url).data)
        # Copy an isolated fixture to a separate SQLite file and reopen that copy;
        # no production volume or credentials are used.
        backup=self.root/"restore-fixture.sqlite3"
        with closing(connect()) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        with closing(sqlite3.connect(backup)) as restored:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            self.assertEqual(restored.execute("PRAGMA foreign_key_check").fetchall(),[])
            self.assertEqual(restored.execute("SELECT postcode,revision FROM hq_customer_postcodes WHERE customer_id=?",
                (self.customer,)).fetchone(),("SW1A 1AA",1))
            self.assertEqual(restored.execute("SELECT count(*) FROM hq_customer_postcode_events").fetchone()[0],1)
            self.assertEqual(restored.execute("SELECT is_archived FROM hq_customer_archive_state WHERE customer_id=?",
                (self.customer,)).fetchone()[0],1)
        self.assertEqual(snapshot(self.customer)[1]["postcode"],"SW1A 1AA")

    def test_customer_portal_session_cannot_edit_or_view_owner_postcode(self):
        save_postcode(self.customer,"SW1A 1AA",0,"f"*32,"synthetic-owner")
        invite=issue_customer_invite(self.customer,"e"*32)
        sid=redeem_customer_invite(invite,"synthetic-client")
        self.assertTrue(sid)
        with self.client.session_transaction() as sess:
            sess["customer_portal_sid"]=sid
            sess["customer_portal_csrf"]="customer-only-csrf"
        account=self.client.get("/customer/account")
        self.assertEqual(account.status_code,200)
        self.assertNotIn(b"SW1A 1AA",account.data)
        self.assertEqual(self.client.get(self.url).status_code,302)
        self.assertEqual(self.client.get("/hq/customers/"+self.other+"/postcode").status_code,302)
        self.assertEqual(self.client.post(self.url,data=self.data(csrf="customer-only-csrf")).status_code,302)
        self.assertEqual(snapshot(self.customer)[1]["postcode"],"SW1A 1AA")

    def test_format_and_missing_customer(self):
        self.assertEqual(normalize_postcode("gir 0aa"),"GIR 0AA")
        self.assertEqual(normalize_postcode("   "),"")
        for value in ("ABC123","SW1A 1A","SW1A 1AA\n","<script>"):
            with self.assertRaises(ValueError):
                normalize_postcode(value)
        self.login()
        missing="/hq/customers/"+uuid.uuid4().hex+"/postcode"
        self.assertEqual(self.client.get(missing).status_code,404)
        self.assertEqual(self.client.post(missing,data=self.data()).status_code,404)


if __name__=="__main__":
    unittest.main()
