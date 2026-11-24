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
from hq_historical_preview import stage_j_candidate, _candidate_stamp, withdraw_j_candidate
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
            "HQ_SESSION_SECRET":"synthetic-historical-batch-test-secret",
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

    def test_restaged_candidate_cannot_bypass_active_batch_collision(self):
        self.create([self.ids[0]],"f"*32)
        withdraw_j_candidate(self.ids[0],0,"synthetic-owner")
        new = stage_j_candidate(formula=self.formula,computed=self.computed,
                    sheet_row=5,job_ref="J0001",
                    source_stamp=_candidate_stamp(self.formula,self.computed,5),
                    work_token=self.work_ids[0]+":0",operation_id=uuid.uuid4().hex,
                    owner_identity="synthetic-owner")
        with self.assertRaises(ValueError):
            self.create([new],"a"*32)
        self.assertEqual(len(list_batches()),1)

    def full_snapshots(self):
        from hq_historical_job_full_evidence import HEADERS, WIDTH
        from hq_historical_preview import WORKBOOK
        self.assertEqual(len(HEADERS), 94)
        self.assertEqual(HEADERS[53], "Deposit Received £")
        self.assertEqual(HEADERS[-1], "Work Release Gate")
        full = []
        for legacy in (self.formula, self.computed):
            rows = legacy["valueRanges"][0]["values"]
            rows[0] = list(HEADERS[:52])
            expanded = [list(HEADERS)]
            for row in rows[1:]:
                expanded.append(row + [""] * (WIDTH - len(row)))
                expanded[-1][53] = 75
                expanded[-1][57] = "Unpaid"
                expanded[-1][93] = "Awaiting agreement"
            full.append({"spreadsheetId": WORKBOOK, "valueRanges": [
                {"range": "Jobs!A4:CP1000", "values": expanded}]})
        return full

    def test_full_width_ui_preserves_legacy_and_rejects_late_column_changes(self):
        from hq_historical_job_full_evidence import save_full_width_evidence
        batch = self.create(self.ids, "a"*32)
        full = self.full_snapshots()
        self.login()
        data = {"csrf":"test-csrf","action":"full_evidence","batch_id":batch,"revision":"0"}
        with patch.dict(app.extensions,{"hq_sheets_connection":lambda:("fixture",9999999999)}), \
             patch("hq_historical_preview._read_live",return_value=(self.formula,self.computed)), \
             patch("hq_historical_job_full_evidence.read_full_width",return_value=full):
            self.assertIn(b"No 94-column source evidence saved", self.client.get(self.url).data)
            self.assertEqual(self.client.post(self.url,data=dict(data,csrf="bad")).status_code,403)
            self.assertEqual(self.client.post(self.url,data=data).status_code,303)
            self.assertIn(b"94-column source evidence v2 saved",self.client.get(self.url).data)
            self.assertEqual(self.client.post(self.url,data=data).status_code,303)
            full[1]["valueRanges"][0]["values"][1][93] = "Changed"
            self.assertIn(b"Changed full-width evidence requires archive",self.client.post(self.url,data=data).data)
        with connect() as db:
            records = db.execute("SELECT * FROM hq_historical_job_batch_full_evidence").fetchall()
            self.assertEqual(len(records),1)
            self.assertEqual(records[0]["version"],2)
            self.assertNotIn("Awaiting agreement",records[0]["evidence"])
            self.assertNotIn("Synthetic Client",records[0]["evidence"])
            self.assertEqual(db.execute("SELECT count(*) FROM hq_work_records WHERE revision=0 AND status='draft'").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_historical_j_candidates WHERE withdrawn_at IS NULL").fetchone()[0],2)
        self.assertIsNotNone(list_batches()[0]["full_evidence_at"])

    def test_full_width_fail_closed_on_incomplete_range_and_source_race(self):
        from hq_historical_job_full_evidence import save_full_width_evidence
        batch = self.create(self.ids,"a"*32)
        full = self.full_snapshots()
        def save(pair):
            save_full_width_evidence(batch,legacy_formula=self.formula,legacy_computed=self.computed,
                                     full_formula=pair[0],full_computed=pair[1],owner_identity="synthetic-owner")
        for index, value in ((16,126),(0,"J9999")):
            changed = copy.deepcopy(full)
            changed[1]["valueRanges"][0]["values"][1][index] = value
            with self.assertRaises(ValueError):
                save(changed)
        changed = copy.deepcopy(full)
        changed[0]["valueRanges"][0]["range"] = "Jobs!A4:AZ1000"
        with self.assertRaises(ValueError):
            save(changed)
        changed = copy.deepcopy(full)
        changed[0]["valueRanges"][0]["values"][0][93] = "Different header"
        with self.assertRaises(ValueError):
            save(changed)
        self.assertIsNone(list_batches()[0]["full_evidence_at"])
        save(full)
        with connect() as db:
            db.execute("UPDATE hq_work_records SET revision=1 WHERE id=?",(self.work_ids[0],))
        with self.assertRaises(ValueError):
            save(full)


    def test_field_review_ui_revision_history_and_source_guards(self):
        from hq_historical_job_field_review import save_plan
        batch = self.create(self.ids, "a"*32)
        full = self.full_snapshots()
        self.login()
        data = {"csrf":"test-csrf","action":"field_review","batch_id":batch,"plan_revision":"0",
                **{"f"+str(i):"u" for i in range(94)}}
        data.update(f0="h", f16="n", f53="v")
        evidence = dict(csrf="test-csrf",action="full_evidence",batch_id=batch,revision="0")
        with patch.dict(app.extensions,{"hq_sheets_connection":lambda:("fixture",9999999999)}), patch("hq_historical_preview._read_live",return_value=(self.formula,self.computed)), patch("hq_historical_job_full_evidence.read_full_width",return_value=full):
            self.assertIn(b"Batch could not be saved", self.client.post(self.url,data=data).data)
            self.assertEqual(self.client.post(self.url,data=evidence).status_code,303)
            self.assertIn(b"Save field review plan",self.client.get(self.url).data)
            self.assertEqual(self.client.post(self.url,data=dict(data,csrf="bad")).status_code,403)
            self.assertIn(b"Batch could not be saved",self.client.post(self.url,data=dict(data,f53="n")).data)
            self.assertEqual(self.client.post(self.url,data=data).status_code,303)
            self.assertEqual(self.client.post(self.url,data=data).status_code,303)
            self.assertIn(b"3/94 classified",self.client.get(self.url).data)
            self.assertIn(b"Batch could not be saved",self.client.post(self.url,data=dict(data,f1="h")).data)
            self.assertEqual(self.client.post(self.url,data=dict(data,plan_revision="1",f1="h")).status_code,303)
            self.assertEqual(list_batches()[0]["field_plan"]["revision"],2)
            full[1]["valueRanges"][0]["values"][1][93]="Changed late field"
            self.assertIn(b"Batch could not be saved",self.client.post(self.url,data=dict(data,plan_revision="2")).data)
            full[1]["valueRanges"][0]["values"][1][93]="Awaiting agreement"
            archive=dict(csrf="test-csrf",action="archive",batch_id=batch,revision="0")
            self.assertEqual(self.client.post(self.url,data=archive).status_code,303)
            self.assertIn(b"4/94 classified",self.client.get(self.url).data)
            self.assertIn(b"Batch could not be saved",self.client.post(self.url,data=dict(data,plan_revision="2")).data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hq_historical_job_field_review_events").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_work_records WHERE revision=0 AND status='draft'").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_historical_j_candidates WHERE withdrawn_at IS NULL").fetchone()[0],2)

    def test_dry_run_saved_owner_comparison_missing_price_and_stale_plan(self):
        from hq_historical_job_dry_run import _pence
        self.assertEqual(_pence(0), 0)
        self.assertEqual(_pence(125.23), 12523)
        for invalid in ("125", "", True, float("nan"), float("inf"), -1, 1.001):
            self.assertIsNone(_pence(invalid))
        with connect() as db:
            db.execute("UPDATE hq_work_records SET price_pence=12500 WHERE id=?", (self.work_ids[0],))
        batch = self.create(self.ids, "a"*32)
        full = self.full_snapshots()
        self.login()
        evidence = dict(csrf="test-csrf",action="full_evidence",batch_id=batch,revision="0")
        fields = dict(csrf="test-csrf",action="field_review",batch_id=batch,plan_revision="0",
                      **{"f"+str(i):"u" for i in range(94)})
        fields["f16"] = "n"
        dry = dict(csrf="test-csrf",action="dry_run",batch_id=batch,plan_revision="1",dry_revision="0")
        with patch.dict(app.extensions,{"hq_sheets_connection":lambda:("fixture",9999999999)}), patch("hq_historical_preview._read_live",return_value=(self.formula,self.computed)), patch("hq_historical_job_full_evidence.read_full_width",return_value=full):
            self.assertEqual(self.client.post(self.url,data=evidence).status_code,303)
            self.assertIn(b"Batch could not be saved",self.client.post(self.url,data=dry).data)
            self.assertEqual(self.client.post(self.url,data=fields).status_code,303)
            self.assertEqual(self.client.post(self.url,data=dict(dry,csrf="bad")).status_code,403)
            self.assertEqual(self.client.post(self.url,data=dry).status_code,303)
            self.assertEqual(self.client.post(self.url,data=dry).status_code,303)
            snapshot = list_batches()[0]["dry_run"]
            self.assertEqual(snapshot["revision"],1)
            rows = snapshot["report"]["rows"]
            self.assertEqual(rows[0]["quote_comparison"],"Equal")
            self.assertEqual(rows[1]["quote_comparison"],"Unknown / missing")
            self.assertIsNone(rows[1]["native_quote_pence"])
            self.assertEqual(rows[0]["customer_name_comparison"],"Equal")
            self.assertEqual(snapshot["report"]["unresolved_fields"],93)
            page = self.client.get(self.url)
            self.assertIn(b"Saved dry-run comparison",page.data)
            self.assertIn(b"Historical quote:",page.data)
            self.assertIn(b"Unknown / missing",page.data)
            self.assertEqual(self.client.post(self.url,data=dict(fields,plan_revision="1",f1="h")).status_code,303)
            self.assertIn(b"Field plan changed since this comparison",self.client.get(self.url).data)
            self.assertIn(b"Batch could not be saved",self.client.post(self.url,data=dry).data)
            self.assertEqual(self.client.post(self.url,data=dict(dry,plan_revision="2",dry_revision="1")).status_code,303)
            full[1]["valueRanges"][0]["values"][1][93] = "Changed"
            self.assertIn(b"Batch could not be saved",self.client.post(self.url,data=dict(dry,plan_revision="2",dry_revision="2")).data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hq_historical_job_dry_runs").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_work_records WHERE revision=0 AND status='draft'").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT price_pence FROM hq_work_records WHERE id=?", (self.work_ids[0],)).fetchone()[0],12500)
            self.assertIsNone(db.execute("SELECT price_pence FROM hq_work_records WHERE id=?", (self.work_ids[1],)).fetchone()[0])

    def test_dry_run_customer_proposal_crosscheck_is_inactive_and_versioned(self):
        from hq_historical_customer_links import stage_customer_candidate, source_stamp, withdraw_customer_candidate
        from hq_historical_job_dry_run import _customer_crosscheck
        batch = self.create(self.ids, "a"*32)
        full = self.full_snapshots()
        with connect() as db:
            customer_id = db.execute("SELECT customer_id FROM hq_work_records WHERE id=?",
                                     (self.work_ids[0],)).fetchone()[0]
        candidate = stage_customer_candidate(
            formula=self.formula, computed=self.computed, sheet_row=5,
            expected_source_stamp=source_stamp(self.formula,self.computed,5),
            customer_id=customer_id, basis="name_crosscheck",
            operation_id=uuid.uuid4().hex, owner_identity="synthetic-owner")
        self.login()
        evidence = dict(csrf="test-csrf",action="full_evidence",batch_id=batch,revision="0")
        fields = dict(csrf="test-csrf",action="field_review",batch_id=batch,plan_revision="0",
                      **{"f"+str(i):"u" for i in range(94)})
        dry = dict(csrf="test-csrf",action="dry_run",batch_id=batch,plan_revision="1",dry_revision="0")
        with patch.dict(app.extensions,{"hq_sheets_connection":lambda:("fixture",9999999999)}), patch("hq_historical_preview._read_live",return_value=(self.formula,self.computed)), patch("hq_historical_job_full_evidence.read_full_width",return_value=full):
            self.assertEqual(self.client.post(self.url,data=evidence).status_code,303)
            self.assertEqual(self.client.post(self.url,data=fields).status_code,303)
            self.assertEqual(self.client.post(self.url,data=dry).status_code,303)
            result = list_batches()[0]["dry_run"]["report"]["rows"][0]["customer_proposal"]
            self.assertEqual(result["status"],"Current inactive proposal cross-check agrees")
            self.assertEqual(result["proposal_id"],candidate)
            self.assertEqual(result["customer_sheet_row"],5)
            self.assertIn(b"Name-only review",self.client.get(self.url).data)
            with connect() as db:
                item = {"customer_id":customer_id,"vehicle_customer":customer_id}
                self.assertIn("not literal",_customer_crosscheck(db,item,"=A1","Synthetic Client",self.formula,self.computed)["status"])
                self.assertEqual(_customer_crosscheck(db,dict(item,vehicle_customer="other"),"Synthetic Client","Synthetic Client",self.formula,self.computed)["status"],"Native vehicle/customer conflict")
                changed = copy.deepcopy(self.computed)
                changed["valueRanges"][1]["values"][1][2] = "changed"
                self.assertEqual(_customer_crosscheck(db,item,"Synthetic Client","Synthetic Client",self.formula,changed)["status"],"Customer proposal stale or ambiguous")
            withdraw_customer_candidate(candidate,0,"synthetic-owner")
            self.assertEqual(self.client.post(self.url,data=dict(dry,dry_revision="1")).status_code,303)
            snapshot = list_batches()[0]["dry_run"]
            self.assertEqual(snapshot["revision"],2)
            self.assertEqual(snapshot["report"]["rows"][0]["customer_proposal"]["status"],"No active customer proposal")
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hq_historical_job_dry_runs").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_work_records WHERE revision=0 AND status='draft'").fetchone()[0],2)

    def test_explicit_source_stage_preserves_formula_effective_cells_and_history(self):
        import json
        batch = self.create(self.ids, "a"*32)
        full = self.full_snapshots()
        full[0]["valueRanges"][0]["values"][1][53] = "=25+50"
        self.login()
        evidence = dict(csrf="test-csrf",action="full_evidence",batch_id=batch,revision="0")
        fields = dict(csrf="test-csrf",action="field_review",batch_id=batch,plan_revision="0",
                      **{"f"+str(i):"u" for i in range(94)})
        fields["f16"] = "n"
        dry = dict(csrf="test-csrf",action="dry_run",batch_id=batch,plan_revision="1",dry_revision="0")
        stage = dict(csrf="test-csrf",action="stage_source",batch_id=batch,plan_revision="1",
                     dry_revision="1",confirm_stage="yes")
        with patch.dict(app.extensions,{"hq_sheets_connection":lambda:("fixture",9999999999)}), patch("hq_historical_preview._read_live",return_value=(self.formula,self.computed)), patch("hq_historical_job_full_evidence.read_full_width",return_value=full):
            self.assertEqual(self.client.post(self.url,data=evidence).status_code,303)
            self.assertEqual(self.client.post(self.url,data=fields).status_code,303)
            self.assertIn(b"Batch could not be saved",self.client.post(self.url,data=stage).data)
            self.assertEqual(self.client.post(self.url,data=dry).status_code,303)
            self.assertIn(b"Save inactive source rows",self.client.get(self.url).data)
            self.assertIn(b"Batch could not be saved",self.client.post(self.url,data=dict(stage,confirm_stage="")).data)
            self.assertEqual(self.client.post(self.url,data=dict(stage,csrf="bad")).status_code,403)
            self.assertEqual(app.test_client().post(self.url,data=stage).status_code,302)
            self.assertIn(b"Batch could not be saved",self.client.post(self.url,data=dict(stage,dry_revision="2")).data)
            full[1]["valueRanges"][0]["values"][1][53] = 76
            self.assertIn(b"Batch could not be saved",self.client.post(self.url,data=stage).data)
            full[1]["valueRanges"][0]["values"][1][53] = 75
            self.assertEqual(self.client.post(self.url,data=stage).status_code,303)
            self.assertEqual(self.client.post(self.url,data=stage).status_code,303)
            self.assertIn(b"Source rows staged: 2",self.client.get(self.url).data)
            self.assertTrue(list_batches()[0]["source_stage"]["row_count_consistent"])
            mapping = self.url + "/" + batch + "/mapping/" + self.ids[0]
            self.assertEqual(app.test_client().get(mapping).status_code,302)
            self.assertIn(b"edit quote mapping draft",self.client.get(self.url).data)
            page = self.client.get(mapping)
            self.assertEqual(page.status_code,200)
            self.assertIn("no-store",page.headers["Cache-Control"])
            self.assertIn(b"=25+50",page.data)
            self.assertIn(b"Original staged source cells (94 columns)",page.data)
            decision = dict(csrf="test-csrf",revision="0",decision="propose")
            self.assertEqual(self.client.post(mapping,data=dict(decision,csrf="bad")).status_code,403)
            self.assertEqual(self.client.post(mapping,data=decision).status_code,303)
            self.assertEqual(self.client.post(mapping,data=decision).status_code,303)
            self.assertIn(b"Revision 1",self.client.get(mapping).data)
            self.assertIn(b"Mapping not saved",self.client.post(mapping,data=dict(decision,decision="preserve")).data)
            self.assertEqual(self.client.post(mapping,data=dict(decision,revision="1",decision="preserve")).status_code,303)
            with connect() as db:
                events = db.execute("SELECT decision,source_pence,native_pence FROM hq_historical_job_quote_mapping ORDER BY revision").fetchall()
                self.assertEqual([tuple(e) for e in events],[("propose",12500,None),("preserve",12500,None)])
                self.assertIsNone(db.execute("SELECT price_pence FROM hq_work_records WHERE id=?",(self.work_ids[0],)).fetchone()[0])
            self.assertEqual(self.client.post(self.url,data=dict(fields,plan_revision="1",f0="h")).status_code,303)
            self.assertIn(b"Review changed since staging",self.client.get(self.url).data)
            self.assertIn(b"editing disabled",self.client.get(mapping).data)
            self.assertIn(b"Mapping not saved",self.client.post(mapping,data=dict(decision,revision="2")).data)
            self.assertIn(b"Batch could not be saved",self.client.post(self.url,data=stage).data)
            self.assertEqual(self.client.post(self.url,data=dict(csrf="test-csrf",action="archive",batch_id=batch,revision="0")).status_code,303)
            self.assertIn(b"Source rows staged: 2",self.client.get(self.url).data)
            self.assertIn(b"Batch could not be saved",self.client.post(self.url,data=stage).data)
        with connect() as db:
            rows = db.execute("SELECT * FROM hq_historical_job_source_rows ORDER BY sheet_row").fetchall()
            self.assertEqual(len(rows),2)
            self.assertEqual(rows[0]["job_ref"],"J0001")
            self.assertEqual(len(json.loads(rows[0]["raw_cells"])),94)
            self.assertEqual(json.loads(rows[0]["raw_cells"])[53],"=25+50")
            self.assertEqual(json.loads(rows[0]["effective_cells"])[53],75)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_historical_job_source_stages").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_work_records WHERE revision=0 AND status='draft'").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_historical_j_candidates WHERE withdrawn_at IS NULL").fetchone()[0],2)
