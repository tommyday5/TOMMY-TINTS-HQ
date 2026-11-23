"""Synthetic HTML and persistence coverage for inactive historical job batches."""
import copy
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
from hq_core_records import _schema
from hq_historical_preview import stage_j_candidate, _candidate_stamp
from hq_historical_job_batches import create_batch, list_batches

ROOT = Path(__file__).resolve().parents[1]


def snapshots():
    jobs = [["Job ID","Date","Customer","Customer Type","Vehicle/Reg","Service 1"]
            + [""]*10 + ["Quoted Price","Status"]]
    for i in range(2):
        jobs.append(["J%04d" % (i+1),"","Synthetic Client","Retail","SYNTH","Tinting"]
                    + [""]*10 + [125,"Quoted"])
    customers = [["Customer","Type","Postcode"],["Synthetic Client","Retail",""]]
    formula = {"valueRanges":[{"range":"Jobs!A4:AZ1000","values":jobs},
                              {"range":"Customers!A4:AF1000","values":customers}]}
    return formula, copy.deepcopy(formula)


class HistoricalJobBatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"HQ_DATA_DIR":tmp.name,
            "GOOGLE_CLIENT_ID":"synthetic","GOOGLE_CLIENT_SECRET":"synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid"})
        env.start()
        self.addCleanup(env.stop)
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.formula, self.computed = snapshots()
        self.original = copy.deepcopy((self.formula,self.computed))
        self.work_ids = [uuid.uuid4().hex for _ in range(2)]
        c, v = uuid.uuid4().hex, uuid.uuid4().hex
        with connect() as db:
            _schema(db)
            db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                       (c,None,"Synthetic Client","fixture@example.invalid","","2026-01-01"))
            db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                       (v,c,"Synthetic","Car","","SYNTH","2026-01-01"))
            for w in self.work_ids:
                db.execute("""INSERT INTO hq_work_records
                    (id,source_request_id,customer_id,vehicle_id,kind,service,notes,
                    preferred_date,preferred_time,status,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (w,"TEST-"+w,c,v,"job","Tinting","","","","draft","2026-01-01"))
        self.ids = [stage_j_candidate(formula=self.formula,computed=self.computed,
                    sheet_row=i+5,job_ref="J%04d" % (i+1),
                    source_stamp=_candidate_stamp(self.formula,self.computed,i+5),
                    work_token=w+":0",operation_id=uuid.uuid4().hex,
                    owner_identity="synthetic-owner") for i,w in enumerate(self.work_ids)]
        self.client = app.test_client()
        self.url = "/hq/migration/job-batches"

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db,"synthetic-owner"),
                        google_sub="synthetic-owner",csrf="test-csrf")

    def data(self, **overrides):
        data = {"csrf":"test-csrf","action":"create","operation_id":"a"*32,
                "candidate_id":self.ids}
        data.update(overrides)
        return data

    def create(self, ids, operation_id):
        return create_batch(ids,operation_id,formula=self.formula,computed=self.computed,
                            owner_identity="synthetic-owner")

    def test_owner_ui_create_reload_retry_archive_and_restage(self):
        self.assertEqual(self.client.get(self.url).status_code,302)
        self.login()
        with patch.dict(app.extensions,{"hq_sheets_connection":lambda:("fixture",9999999999)}), \
             patch("hq_historical_preview._read_live",return_value=(self.formula,self.computed)):
            page = self.client.get(self.url)
            self.assertEqual(page.status_code,200)
            self.assertIn(b"J0001",page.data)
            self.assertIn("no-store",page.headers["Cache-Control"])
            self.assertEqual(self.client.post(self.url,data=self.data(csrf="bad")).status_code,403)
            self.assertEqual(self.client.post(self.url,data=self.data()).status_code,303)
            self.assertEqual(self.client.post(self.url,data=self.data()).status_code,303)
            batch = list_batches()[0]
            self.assertEqual(len(batch["items"]),2)
            self.assertIn(b"Native revision unchanged",self.client.get(self.url).data)
            collision = self.client.post(self.url,data=self.data(operation_id="b"*32))
            self.assertIn(b"Batch could not be saved",collision.data)
            archive = {"csrf":"test-csrf","action":"archive","batch_id":batch["id"],"revision":"0"}
            self.assertEqual(self.client.post(self.url,data=archive).status_code,303)
            self.assertEqual(self.client.post(self.url,data=archive).status_code,303)
            self.assertIn(b"Archived",self.client.get(self.url).data)
            self.assertEqual(self.client.post(self.url,data=self.data(operation_id="c"*32)).status_code,303)
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hq_historical_job_batches").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_historical_job_batch_events").fetchone()[0],3)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_work_records WHERE revision=0 AND status='draft'").fetchone()[0],2)
        self.assertEqual((self.formula,self.computed),self.original)

    def test_all_or_nothing_on_changed_source_or_native(self):
        self.computed["valueRanges"][0]["values"][2][16] = 126
        with self.assertRaises(ValueError):
            self.create(self.ids,"d"*32)
        self.computed = copy.deepcopy(self.original[1])
        self.assertEqual(list_batches(),[])
        with connect() as db:
            db.execute("UPDATE hq_work_records SET revision=1 WHERE id=?",(self.work_ids[1],))
        with self.assertRaises(ValueError):
            self.create(self.ids,"e"*32)
        self.assertEqual(list_batches(),[])

    def test_selection_validation_and_stale_local_warning(self):
        with self.assertRaises(ValueError):
            self.create([self.ids[0],self.ids[0]],"f"*32)
        batch = self.create([self.ids[0]],"f"*32)
        with self.assertRaises(ValueError):
            self.create([self.ids[1]],"f"*32)
        with connect() as db:
            db.execute("UPDATE hq_work_records SET revision=1 WHERE id=?",(self.work_ids[0],))
        self.assertEqual(list_batches()[0]["id"],batch)
        self.assertEqual(list_batches()[0]["items"][0]["stale"],1)
