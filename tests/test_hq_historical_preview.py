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
    _candidate_stamp, stage_j_candidate, withdraw_j_candidate, _current_candidates,
    _candidate_for_review, _field_comparison, save_candidate_field_review,
    save_candidate_field_ownership)

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

    def test_full_range_paginated_without_exposing_customer_data(self):
        formula, actual = snapshots()
        for i in range(90):
            row = [f"J{i + 1000:04d}", "2026-03-01", f"Private Person {i}",
                   "Retail", "PRIVATE", "Tinting"] + [""] * 10 + [150, "Quoted"]
            formula["valueRanges"][0]["values"].append(row[:])
            actual["valueRanges"][0]["values"].append(row[:])
        first = build_preview(formula, actual)
        second = build_preview(formula, actual, page_index=1)
        self.assertEqual(first["jobs_rows"], 94)
        self.assertEqual(first["page_count"], 2)
        self.assertEqual(len(first["sample"]), 60)
        self.assertEqual(len(second["sample"]), 34)
        self.assertEqual(second["sample"][0]["row"], 65)
        self.assertNotIn("Private Person", str(second))
        self.assertNotIn("PRIVATE", str(second))
        self.assertEqual(second["jobs_rows"], first["jobs_rows"])
        exact = build_preview(formula, actual, find_ref="J1089")
        self.assertEqual(exact["filtered_count"], 1)
        self.assertEqual(exact["sample"][0]["ref"], "J1089")
        self.assertEqual(build_preview(formula, actual, find_ref="J9999")["sample"], [])
        for kwargs in ({"page_index": -1}, {"page_index": 2},
                       {"find_ref": "j1000"}, {"find_ref": "J1000' OR 1=1"}):
            with self.assertRaises(ValueError):
                build_preview(formula, actual, **kwargs)

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

    def test_page_navigation_and_reference_lookup_are_explicit_and_read_only(self):
        formula, actual = snapshots()
        for i in range(80):
            row = [f"J{i + 1000:04d}", "2026-03-01", f"Private Person {i}",
                   "Retail", "PRIVATE", "Tinting"] + [""] * 10 + [150, "Quoted"]
            formula["valueRanges"][0]["values"].append(row[:])
            actual["valueRanges"][0]["values"].append(row[:])
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, patch.dict(
            os.environ, {"HQ_DATA_DIR": directory, "HQ_SESSION_SECRET": "fixture",
                         "GOOGLE_CLIENT_ID": "fixture",
                         "GOOGLE_CLIENT_SECRET": "fixture",
                         "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid"}):
            client = app.test_client()
            with client.session_transaction() as sess, auth_db() as db:
                sess.update(hq_sid=issue_session(db, "synthetic-subject"),
                            csrf="page-csrf", google_sub="synthetic-subject")
            with (patch.dict(app.extensions, {
                    "hq_sheets_connection": lambda: ("fixture", 9999999999)}),
                  patch("hq_historical_preview._read_live", return_value=(formula, actual)) as read):
                response = client.post("/hq/migration/preview", data={
                    "csrf": "page-csrf", "action": "compare", "page_index": "1"})
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"page 2 of 2", response.data)
                self.assertIn(b"J1079", response.data)
                self.assertNotIn(b"Private Person", response.data)
                self.assertIn(b"Previous 60 rows", response.data)
                exact = client.post("/hq/migration/preview", data={
                    "csrf": "page-csrf", "action": "compare", "find_ref": "J1079"})
                self.assertIn(b"Showing 1 of 1", exact.data)
                bad = client.post("/hq/migration/preview", data={
                    "csrf": "page-csrf", "action": "compare", "page_index": "99"})
                self.assertEqual(bad.status_code, 400)
                self.assertEqual(read.call_count, 2)

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


    def test_field_comparison_is_owner_only_and_read_only(self):
        cid = self._stage()
        with app.app_context():
            staged = _candidate_for_review(cid)
            fields = _field_comparison(self.formula, self.actual, staged)
        self.assertEqual([f["key"] for f in fields],
                         ["customer", "customer_tier", "vehicle", "services",
                          "quoted_price", "lifecycle"])
        self.assertEqual(fields[0]["source"], "Synthetic A")
        self.assertEqual(fields[0]["native"], "Fixture Customer")
        self.assertIn("250", fields[4]["source"])
        self.assertIn("unrecorded", fields[4]["native"])
        self.assertNotIn("field_review", staged)
        with connect() as db:
            self.assertEqual(db.execute("""SELECT COUNT(*) FROM hq_historical_j_candidates
                WHERE withdrawn_at IS NULL""").fetchone()[0], 1)
            self.assertIsNone(db.execute("""SELECT 1 FROM sqlite_master WHERE
                name='hq_historical_candidate_field_reviews'""").fetchone())

    def test_field_assessments_persist_audit_and_replay_safely(self):
        cid = self._stage()
        fields = {"customer": "conflict", "customer_tier": "needs_evidence",
                  "vehicle": "appears_consistent", "services": "conflict",
                  "quoted_price": "needs_evidence", "lifecycle": "needs_evidence"}
        with app.app_context():
            rev = save_candidate_field_review(
                candidate_id=cid, expected_revision=0, assessments=fields,
                formula=self.formula, computed=self.actual, owner_identity="fixture")
            self.assertEqual(rev, 1)
            self.assertEqual(save_candidate_field_review(
                candidate_id=cid, expected_revision=0, assessments=fields,
                formula=self.formula, computed=self.actual, owner_identity="fixture"), 1)
        stored = _candidate_for_review(cid)["field_review"]
        self.assertEqual(stored["assessments"], fields)
        self.assertEqual(stored["revision"], 1)
        with connect() as db:
            self.assertEqual(db.execute("""SELECT COUNT(*) FROM
                hq_historical_candidate_field_events WHERE candidate_id=?""",
                (cid,)).fetchone()[0], 1)
            self.assertEqual(tuple(db.execute("""SELECT status,revision,price_pence
                FROM hq_work_records WHERE id=?""", (self.work_id,)).fetchone()),
                ("approved", 0, None))
            row = db.execute("""SELECT * FROM hq_historical_candidate_field_reviews
                WHERE candidate_id=?""", (cid,)).fetchone()
            self.assertNotIn("Synthetic A", str(dict(row)))
            self.assertNotIn("Fixture Customer", str(dict(row)))

    def test_field_assessment_validation_concurrency_and_withdrawal(self):
        cid = self._stage()
        assessments = {k: "needs_evidence" for k in
                       ("customer", "customer_tier", "vehicle", "services",
                        "quoted_price", "lifecycle")}
        with app.app_context():
            with self.assertRaisesRegex(ValueError, "Review at least one"):
                save_candidate_field_review(
                    candidate_id=cid, expected_revision=0, assessments=assessments,
                    formula=self.formula, computed=self.actual, owner_identity="fixture")
            assessments["customer"] = "conflict"
            self.assertEqual(save_candidate_field_review(
                candidate_id=cid, expected_revision=0, assessments=assessments,
                formula=self.formula, computed=self.actual, owner_identity="fixture"), 1)
            changed = dict(assessments, customer="appears_consistent")
            with self.assertRaisesRegex(ValueError, "changed; reload"):
                save_candidate_field_review(
                    candidate_id=cid, expected_revision=0, assessments=changed,
                    formula=self.formula, computed=self.actual, owner_identity="fixture")
            altered, _ = snapshots()
            altered["valueRanges"][0]["values"][1][5] = "Altered scope"
            with self.assertRaisesRegex(ValueError, "Source row changed"):
                save_candidate_field_review(
                    candidate_id=cid, expected_revision=1, assessments=changed,
                    formula=altered, computed=self.actual, owner_identity="fixture")
            with connect() as db:
                db.execute("UPDATE hq_work_records SET revision=revision+1 WHERE id=?",
                           (self.work_id,))
            with self.assertRaisesRegex(ValueError, "Native work changed"):
                save_candidate_field_review(
                    candidate_id=cid, expected_revision=1, assessments=changed,
                    formula=self.formula, computed=self.actual, owner_identity="fixture")
            withdraw_j_candidate(cid, 0, "fixture")
            with self.assertRaisesRegex(ValueError, "withdrawn"):
                save_candidate_field_review(
                    candidate_id=cid, expected_revision=1, assessments=changed,
                    formula=self.formula, computed=self.actual, owner_identity="fixture")

    def test_owner_field_review_route_get_never_fetches_source_and_save_is_explicit(self):
        cid = self._stage()
        endpoint = "/hq/migration/candidates/" + cid + "/review"
        client = app.test_client()
        self.assertEqual(client.get(endpoint).status_code, 302)
        with client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "owner-fixture"),
                        csrf="field-csrf", google_sub="owner-fixture")
        with patch("hq_historical_preview._read_live") as read:
            page = client.get(endpoint)
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"Compare current Sheets and HQ values", page.data)
            self.assertNotIn(b"Synthetic A", page.data)
            self.assertIn("no-store", page.headers.get("Cache-Control", ""))
            read.assert_not_called()
            self.assertEqual(client.post(endpoint, data={"action": "inspect",
                "csrf": "wrong"}).status_code, 403)
            read.assert_not_called()
        with (patch.dict(app.extensions,
                         {"hq_sheets_connection": lambda: ("fixture", 9999999999)}),
              patch("hq_historical_preview._read_live",
                    return_value=(self.formula, self.actual)) as read):
            inspected = client.post(endpoint, data={"action": "inspect",
                                                   "csrf": "field-csrf"})
            self.assertEqual(inspected.status_code, 200)
            self.assertIn(b"Synthetic A", inspected.data)
            self.assertIn(b"Fixture Customer", inspected.data)
            data = {"action": "save", "csrf": "field-csrf", "revision": "0",
                    "customer": "conflict", "customer_tier": "needs_evidence",
                    "vehicle": "needs_evidence", "services": "needs_evidence",
                    "quoted_price": "needs_evidence", "lifecycle": "needs_evidence"}
            saved = client.post(endpoint, data=data)
            self.assertEqual(saved.status_code, 303)
            self.assertEqual(read.call_count, 2)
        refreshed = client.get(endpoint)
        self.assertIn(b"revision 1", refreshed.data)
        self.assertIn(b"conflict", refreshed.data)
        self.assertNotIn(b"Synthetic A", refreshed.data)



    def test_field_ownership_plan_is_audited_idempotent_and_inactive(self):
        cid = self._stage()
        assessments = {"customer": "conflict", "customer_tier": "appears_consistent",
                       "vehicle": "appears_consistent", "services": "needs_evidence",
                       "quoted_price": "appears_consistent", "lifecycle": "needs_evidence"}
        selections = {"customer": "manual_resolution", "customer_tier": "review_hq",
                      "vehicle": "review_sheets", "services": "hold",
                      "quoted_price": "review_sheets", "lifecycle": "hold"}
        with app.app_context():
            save_candidate_field_review(
                candidate_id=cid, expected_revision=0, assessments=assessments,
                formula=self.formula, computed=self.actual, owner_identity="fixture")
            self.assertEqual(save_candidate_field_ownership(
                candidate_id=cid, expected_revision=0, selections=selections,
                formula=self.formula, computed=self.actual, owner_identity="fixture"), 1)
            self.assertEqual(save_candidate_field_ownership(
                candidate_id=cid, expected_revision=0, selections=selections,
                formula=self.formula, computed=self.actual, owner_identity="fixture"), 1)
        stored = _candidate_for_review(cid)["field_ownership"]
        self.assertEqual(stored["selections"], selections)
        self.assertEqual(stored["revision"], 1)
        self.assertEqual(stored["assessment_revision"], 1)
        with connect() as db:
            self.assertEqual(db.execute("""SELECT COUNT(*) FROM
                hq_historical_field_ownership_events WHERE candidate_id=?""",
                (cid,)).fetchone()[0], 1)
            self.assertEqual(tuple(db.execute("""SELECT status,revision,price_pence
                FROM hq_work_records WHERE id=?""", (self.work_id,)).fetchone()),
                ("approved", 0, None))
            self.assertNotIn("Fixture Customer", str(dict(db.execute(
                "SELECT * FROM hq_historical_field_ownership WHERE candidate_id=?",
                (cid,)).fetchone())))

    def test_field_ownership_requires_evidence_and_rejects_conflicts(self):
        cid = self._stage()
        base = {key: "hold" for key in ("customer","customer_tier","vehicle",
                                        "services","quoted_price","lifecycle")}
        with app.app_context():
            with self.assertRaisesRegex(ValueError, "assessment first"):
                save_candidate_field_ownership(
                    candidate_id=cid, expected_revision=0,
                    selections=dict(base, customer="review_hq"),
                    formula=self.formula, computed=self.actual, owner_identity="fixture")
            assessment = {key: "needs_evidence" for key in base}
            assessment["customer"] = "conflict"
            assessment["customer_tier"] = "appears_consistent"
            save_candidate_field_review(
                candidate_id=cid, expected_revision=0, assessments=assessment,
                formula=self.formula, computed=self.actual, owner_identity="fixture")
            with self.assertRaisesRegex(ValueError, "consistent field"):
                save_candidate_field_ownership(
                    candidate_id=cid, expected_revision=0,
                    selections=dict(base, customer="review_sheets"),
                    formula=self.formula, computed=self.actual, owner_identity="fixture")
            with self.assertRaisesRegex(ValueError, "Assess the field"):
                save_candidate_field_ownership(
                    candidate_id=cid, expected_revision=0,
                    selections=dict(base, services="manual_resolution"),
                    formula=self.formula, computed=self.actual, owner_identity="fixture")
            plan = dict(base, customer="manual_resolution")
            self.assertEqual(save_candidate_field_ownership(
                candidate_id=cid, expected_revision=0, selections=plan,
                formula=self.formula, computed=self.actual, owner_identity="fixture"), 1)
            with self.assertRaisesRegex(ValueError, "changed; reload"):
                save_candidate_field_ownership(
                    candidate_id=cid, expected_revision=0,
                    selections=dict(plan, customer_tier="review_hq"),
                    formula=self.formula, computed=self.actual, owner_identity="fixture")
            altered, actual = snapshots()
            altered["valueRanges"][0]["values"][1][5] = "Changed"
            with self.assertRaisesRegex(ValueError, "Source row changed"):
                save_candidate_field_ownership(
                    candidate_id=cid, expected_revision=1, selections=plan,
                    formula=altered, computed=actual, owner_identity="fixture")
            withdraw_j_candidate(cid, 0, "fixture")
            with self.assertRaisesRegex(ValueError, "withdrawn"):
                save_candidate_field_ownership(
                    candidate_id=cid, expected_revision=1, selections=plan,
                    formula=self.formula, computed=self.actual, owner_identity="fixture")

    def test_field_ownership_ui_is_owner_only_and_saves_on_explicit_post(self):
        cid = self._stage()
        with app.app_context():
            assess = {key:"appears_consistent" for key in
                      ("customer","customer_tier","vehicle","services",
                       "quoted_price","lifecycle")}
            save_candidate_field_review(
                candidate_id=cid, expected_revision=0, assessments=assess,
                formula=self.formula, computed=self.actual, owner_identity="fixture")
        endpoint = "/hq/migration/candidates/" + cid + "/review"
        client = app.test_client()
        self.assertEqual(client.get(endpoint).status_code, 302)
        with client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db,"owner-fixture"),
                        csrf="ownership-csrf",google_sub="owner-fixture")
        with patch("hq_historical_preview._read_live") as read:
            before = client.get(endpoint)
            self.assertNotIn(b"Synthetic A", before.data)
            self.assertEqual(client.post(endpoint, data={
                "csrf":"bad","action":"save_ownership"}).status_code,403)
            read.assert_not_called()
        with (patch.dict(app.extensions, {
                "hq_sheets_connection": lambda: ("fixture",9999999999)}),
              patch("hq_historical_preview._read_live",
                    return_value=(self.formula,self.actual)) as read):
            data = {"csrf":"ownership-csrf","action":"save_ownership",
                    "ownership_revision":"0"}
            for key in assess:
                data["ownership_"+key] = "hold"
            data["ownership_customer"] = "review_sheets"
            self.assertEqual(client.post(endpoint,data=data).status_code,303)
            self.assertEqual(read.call_count,1)
        after = client.get(endpoint)
        self.assertIn(b"Saved field handling plan",after.data)
        self.assertIn(b"review sheets",after.data)
        self.assertNotIn(b"Synthetic A",after.data)


if __name__ == "__main__":
    unittest.main()
