"""Synthetic-only migration dry-run tests. No real Sheets API or live writes."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import _schema
from customer_request_store import connect
from hq_historical_preview import (build_preview, _read_live, _native_inventory,
    _candidate_stamp, stage_j_candidate, withdraw_j_candidate, _current_candidates)

ROOT = Path(__file__).resolve().parents[1]


def snapshots():
    jobs = [
        ["Job ID", "Date", "Customer", "Customer Type", "Vehicle/Reg",
         "Service 1"] + [""] * 10 + ["Quoted Price", "Status"],
        ["J0081", "2026-02-01", "Synthetic A", "Retail", "FAKE", "Tinting"]
        + [""] * 10 + [250, "Completed"],
        ["J0082", "2026-02-02", "Synthetic B", "Trade", "TEST", "Wrapping"]
        + [""] * 10 + ["=SUM(100,100)", "Quoted"],
        ["J0082", "2026-02-03", "Synthetic B", "Trade", "TEST", "Wrapping"]
        + [""] * 10 + [210, "Quoted"],
        ["", "", "Missing Reference"],
    ]
    cust = [["Customer", "Type", "Postcode"],
            ["Synthetic A", "Retail", "XX0 0XX"],
            ["Synthetic A", "Retail", "XX0 0XX"],
            ["Synthetic B", "Trade", "XX0 0XX"]]
    def response(rows):
        return {"valueRanges": [
            {"range": "Jobs!A4:AZ1000", "values": rows[0]},
            {"range": "Customers!A4:AF1000", "values": rows[1]},
        ]}
    formula = response([jobs, cust])
    effective_jobs = [r[:] for r in jobs]
    effective_jobs[2] = jobs[2][:]
    effective_jobs[2][16] = 200
    return formula, response([effective_jobs, [row[:] for row in cust]])


class HistoricalPreviewTests(unittest.TestCase):
    def test_duplicate_missing_and_hq_candidates_are_advisory(self):
        formula, actual = snapshots()
        result = build_preview(formula, actual, native={
            "names": {"synthetic a"}, "customers": 3, "works": 2})
        self.assertEqual(result["jobs_rows"], 4)
        self.assertEqual(result["valid_unique_j_refs"], 1)
        self.assertEqual(result["duplicate_j_rows"], 2)
        self.assertEqual(result["missing_or_invalid_j_rows"], 1)
        self.assertEqual(result["customers_named_rows"], 3)
        self.assertEqual(result["duplicate_customer_name_rows"], 2)
        self.assertEqual(result["possible_native_customer_rows"], 2)
        self.assertEqual(result["possible_native_job_rows"], 1)
        self.assertEqual(result["sample"][1]["price_kind"], "formula")
        self.assertFalse(result["import_authorised"])
        self.assertFalse(result["source_of_truth_changed"])
        self.assertFalse(result["identity_verified"])
        self.assertNotIn("Synthetic A", str(result))
        self.assertNotIn("FAKE", str(result))
        self.assertNotIn("250", str(result))

    def test_no_native_db_does_not_create_one_or_invent_matches(self):
        formula, actual = snapshots()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"HQ_DATA_DIR": directory}):
            self.assertIsNone(_native_inventory())
            self.assertFalse((Path(directory) / "customer_requests.sqlite3").exists())
            result = build_preview(formula, actual, native=None)
        self.assertIsNone(result["possible_native_customer_rows"])
        self.assertFalse(result["native_available"])

    def test_header_change_and_missing_range_fail_closed(self):
        formula, actual = snapshots()
        actual["valueRanges"][0]["values"][0][0] = "Job Number"
        with self.assertRaisesRegex(ValueError, "header"):
            build_preview(formula, actual)
        formula, actual = snapshots()
        actual["valueRanges"].pop()
        with self.assertRaises(ValueError):
            build_preview(formula, actual)

    def test_literal_identity_changes_between_reads_rejected(self):
        formula, actual = snapshots()
        actual["valueRanges"][0]["values"][1][2] = "Different Synthetic"
        with self.assertRaisesRegex(ValueError, "changed"):
            build_preview(formula, actual)

    def test_formula_derived_customer_name_is_not_identity(self):
        formula, actual = snapshots()
        formula["valueRanges"][1]["values"].append(["=A5"])
        actual["valueRanges"][1]["values"].append(["Synthetic A"])
        result = build_preview(formula, actual)
        self.assertEqual(result["formula_customer_name_rows"], 1)
        self.assertEqual(result["customers_named_rows"], 3)

    def test_lower_case_ref_requires_review_not_normalised_import(self):
        formula, actual = snapshots()
        formula["valueRanges"][0]["values"][1][0] = "j0081"
        actual["valueRanges"][0]["values"][1][0] = "j0081"
        result = build_preview(formula, actual)
        self.assertEqual(result["sample"][0]["status"], "missing_or_invalid_id")
        self.assertIsNone(result["sample"][0]["ref"])

    def test_provider_is_only_get_and_checks_sheet_boundaries(self):
        formula, actual = snapshots()
        meta = {"spreadsheetId": "1B0XULUDiMlVuW9u0kUOYem8WelBtOH-Xey_wWVYdb2M",
                "sheets": [{"properties": {"title": "Jobs", "sheetId": 330634230,
                         "gridProperties": {"rowCount": 1000}}},
                           {"properties": {"title": "Customers", "sheetId": 378990021,
                         "gridProperties": {"rowCount": 1000}}}]}
        with patch("hq_historical_preview.requests.get") as get:
            get.side_effect = [Mock(json=lambda: meta, raise_for_status=lambda: None),
                               Mock(json=lambda: formula, raise_for_status=lambda: None),
                               Mock(json=lambda: actual, raise_for_status=lambda: None)]
            f, v = _read_live("fixture-token")
        self.assertEqual((f, v), (formula, actual))
        self.assertEqual(get.call_count, 3)
        self.assertIn("FORMULA", str(get.call_args_list[1]))
        self.assertIn("UNFORMATTED_VALUE", str(get.call_args_list[2]))
        meta["sheets"][0]["properties"]["gridProperties"]["rowCount"] = 1001
        with patch("hq_historical_preview.requests.get") as get:
            get.return_value = Mock(json=lambda: meta, raise_for_status=lambda: None)
            with self.assertRaisesRegex(ValueError, "bounds"):
                _read_live("fixture-token")
            self.assertEqual(get.call_count, 1)

    def test_owner_only_csrf_and_get_never_calls_provider(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            env = patch.dict(os.environ, {
                "HQ_DATA_DIR": directory, "HQ_SESSION_SECRET": "fixture",
                "GOOGLE_CLIENT_ID": "fixture",
                "GOOGLE_CLIENT_SECRET": "fixture",
                "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
            })
            with env, patch("hq_historical_preview._read_live") as read:
                client = app.test_client()
                self.assertEqual(client.get("/hq/migration/preview").status_code, 302)
                with client.session_transaction() as sess, auth_db() as db:
                    sess.update(hq_sid=issue_session(db, "synthetic-subject"),
                                csrf="test-csrf", google_sub="synthetic-subject")
                response = client.get("/hq/migration/preview")
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Run fresh read-only comparison", response.data)
                self.assertIn("no-store", response.headers.get("Cache-Control", ""))
                read.assert_not_called()
                self.assertEqual(client.post("/hq/migration/preview",
                                              data={"csrf": "bad"}).status_code, 403)
                read.assert_not_called()
                formula, actual = snapshots()
                with patch.dict(app.extensions, {"hq_sheets_connection": lambda: ("fixture", 9999999999)}), \
                     patch("hq_historical_preview._native_inventory", return_value=None):
                    read.return_value = (formula, actual)
                    ok = client.post("/hq/migration/preview",
                                     data={"csrf": "test-csrf"})
                self.assertEqual(ok.status_code, 200)
                self.assertIn(b"Unique, correctly formatted J references", ok.data)
                read.assert_called_once()
                self.assertNotIn(b"Synthetic A", ok.data)
                self.assertNotIn(b"FAKE", ok.data)



class HistoricalMappingProposalTests(unittest.TestCase):
    """No production data or Sheets calls. Only synthetic canonical fixtures."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.tmp.name, "HQ_SESSION_SECRET": "fixture",
            "GOOGLE_CLIENT_ID": "fixture", "GOOGLE_CLIENT_SECRET": "fixture",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        self.env.start()
        self.customer_id = "a" * 32
        self.vehicle_id = "b" * 32
        self.work_id = "c" * 32
        with connect() as db:
            _schema(db)
            db.execute("""INSERT INTO hq_customers
                (id,source_request_id,name,email,phone,created_at)
                VALUES (?,?,?,?,?,?)""",
                (self.customer_id,None,"Fixture Customer","unit@example.invalid",
                 "07000000000","2026-01-01"))
            db.execute("""INSERT INTO hq_vehicles
                (id,customer_id,make,model,model_year,registration,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (self.vehicle_id,self.customer_id,"Fixture","Car","2020","TEST",
                 "2026-01-01"))
            db.execute("""INSERT INTO hq_work_records
                (id,source_request_id,customer_id,vehicle_id,kind,service,notes,
                preferred_date,preferred_time,status,revision,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.work_id,"HQ-"+self.work_id,self.customer_id,self.vehicle_id,
                 "job","Window Tinting","","","","approved",0,"2026-01-01"))
        self.formula,self.actual = snapshots()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _stage(self, *, operation_id=None, source_stamp=None, sheet_row=5, job_ref="J0081",
               work_token=None, formula=None, computed=None):
        with app.app_context():
            stamp = source_stamp or _candidate_stamp(self.formula,self.actual,5)
            return stage_j_candidate(
                formula=self.formula if formula is None else formula,
                computed=self.actual if computed is None else computed,
                sheet_row=sheet_row, job_ref=job_ref, source_stamp=stamp,
                work_token=work_token or self.work_id+":0",
                operation_id=operation_id or "d"*32, owner_identity="owner-fixture")

    def test_persistent_proposal_is_inactive_idempotent_and_audited(self):
        candidate = self._stage()
        self.assertEqual(self._stage(), candidate)
        self.assertEqual(len(_current_candidates()), 1)
        with connect() as db:
            work = db.execute("SELECT status,revision,price_pence FROM hq_work_records WHERE id=?",
                              (self.work_id,)).fetchone()
            self.assertEqual(tuple(work), ("approved",0,None))
            self.assertEqual(db.execute("""SELECT COUNT(*) FROM hq_historical_j_candidate_events
                WHERE candidate_id=?""",(candidate,)).fetchone()[0], 1)
            proposal = db.execute("""SELECT * FROM hq_historical_j_candidates
                WHERE id=?""",(candidate,)).fetchone()
            self.assertEqual((proposal["sheet_row"],proposal["job_ref"],proposal["work_id"]),
                             (5,"J0081",self.work_id))
            self.assertEqual(len(proposal["source_stamp"]),64)
            self.assertNotIn("Synthetic A",str(dict(proposal)))
            self.assertNotIn("price_pence",proposal.keys())
            self.assertNotIn("customer_name",proposal.keys())

    def test_review_collision_stale_revision_and_source_mutations_fail_closed(self):
        self._stage()
        with self.assertRaisesRegex(ValueError,"collision"):
            self._stage(operation_id="e"*32)
        with self.assertRaisesRegex(ValueError,"another candidate"):
            self._stage(work_token=self.work_id+":1",operation_id="d"*32)
        with self.assertRaisesRegex(ValueError,"changed or is unavailable"):
            self._stage(operation_id="f"*32,work_token=self.work_id+":1")
        changed,_ = snapshots()
        changed["valueRanges"][0]["values"][1][5] = "Different service"
        with self.assertRaisesRegex(ValueError,"Source row changed"):
            self._stage(operation_id="f"*32, formula=changed)
        with self.assertRaisesRegex(ValueError,"duplicated"):
            self._stage(sheet_row=6,job_ref="J0082", operation_id="f"*32)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_historical_j_candidates").fetchone()[0],1)

    def test_withdraw_keeps_evidence_and_allows_new_staging(self):
        candidate = self._stage()
        with app.app_context():
            withdraw_j_candidate(candidate,0,"owner-fixture")
            withdraw_j_candidate(candidate,0,"owner-fixture")
        self.assertEqual(_current_candidates(),[])
        with connect() as db:
            actions = [r[0] for r in db.execute("""SELECT action FROM
                hq_historical_j_candidate_events WHERE candidate_id=?
                ORDER BY revision""",(candidate,))]
        self.assertEqual(actions,["proposed","withdrawn"])
        replacement = self._stage(operation_id="e"*32)
        self.assertNotEqual(candidate,replacement)
        self.assertEqual(len(_current_candidates()),1)

    def test_owner_route_rechecks_provider_no_public_write_or_unauthed_access(self):
        client = app.test_client()
        self.assertEqual(client.get("/hq/migration/preview").status_code,302)
        with client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db,"owner-fixture"),csrf="fixed-csrf",
                        google_sub="owner-fixture")
        with app.app_context():
            stamp = _candidate_stamp(self.formula,self.actual,5)
        data = {"action":"propose","csrf":"fixed-csrf","sheet_row":"5",
                "job_ref":"J0081","source_stamp":stamp,"work_token":self.work_id+":0",
                "operation_id":"f"*32}
        with (patch.dict(app.extensions,{"hq_sheets_connection":lambda:("fixture",9999999999)}),
              patch("hq_historical_preview._read_live",
                    return_value=(self.formula,self.actual)) as read):
            self.assertEqual(client.post("/hq/migration/preview",
                                         data={**data,"csrf":"wrong"}).status_code,403)
            ok = client.post("/hq/migration/preview",data=data)
            self.assertEqual(ok.status_code,303)
            read.assert_called_once()
        page = client.get("/hq/migration/preview")
        self.assertIn(b"J0081",page.data)
        self.assertIn(b"Withdraw candidate",page.data)
        self.assertNotIn(b"Synthetic A",page.data)
        self.assertNotIn(b"250",page.data)
        with patch("hq_historical_preview._read_live") as read:
            result = client.post("/hq/migration/preview",data={
                "action":"withdraw","csrf":"fixed-csrf",
                "candidate_id":_current_candidates()[0]["id"],"revision":"0"})
            self.assertEqual(result.status_code,303)
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
