"""Synthetic-only verification of typed Google Sheets input cells; no provider writes."""
import unittest
from unittest.mock import Mock, patch

from hq_sheet_link_preflight import verify_unformatted_candidate, preflight_job_link
from hq_sheets_live import read_job_link_snapshot
from test_sheet_link_preflight import fixture


def typed_fixture():
    snap = fixture()
    rows = snap["sheets"][0]["data"][0]["rowData"]
    rows.pop()  # Google omits trailing blank, but selected row has raw cells.
    cells = rows[1]["values"]
    cells.extend({} for _ in range(18 - len(cells)))
    cells[16] = {"userEnteredValue": {"numberValue": 100}}
    cells[17] = {"userEnteredValue": {"boolValue": True}}
    return snap


def value_response(candidate):
    return {"range": "Jobs!A4:AJ7", "majorDimension": "ROWS",
            "values": [["Job ID"], candidate]}


class TypedCandidateChecks(unittest.TestCase):
    def test_unformatted_read_verifies_price_and_boolean_without_currency_mismatch(self):
        snap = typed_fixture()
        rendered = ["J0077"] + [""] * 15 + ["£100.00", True]
        unformatted = ["J0077"] + [""] * 15 + [100, True]
        responses = []
        for data in (snap, value_response(rendered), value_response(unformatted)):
            response = Mock()
            response.json.return_value = data
            responses.append(response)
        with patch("hq_sheets_live.requests.get", side_effect=responses) as get:
            completed = read_job_link_snapshot("synthetic-token", selected_row=5)
        self.assertEqual(get.call_count, 3)
        params = dict(get.call_args.kwargs["params"])
        self.assertEqual(params["valueRenderOption"], "UNFORMATTED_VALUE")
        self.assertEqual(params["dateTimeRenderOption"], "SERIAL_NUMBER")
        result = preflight_job_link(completed, job_ref="J0077",
                                    target_row=5, mode="existing")
        self.assertFalse(result["live_sync"])
        self.assertFalse(result["baseline_verified"])

    def test_changed_number_boolean_or_reference_cannot_pass(self):
        snap = typed_fixture()
        for candidate, pattern in (
            (["J0077"] + [""] * 15 + [200, True], "numeric"),
            (["J0077"] + [""] * 15 + [True, True], "numeric"),
            (["J0077"] + [""] * 15 + [100, False], "boolean"),
            (["J0099"] + [""] * 15 + [100, True], "reference"),
        ):
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(ValueError, pattern):
                    verify_unformatted_candidate(
                        snap, value_response(candidate), candidate_row=5)

    def test_third_read_failure_does_not_return_approved_candidate(self):
        snap = typed_fixture()
        first, second = Mock(), Mock()
        first.json.return_value = snap
        second.json.return_value = value_response(
            ["J0077"] + [""] * 15 + ["£100.00", True])
        with patch("hq_sheets_live.requests.get",
                   side_effect=[first, second, RuntimeError("synthetic failure")]) as get:
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                read_job_link_snapshot("synthetic-token", selected_row=5)
        self.assertEqual(get.call_count, 3)

    def test_trailing_new_row_and_wrong_source_range_rejected(self):
        snap = typed_fixture()
        self.assertRaisesRegex(
            ValueError, "coverage", verify_unformatted_candidate, snap,
            value_response(["J0077", "", "", "J0088"]), candidate_row=5)
        wrong = value_response(["J0077"] + [""] * 15 + [100, True])
        wrong["range"] = "Jobs!A4:AJ6"
        with self.assertRaisesRegex(ValueError, "exact"):
            verify_unformatted_candidate(snap, wrong, candidate_row=5)
