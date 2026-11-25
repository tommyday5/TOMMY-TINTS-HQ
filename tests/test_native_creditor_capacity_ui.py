"""Owner-only affordability scenarios on disposable synthetic SQLite storage."""
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
from hq_creditor_capacity import snapshot
from hq_creditor_cases import save_case

ROOT = Path(__file__).resolve().parents[1]
URL = "/hq/finance/creditor-capacity"


class CreditorCapacityUITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name,
            "HQ_SESSION_SECRET": "synthetic-capacity-secret",
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

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        google_sub="synthetic-owner", csrf="test-csrf")

    def fields(self, **changes):
        data = dict(operation=uuid.uuid4().hex, revision="0",
                    csrf="test-csrf", confirm="yes", trading="3000",
                    opex="1000", growth="200", observed_debt="400",
                    registered_debt="500", director_funding="0",
                    director_allowance="300", reserve_target="600", cash="0",
                    reserve_min="100", reserve_rate="20", safe_rate="50",
                    max_rate="80", mode="conservative", as_of="2025-01-01")
        data.update(changes)
        return data

    def test_owner_form_save_reload_history_and_conflicts(self):
        self.assertEqual(self.client.get(URL).status_code, 302)
        self.assertEqual(self.client.post(URL, data=self.fields()).status_code, 302)
        self.login()
        empty = self.client.get(URL)
        self.assertEqual(empty.status_code, 200)
        self.assertIn(b"No scenario saved", empty.data)
        self.assertIn(b"Save and calculate scenario", empty.data)
        first = self.fields()
        response = self.client.post(URL, data=first)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], URL)
        self.assertEqual(self.client.post(URL, data=first).status_code, 303)
        self.assertEqual(self.client.post(URL, data=dict(first, trading="1")).status_code, 409)
        page = self.client.get(URL)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn("£360.00".encode(), page.data)
        self.assertIn("£576.00".encode(), page.data)
        self.assertIn(b'Revision 1', page.data)
        self.assertIn(b'value="3000.00"', page.data)
        self.assertEqual(self.client.post(URL, data=self.fields()).status_code, 409)
        self.assertEqual(self.client.post(URL, data=self.fields(revision="1", trading="0")).status_code, 303)
        history, _ = snapshot()
        self.assertEqual([row["revision"] for row in history], [2, 1])
        self.assertEqual(history[0]["state"]["trading"], 0)
        self.assertEqual(history[1]["state"]["trading"], 300000)
        self.client = app.test_client()
        self.login()
        self.assertIn(b"Revision 2", self.client.get(URL).data)
        self.assertIn(b'value="0.00"', self.client.get(URL).data)
        with connect() as db:
            for table in ("finance_transactions", "finance_bills", "finance_plan"):
                if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0], 0)

    def test_validation_and_incomplete_case_warning(self):
        self.login()
        self.assertEqual(self.client.post(URL, data=self.fields(csrf="wrong")).status_code, 403)
        self.assertEqual(self.client.post(URL, data=self.fields(confirm="")).status_code, 400)
        for changes in (dict(cash=""), dict(safe_rate="101"), dict(max_rate="49"),
                        dict(opex="-1"), dict(as_of="2025-02-30"), dict(mode="invalid")):
            self.assertEqual(self.client.post(URL, data=self.fields(**changes)).status_code, 409)
        self.assertEqual(snapshot()[0], [])
        save_case(uuid.uuid4().hex, uuid.uuid4().hex, 0,
                  dict(creditor="Synthetic private creditor", reference="TEST",
                       scope="business", status="disputed", inclusion="include",
                       committed_monthly="", proposed_monthly="10"), "synthetic-owner")
        self.assertEqual(self.client.post(URL, data=self.fields()).status_code, 303)
        page = self.client.get(URL)
        self.assertIn(b"Native creditor comparison has unresolved", page.data)
        self.assertEqual(snapshot()[1]["incomplete"], 1)
        self.assertIn(b"/hq/finance/creditor-capacity", self.client.get("/hq/finance/creditors").data)
        outsider = app.test_client()
        self.assertNotIn(b"Saved scenario", outsider.get(URL).data)
        self.assertNotIn(b"Creditor affordability scenario", outsider.get("/book").data)
