"""Synthetic-only historical HQ-to-J review: no customer data is emitted."""
import json
import unittest

from hq_sheet_identity_review import review_historical_work
from hq_sheet_link_preflight import preflight_job_link
from test_sheet_link_preflight import fixture


WORK_ID = "a" * 32


def record(**updates):
    data = {"id": WORK_ID, "revision": 3, "name": "Synthetic Owner",
            "price_pence": 27000, "registration": "ZZ00ZZZ"}
    data.update(updates)
    return data


def selected_sheet(*, customer="Synthetic Owner", price=270.0):
    sheet = fixture()
    row = sheet["sheets"][0]["data"][0]["rowData"][1]
    row["values"] = [{} for _ in range(18)]
    row["values"][0] = {"userEnteredValue": {"stringValue": "J0077"}}
    if customer is not None:
        row["values"][2] = {"userEnteredValue": {"stringValue": customer}}
    if price is not None:
        row["values"][16] = {"userEnteredValue": {"numberValue": price}}
    preflight_job_link(sheet, job_ref="J0077", target_row=5, mode="existing")
    return sheet


class HistoricalWorkReviewTests(unittest.TestCase):
    def review(self, sheet=None, work=None, revision=3):
        return review_historical_work(
            sheet if sheet is not None else selected_sheet(),
            work=work if work is not None else record(),
            expected_revision=revision, target_row=5)

    def test_matching_values_are_only_advisory_not_identity_proof(self):
        result = self.review()
        self.assertEqual(result["customer_candidate_comparison"], "candidate_agreement")
        self.assertEqual(result["quoted_price_candidate_comparison"], "candidate_agreement")
        self.assertEqual(result["hq_candidate_review"], "manual_identity_review_required")
        for field in ("mapping_semantics_verified", "historical_mapping_verified",
                      "baseline_verified", "write_authorised", "live_sync", "reconciled"):
            self.assertFalse(result[field])
        response = json.dumps(result)
        for secret in ("Synthetic Owner", "ZZ00ZZZ", WORK_ID, "27000", "J0077"):
            self.assertNotIn(secret, response)

    def test_unequal_customer_or_price_never_claims_match(self):
        for sheet in (selected_sheet(customer="Somebody Else"),
                      selected_sheet(price=271.0)):
            with self.subTest(sheet=sheet):
                result = self.review(sheet)
                self.assertEqual(result["hq_candidate_review"], "conflict_or_difference")
                self.assertIn("requires_manual_review",
                              (result["customer_candidate_comparison"],
                               result["quoted_price_candidate_comparison"]))

    def test_unknown_values_and_non_penny_price_fail_closed(self):
        result = self.review(selected_sheet(customer=None, price=None))
        self.assertEqual(result["customer_candidate_comparison"], "unavailable")
        self.assertEqual(result["quoted_price_candidate_comparison"], "unavailable")
        self.assertFalse(result["historical_mapping_verified"])
        self.assertEqual(self.review(selected_sheet(price=270.005))[
            "quoted_price_candidate_comparison"], "requires_manual_review")
        self.assertEqual(self.review(selected_sheet(price=True))[
            "quoted_price_candidate_comparison"], "requires_manual_review")

    def test_revision_and_id_must_be_exact(self):
        for bad_record, rev in ((record(revision=4), 3),
                                (record(id="bad"), 3),
                                (record(revision=True), 1),
                                (record(), True)):
            with self.subTest(bad_record=bad_record, rev=rev), self.assertRaises(ValueError):
                self.review(work=bad_record, revision=rev)

    def test_does_not_accept_formula_price_as_approved_price(self):
        sheet = selected_sheet()
        sheet["sheets"][0]["data"][0]["rowData"][1]["values"][16] = {
            "userEnteredValue": {"formulaValue": "=270"}}
        # Preflight would reject this cell; the comparison also declines it.
        result = self.review(sheet)
        self.assertEqual(result["quoted_price_candidate_comparison"], "requires_manual_review")


if __name__ == "__main__":
    unittest.main()
