"""Regression tests: HQ job costing is read-only and tied to actual work pricing."""
import os
import tempfile
import unittest
from unittest.mock import patch

from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import create_test_job, set_draft_financials, change_work_status
from job_material_store import write_log, read_log
from hq_job_costing import costing_snapshot


class InternalJobCostingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {
            "HQ_DATA_DIR": temporary.name,
            "HQ_SESSION_SECRET": "internal-costing-tests",
            "GOOGLE_CLIENT_ID": "test", "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.client = app.test_client()

    def sign_in(self, subject="cost-owner"):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, subject), google_sub=subject, csrf="csrf")

    @staticmethod
    def log(sale="9999"):
        return {
            "lines": [{"name": "Clearcoat", "cost": "10.00"}],
            "sale": sale, "labour": "3", "actual_labour_rate": "18",
            "overhead": "25", "other_costs": "5",
        }

    def test_owner_only_read_only_and_approved_sale_is_authoritative(self):
        work = create_test_job()
        set_draft_financials(work, 35000, 7600, expected_revision=0)
        write_log("cost-owner", work, self.log(), expected_revision=0)
        url = "/api/hq/work-records/" + work + "/costing"
        self.assertEqual(self.client.get(url).status_code, 302)
        self.sign_in()
        draft = self.client.get(url)
        self.assertEqual(draft.status_code, 200)
        self.assertEqual(draft.headers["Cache-Control"], "no-store")
        self.assertFalse(draft.json["price_approved"])
        self.assertEqual(draft.json["price_pence"], 35000)
        self.assertIsNone(draft.json["estimated_contribution"])
        self.assertFalse(draft.json["saved_sale_matches_record"])
        self.assertNotIn("name", draft.json)
        self.assertNotIn("phone", draft.json)
        self.assertNotIn("email", draft.json)
        self.assertFalse(draft.json["payment_verified"])
        change_work_status(work, "ready_for_review", expected_revision=1)
        change_work_status(work, "approved", expected_revision=2)
        approved = self.client.get(url)
        self.assertTrue(approved.json["price_approved"])
        self.assertTrue(approved.json["complete_costs"])
        self.assertEqual(approved.json["materials"], "10.00")
        self.assertEqual(approved.json["actual_labour_cost"], "54.00")
        self.assertEqual(approved.json["total_recorded_cost"], "94.00")
        self.assertEqual(approved.json["estimated_contribution"], "256.00")
        self.assertEqual(approved.json["margin_percent"], "73.14")
        self.assertEqual(approved.json["material_line_count"], 1)
        self.assertEqual(approved.json["labour_hours"], "3")
        self.assertEqual(approved.json["actual_labour_rate"], "18")
        self.assertEqual(approved.json["allocated_overhead"], "25.00")
        self.assertEqual(approved.json["other_costs"], "5.00")
        self.assertEqual(approved.json["missing_cost_fields"], [])
        self.assertNotIn("Clearcoat", approved.get_data(as_text=True))
        # GET must never mutate the cost log or its revision.
        self.assertEqual(read_log("cost-owner", work)["revision"], 1)

    def test_missing_log_and_unpriced_job_do_not_fabricate_margin(self):
        work = create_test_job()
        self.sign_in()
        url = "/api/hq/work-records/" + work + "/costing"
        missing = self.client.get(url)
        self.assertFalse(missing.json["costing_saved"])
        self.assertIsNone(missing.json["total_recorded_cost"])
        self.assertIsNone(missing.json["estimated_contribution"])
        write_log("cost-owner", work, self.log("350"), expected_revision=0)
        unpriced = self.client.get(url)
        self.assertTrue(unpriced.json["costing_saved"])
        self.assertIsNone(unpriced.json["price_pence"])
        self.assertIsNone(unpriced.json["estimated_contribution"])
        self.assertIsNone(unpriced.json["saved_sale_matches_record"])
        # Costs still exist even without an approved selling price; never
        # treat the temporary calculation sale=0 as verified revenue.
        self.assertEqual(unpriced.json["total_recorded_cost"], "94.00")
        self.assertEqual(unpriced.json["materials"], "10.00")
        self.assertEqual(unpriced.json["actual_labour_cost"], "54.00")

    def test_incomplete_costs_and_owner_isolation(self):
        work = create_test_job()
        set_draft_financials(work, 10000, 0, expected_revision=0)
        change_work_status(work, "ready_for_review", expected_revision=1)
        change_work_status(work, "approved", expected_revision=2)
        write_log("cost-owner", work, {"lines": [], "labour": "2", "sale": "100"}, expected_revision=0)
        self.sign_in("other-owner")
        other = self.client.get("/api/hq/work-records/" + work + "/costing")
        self.assertFalse(other.json["costing_saved"])
        self.sign_in()
        incomplete = self.client.get("/api/hq/work-records/" + work + "/costing")
        self.assertFalse(incomplete.json["complete_costs"])
        self.assertEqual(incomplete.json["missing_cost_fields"],
                         ["actual_labour_rate", "overhead", "other_costs"])
        self.assertEqual(incomplete.json["material_line_count"], 0)
        self.assertEqual(incomplete.json["labour_hours"], "2")
        self.assertEqual(incomplete.json["materials"], "0.00")
        self.assertIsNone(incomplete.json["actual_labour_cost"])
        self.assertIsNone(incomplete.json["allocated_overhead"])
        self.assertIsNone(incomplete.json["other_costs"])
        self.assertIsNone(incomplete.json["total_recorded_cost"])
        self.assertIsNone(incomplete.json["estimated_contribution"])
        self.assertTrue(incomplete.json["saved_sale_matches_record"])

    def test_invalid_record_and_bad_legacy_costing_are_safe(self):
        self.sign_in()
        bad = self.client.get("/api/hq/work-records/../../costing")
        self.assertNotEqual(bad.status_code, 200)
        self.assertEqual(self.client.get("/api/hq/work-records/not-a-work-id/costing").status_code, 400)
        self.assertEqual(self.client.get("/api/hq/work-records/" + "a"*32 + "/costing").status_code, 404)
        work = create_test_job()
        set_draft_financials(work, 20000, 0, expected_revision=0)
        change_work_status(work, "ready_for_review", expected_revision=1)
        change_work_status(work, "approved", expected_revision=2)
        write_log("cost-owner", work, {
            "lines": [{"cost": "garbage"}], "labour": "3",
            "actual_labour_rate": "10", "overhead": "5", "other_costs": "5",
        }, expected_revision=0)
        response = self.client.get("/api/hq/work-records/" + work + "/costing")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["complete_costs"])
        self.assertIsNone(response.json["total_recorded_cost"])
        self.assertIsNone(response.json["materials"])
        self.assertIn("invalid", response.json["note"])
        self.assertIsNone(response.json["estimated_contribution"])

    def test_existing_cards_can_link_to_internal_costing(self):
        self.sign_in()
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('"/api/hq/work-records/"+encodeURIComponent(job.id)+"/costing"', html)
        self.assertIn('currentMaterialJob=job.id', html)
        self.assertIn('await loadServerMat()', html)
        self.assertIn('snapshot.material_line_count', html)
        self.assertIn('snapshot.labour_hours', html)
        self.assertIn('snapshot.allocated_overhead', html)
        self.assertIn('snapshot.missing_cost_fields', html)


if __name__ == "__main__":
    unittest.main()
