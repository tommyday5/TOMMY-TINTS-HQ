"""Synthetic-only Sheets GridData linkage preflight regression tests."""
import unittest
from hq_job_sync import WORKBOOK
from hq_sheet_link_preflight import JOBS_SHEET_ID, VERIFIED_HEADERS, preflight_job_link


def fixture():
    def cell(value):
        return {"userEnteredValue": {"stringValue": value}}
    heading = {"values": [{} for _ in range(36)]}
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

    def test_no_personal_values_or_payment_evidence_are_returned(self):
        snap = fixture()
        snap["sheets"][0]["data"][0]["rowData"][1]["values"].append({
            "userEnteredValue": {"stringValue": "PERSONAL"}})
        result = self.check(snap)
        self.assertNotIn("PERSONAL", str(result))
        self.assertNotIn("J0077", str(result))
