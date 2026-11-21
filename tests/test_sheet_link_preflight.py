"""Synthetic-only Sheets GridData linkage preflight regression tests."""
import unittest
from unittest.mock import Mock, patch
from hq_job_sync import WORKBOOK
from hq_sheet_link_preflight import JOBS_SHEET_ID, VERIFIED_HEADERS, preflight_job_link


def fixture():
    def cell(value):
        return {"userEnteredValue": {"stringValue": value}}
    heading = {"values": [{} for _ in range(36)]}
    # Synthetic placeholders only: B:E values remain subject to a real
    # owner-authorised source mapping review, not an assumed exact schema.
    for index, value in {1: "Date", 2: "Customer",
                         3: "Customer Type", 4: "Vehicle/Reg"}.items():
        heading["values"][index] = cell(value)
    for index, value in VERIFIED_HEADERS.items():
        heading["values"][index] = cell(value)
    return {
        "spreadsheetId": WORKBOOK,
        "sheets": [{
            "properties": {
                "title": "Jobs", "sheetId": JOBS_SHEET_ID,
                "gridProperties": {"rowCount": 7, "columnCount": 94}
            },
            "data": [{
                "startRow": 3, "startColumn": 0,
                "rowData": [
                    heading, {"values": [cell("J0077")]},
                    {"values": []}, {"values": [cell("J0082")]}
                ],
            }],
        }],
    }


class SheetLinkPreflightTests(unittest.TestCase):
    def check(self, snap=None, *, job_ref="J0083", row=6, mode="new"):
        return preflight_job_link(
            snap if snap is not None else fixture(),
            job_ref=job_ref, target_row=row, mode=mode,
        )

    def test_blank_new_candidate_preserves_every_approval_gate(self):
        result = self.check()
        self.assertTrue(result["complete_reference_scan"])
        for stage in ("historical_mapping_verified", "baseline_verified",
                      "owner_write_approval", "write_authorised",
                      "live_sync", "reconciled"):
            self.assertFalse(result[stage])

    def test_existing_ref_is_not_a_verified_hq_link(self):
        result = self.check(job_ref="J0077", row=5, mode="existing")
        self.assertEqual(result["candidate_kind"], "existing")
        self.assertFalse(result["historical_mapping_verified"])
        with self.assertRaises(ValueError):
            self.check(job_ref="J0077", row=6, mode="existing")

    def test_duplicate_id_anywhere_blocks_preflight(self):
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"][2]["values"] = [
            {"userEnteredValue": {"stringValue": "j0077"}}]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.check(snap)

    def test_new_job_ref_cannot_reuse_differently_cased_historical_id(self):
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"][3]["values"][0] = {
            "userEnteredValue": {"stringValue": "j0083"}}
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.check(snap, job_ref="J0083", row=6, mode="new")

    def test_existing_link_must_match_exact_historical_id_case(self):
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"][1]["values"][0] = {
            "userEnteredValue": {"stringValue": "j0077"}}
        with self.assertRaisesRegex(ValueError, "casing"):
            self.check(snap, job_ref="J0077", row=5, mode="existing")
        # Exact owner-reviewed original case (uppercase) still succeeds.
        self.assertEqual(
            self.check(fixture(), job_ref="J0077", row=5, mode="existing")[
                "candidate_kind"], "existing")

    def test_truncated_scan_never_proves_uniqueness(self):
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"].pop()
        with self.assertRaisesRegex(ValueError, "Truncated"):
            self.check(snap)

    def test_edited_or_duplicated_header_is_rejected(self):
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"][0]["values"][16] = {
            "userEnteredValue": {"stringValue": "Sale price"}}
        with self.assertRaisesRegex(ValueError, "schema"):
            self.check(snap)
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"][0]["values"][15] = {
            "userEnteredValue": {"stringValue": "Job ID"}}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.check(snap)

    def test_blank_or_missing_unverified_input_heading_blocks(self):
        # Presence of B:E headings is a minimum safety check; their exact
        # semantic mapping to the owner workbook is still unverified.
        for column in (1, 2, 3, 4):
            with self.subTest(column=column):
                snap = fixture()
                snap["sheets"][0]["data"][0]["rowData"][0]["values"][column] = {}
                with self.assertRaisesRegex(ValueError, "input heading missing"):
                    self.check(snap)
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"][0]["values"][3] = {
            "userEnteredValue": {"stringValue": "  "}}
        with self.assertRaisesRegex(ValueError, "input heading missing"):
            self.check(snap)

    def test_unverified_headings_remain_literal_and_unique(self):
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"][0]["values"][2] = {
            "userEnteredValue": {"formulaValue": '="Customer"'}}
        with self.assertRaisesRegex(ValueError, "literal text"):
            self.check(snap)
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"][0]["values"][4] = {
            "userEnteredValue": {"stringValue": "customer"}}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.check(snap)

    def test_mapped_input_formula_is_not_safe_to_overwrite(self):
        snap = fixture()
        target = snap["sheets"][0]["data"][0]["rowData"][2]
        target["values"] = [{} for _ in range(36)]
        target["values"][16] = {"userEnteredValue": {"formulaValue": "=1+2"}}
        with self.assertRaisesRegex(ValueError, "formula"):
            self.check(snap)

    def test_unmapped_derived_formula_can_be_preserved(self):
        snap = fixture()
        target = snap["sheets"][0]["data"][0]["rowData"][2]
        target["values"] = [{} for _ in range(20)]
        target["values"][18] = {"userEnteredValue": {"formulaValue": "=TODAY()"}}
        self.assertEqual(self.check(snap)["preflight"], "sheet_snapshot_only")

    def test_occupied_input_and_reused_id_block(self):
        snap = fixture()
        target = snap["sheets"][0]["data"][0]["rowData"][2]
        target["values"] = [{} for _ in range(18)]
        target["values"][2] = {"userEnteredValue": {"stringValue": "Private Customer"}}
        with self.assertRaisesRegex(ValueError, "already contains"):
            self.check(snap)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.check(fixture(), job_ref="J0077")

    def test_formatted_only_cells_are_not_proof_of_blank_input(self):
        snap = fixture()
        target = snap["sheets"][0]["data"][0]["rowData"][2]
        target["values"] = [{} for _ in range(17)]
        target["values"][16] = {"formattedValue": "£100.00"}
        with self.assertRaisesRegex(ValueError, "entered-value"):
            self.check(snap)

    def test_formula_driven_job_ids_cannot_establish_uniqueness(self):
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"][1]["values"][0] = {
            "userEnteredValue": {"formulaValue": '=TEXT(77,"J0000")'}}
        with self.assertRaisesRegex(ValueError, "literal text"):
            self.check(snap)

    def test_invalid_source_and_candidate_rows_block(self):
        snap = fixture()
        snap["spreadsheetId"] = "different"
        with self.assertRaises(ValueError):
            self.check(snap)
        snap = fixture()
        snap["sheets"][0]["properties"]["sheetId"] = 123
        with self.assertRaises(ValueError):
            self.check(snap)
        for candidate in (True, 4, 8):
            with self.assertRaises(ValueError):
                self.check(row=candidate)

    def test_trailing_blank_rows_require_independent_formula_range(self):
        from hq_sheets_live import read_job_link_snapshot
        snap = fixture()
        # In this mock, row 7 is genuinely empty but Google's GridData
        # response stops at row 6 (the candidate is still raw-inspected).
        snap["sheets"][0]["data"][0]["rowData"].pop()
        values = {
            "range": "Jobs!A4:AJ7", "majorDimension": "ROWS",
            "values": [["Job ID"], ["J0077"]],
        }
        first = Mock()
        first.json.return_value = snap
        second = Mock()
        second.json.return_value = values
        with patch("hq_sheets_live.requests.get", side_effect=[first, second]) as get:
            completed = read_job_link_snapshot("synthetic-token", selected_row=6)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(
            dict(get.call_args.kwargs["params"])["valueRenderOption"], "FORMULA")
        self.assertEqual(
            len(completed["sheets"][0]["data"][0]["rowData"]), 4)
        self.assertEqual(
            self.check(completed)["preflight"], "sheet_snapshot_only")
        # Do not modify the original Google response in memory.
        self.assertEqual(len(snap["sheets"][0]["data"][0]["rowData"]), 3)

    def test_missing_raw_candidate_row_stays_blocked_without_second_read(self):
        from hq_sheets_live import read_job_link_snapshot
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"].pop()
        response = Mock()
        response.json.return_value = snap
        with patch("hq_sheets_live.requests.get", return_value=response) as get:
            original = read_job_link_snapshot("synthetic-token", selected_row=7)
        self.assertEqual(get.call_count, 1)
        with self.assertRaisesRegex(ValueError, "Truncated"):
            self.check(original, row=7)

    def test_trailing_collision_or_changed_reference_is_not_blank_evidence(self):
        from hq_sheet_link_preflight import complete_trailing_blank_rows
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"].pop()
        cases = (
            {"range": "Jobs!A4:AJ7", "majorDimension": "ROWS",
             "values": [["Job ID"], ["J0077"], [], ["J0083"]]},
            {"range": "Jobs!A4:AJ7", "majorDimension": "ROWS",
             "values": [["Job ID"], ["J0099"]]},
            {"range": "Jobs!A4:AJ6", "majorDimension": "ROWS",
             "values": [["Job ID"], ["J0077"]]},
            {"range": "Jobs!A4:AJ7", "majorDimension": "COLUMNS",
             "values": [["Job ID"], ["J0077"]]},
            {"range": "Jobs!A4:AJ7", "majorDimension": "ROWS",
             "values": [["Job ID"], ["J0077"], [], ["=NOW()"]]},
        )
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                complete_trailing_blank_rows(snap, bad, candidate_row=6)

    def test_secondary_read_rejects_new_target_input_without_new_job_id(self):
        from hq_sheet_link_preflight import complete_trailing_blank_rows
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"].pop()
        # Row 6 is blank in the raw read, but changed during the second
        # read. The ref in column A stays empty, so ID-only checks miss it.
        changed_rows = (
            ["", "", "New customer"],
            ["", "=NOW()"],
            ["", 0],
        )
        for candidate_values in changed_rows:
            with self.subTest(candidate_values=candidate_values):
                second = {
                    "range": "Jobs!A4:AJ7", "majorDimension": "ROWS",
                    "values": [["Job ID"], ["J0077"], candidate_values],
                }
                with self.assertRaisesRegex(ValueError, "Candidate input changed"):
                    complete_trailing_blank_rows(snap, second, candidate_row=6)

    def test_secondary_read_rejects_new_input_in_existing_link_candidate(self):
        from hq_sheet_link_preflight import complete_trailing_blank_rows
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"].pop()
        # The existing J0077 reference matches, but its previously empty
        # customer field has changed between the two provider reads.
        second = {
            "range": "Jobs!A4:AJ7", "majorDimension": "ROWS",
            "values": [["Job ID"], ["J0077", "", "Private customer"]],
        }
        with self.assertRaisesRegex(ValueError, "Candidate input changed"):
            complete_trailing_blank_rows(snap, second, candidate_row=5)

    def test_trailing_value_request_failure_is_not_silently_accepted(self):
        from hq_sheets_live import read_job_link_snapshot
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"].pop()
        response = Mock()
        response.json.return_value = snap
        with patch("hq_sheets_live.requests.get",
                   side_effect=[response, RuntimeError("synthetic provider error")]):
            with self.assertRaises(RuntimeError):
                read_job_link_snapshot("synthetic-token", selected_row=6)

    def test_no_personal_values_or_payment_evidence_are_returned(self):
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"][1]["values"].append({
            "userEnteredValue": {"stringValue": "PERSONAL"}})
        result = self.check(snap)
        self.assertNotIn("PERSONAL", str(result))
        self.assertNotIn("J0077", str(result))

    def test_secondary_read_checks_changed_or_deleted_existing_literal(self):
        from hq_sheet_link_preflight import complete_trailing_blank_rows
        snap = fixture()
        rows = snap["sheets"][0]["data"][0]["rowData"]
        rows.pop()  # Missing trailing blank row makes a second read necessary.
        original = rows[1]["values"]
        original.extend({} for _ in range(3 - len(original)))
        original[2] = {"userEnteredValue": {"stringValue": "Original customer"}}
        for candidate in ("Changed customer", ""):
            with self.subTest(candidate=candidate):
                second = {
                    "range": "Jobs!A4:AJ7", "majorDimension": "ROWS",
                    "values": [["Job ID"], ["J0077", "", candidate]],
                }
                with self.assertRaisesRegex(ValueError, "Candidate input changed"):
                    complete_trailing_blank_rows(snap, second, candidate_row=5)

    def test_secondary_read_preserves_unchanged_literal_and_numeric_format(self):
        from hq_sheet_link_preflight import complete_trailing_blank_rows
        snap = fixture()
        rows = snap["sheets"][0]["data"][0]["rowData"]
        rows.pop()
        cells = rows[1]["values"]
        cells.extend({} for _ in range(17 - len(cells)))
        cells[2] = {"userEnteredValue": {"stringValue": "Original customer"}}
        cells[16] = {"userEnteredValue": {"numberValue": 100}}
        # FORMULA-rendered values may format currency numbers differently.
        # Exact literal customer names, unlike numbers, must still agree.
        observed = ["J0077", "", "Original customer"] + [""] * 13 + ["£100.00"]
        second = {
            "range": "Jobs!A4:AJ7", "majorDimension": "ROWS",
            "values": [["Job ID"], observed],
        }
        completed = complete_trailing_blank_rows(snap, second, candidate_row=5)
        self.assertEqual(
            self.check(completed, job_ref="J0077", row=5, mode="existing")[
                "preflight"], "sheet_snapshot_only")

    def test_complete_grid_also_requires_second_formula_read(self):
        from hq_sheets_live import read_job_link_snapshot
        snap = fixture()  # All Jobs rows were returned by the first read.
        formula = {
            "range": "Jobs!A4:AJ7", "majorDimension": "ROWS",
            "values": [["Job ID"], ["J0077"], [], ["J0082"]],
        }
        raw_response, formula_response = Mock(), Mock()
        raw_response.json.return_value = snap
        formula_response.json.return_value = formula
        with patch("hq_sheets_live.requests.get",
                   side_effect=[raw_response, formula_response]) as get:
            inspected = read_job_link_snapshot("synthetic-token", selected_row=6)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(
            dict(get.call_args.kwargs["params"])["valueRenderOption"], "FORMULA")
        self.assertEqual(self.check(inspected)["preflight"], "sheet_snapshot_only")
        self.assertFalse(self.check(inspected)["baseline_verified"])

    def test_complete_grid_rejects_changes_without_missing_tail(self):
        from hq_sheets_live import read_job_link_snapshot
        for changed_row in (
            ["", "", "Changed customer"],
            ["", '=IF(FALSE,"test","")'],
            ["J0084"],
        ):
            with self.subTest(changed_row=changed_row):
                raw, formula = Mock(), Mock()
                raw.json.return_value = fixture()
                formula.json.return_value = {
                    "range": "Jobs!A4:AJ7", "majorDimension": "ROWS",
                    "values": [["Job ID"], ["J0077"], changed_row, ["J0082"]],
                }
                with patch("hq_sheets_live.requests.get",
                           side_effect=[raw, formula]) as get:
                    with self.assertRaises(ValueError):
                        read_job_link_snapshot("synthetic-token", selected_row=6)
                self.assertEqual(get.call_count, 2)

    def test_complete_grid_second_read_failure_blocks_preflight(self):
        from hq_sheets_live import read_job_link_snapshot
        raw = Mock()
        raw.json.return_value = fixture()
        with patch("hq_sheets_live.requests.get",
                   side_effect=[raw, RuntimeError("fixture provider failure")]) as get:
            with self.assertRaisesRegex(RuntimeError, "fixture provider failure"):
                read_job_link_snapshot("synthetic-token", selected_row=6)
        self.assertEqual(get.call_count, 2)
