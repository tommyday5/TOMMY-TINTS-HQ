"""Synthetic-only Jobs header consistency checks for independent Sheets reads."""
import copy
import unittest

from hq_job_sync import WORKBOOK
from hq_sheet_link_preflight import (
    JOBS_SHEET_ID, VERIFIED_HEADERS, complete_trailing_blank_rows,
    verify_unformatted_candidate,
)


def cell(value):
    return {"userEnteredValue": {"stringValue": value}}


def fixture():
    headings = [f"Heading {i}" for i in range(36)]
    for index, title in VERIFIED_HEADERS.items():
        headings[index] = title
    original = {
        "spreadsheetId": WORKBOOK,
        "sheets": [{
            "properties": {
                "sheetId": JOBS_SHEET_ID, "title": "Jobs",
                "gridProperties": {"rowCount": 7, "columnCount": 94},
            },
            "data": [{
                "startRow": 3, "startColumn": 0,
                "rowData": [
                    {"values": [cell(title) for title in headings]},
                    {"values": [cell("J0077"), {}, cell("Synthetic customer")]
                     + [{} for _ in range(13)]
                     + [{"userEnteredValue": {"numberValue": 270}}]},
                    {"values": []},
                    {"values": [cell("J0082")]},
                ],
            }],
        }],
    }
    displayed = {
        "range": "Jobs!A4:AJ7", "majorDimension": "ROWS",
        "values": [
            list(headings),
            ["J0077", "", "Synthetic customer"] + [""] * 13 + ["£270.00"],
            [], ["J0082"],
        ],
    }
    numeric = copy.deepcopy(displayed)
    numeric["values"][1][16] = 270
    return original, displayed, numeric


class SheetsHeaderConsistencyTests(unittest.TestCase):
    def test_unchanged_headers_allow_read_only_preflight_comparison(self):
        snapshot, formula, numeric = fixture()
        completed = complete_trailing_blank_rows(
            snapshot, formula, candidate_row=5)
        self.assertEqual(len(completed["sheets"][0]["data"][0]["rowData"]), 4)
        verify_unformatted_candidate(snapshot, numeric, candidate_row=5)
        self.assertEqual(snapshot["sheets"][0]["data"][0]["rowData"][0]
                         ["values"][2]["userEnteredValue"]["stringValue"],
                         "Heading 2")

    def test_formula_read_rejects_changed_or_newly_formula_based_headings(self):
        for changed in ("Client", '=IF(1,"Heading 2","")', ""):
            with self.subTest(changed=changed):
                snapshot, formula, _ = fixture()
                formula["values"][0][2] = changed
                with self.assertRaisesRegex(ValueError, "column headings changed"):
                    complete_trailing_blank_rows(snapshot, formula, candidate_row=5)

    def test_formula_read_rejects_missing_trailing_heading(self):
        snapshot, formula, _ = fixture()
        formula["values"][0] = formula["values"][0][:35]
        with self.assertRaisesRegex(ValueError, "column headings changed"):
            complete_trailing_blank_rows(snapshot, formula, candidate_row=5)

    def test_typed_read_rejects_header_changed_after_formula_read(self):
        for changed in ("Client", "", 123):
            with self.subTest(changed=changed):
                snapshot, formula, numeric = fixture()
                complete_trailing_blank_rows(snapshot, formula, candidate_row=5)
                numeric["values"][0][2] = changed
                with self.assertRaisesRegex(ValueError, "column headings changed"):
                    verify_unformatted_candidate(
                        snapshot, numeric, candidate_row=5)


if __name__ == "__main__":
    unittest.main()
