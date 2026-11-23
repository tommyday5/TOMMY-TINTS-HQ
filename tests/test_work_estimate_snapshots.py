"""Synthetic regression tests for owner-editable, frozen HQ Cost Library job plans."""
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
from hq_cost_library import apply_change
from hq_core_records import change_work_status, set_draft_financials
from hq_native_work import create_native_work
from hq_work_cost_estimates import apply_estimate, estimate_snapshot

ROOT = Path(__file__).resolve().parents[1]


class WorkEstimateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name,
            "HQ_SESSION_SECRET": "synthetic-estimates-session",
            "GOOGLE_CLIENT_ID": "synthetic",
            "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner = app.test_client()
        self.other = app.test_client()
        self.csrf = "synthetic-estimate-csrf"
        self.library = {
            "service_name": "Synthetic bodywork bonnet estimate",
            "default_hours": "2.75", "material_cost": "19.25",
            "consumables_cost": "6.00",
            "retail_multiplier": "1.3500",
            "trade_multiplier": "1.0000",
            "maintenance_multiplier": "1.1000",
            "insurance_multiplier": "1.5500",
            "notes": "Planning baseline only", "pricing_method": "Review",
            "approval_rule": "Owner approval", "breakdown_note": "",
        }
        with app.app_context():
            self.item_id = apply_change(
                action="create", item_id="", expected_revision=None,
                operation_id=uuid.uuid4().hex, fields=self.library,
                owner="synthetic-owner", secret=app.secret_key)
            self.work_id = create_native_work({
                "operation_id": uuid.uuid4().hex, "customer_id": "",
                "vehicle_id": "", "customer_name": "Synthetic only",
                "email": "synthetic-estimate@example.invalid",
                "phone": "07700 900111",
                "make": "Hyundai", "model": "i30", "model_year": "2022",
                "registration": "ZZ22 SYN", "kind": "quote",
                "service": "Bodywork & Paint", "customer_tier": "retail",
                "notes": "", "preferred_date": "", "preferred_time": "",
            })
        self.path = "/hq/work/" + self.work_id + "/estimate"
        self.form = {
            "csrf": self.csrf, "action": "save",
            "operation_id": uuid.uuid4().hex,
            "work_revision": "0", "expected_revision": "0",
            "source_item_id": self.item_id, "source_item_revision": "0",
            "hours": "2.75", "materials": "19.25",
            "consumables": "6.00", "note": "Synthetic scope",
        }

    def login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(
                hq_sid=issue_session(db, "synthetic-owner"),
                csrf=self.csrf, google_sub="synthetic-owner")

    def save(self):
        self.login()
        response = self.owner.post(self.path, data=self.form)
        self.assertEqual(response.status_code, 303, response.data[:300])
        return response

    def test_owner_auth_csrf_and_initial_prefill(self):
        self.assertEqual(self.other.get(self.path).status_code, 302)
        self.assertEqual(self.other.post(self.path, data=self.form).status_code, 302)
        self.login()
        page = self.owner.get(self.path + "?library_id=" + self.item_id)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"2.75", page.data)
        self.assertIn(b"19.25", page.data)
        self.assertIn(b"Planning only", page.data)
        self.assertIn("no-store", page.headers["Cache-Control"])
        self.assertEqual(self.owner.post(self.path, data={
            **self.form, "csrf": "bad"}).status_code, 403)
        with app.app_context():
            self.assertIsNone(estimate_snapshot(self.work_id)[1])
        self.assertEqual(self.owner.get(self.path + "?library_id=wrong").status_code, 400)
        self.assertEqual(self.owner.get("/hq/work/" + ("f"*32) + "/estimate").status_code, 404)

    def test_persistent_revisions_idempotency_and_archive_restore(self):
        self.save()
        self.assertEqual(self.owner.post(self.path, data=self.form).status_code, 303)
        with app.app_context():
            work, existing, _ = estimate_snapshot(self.work_id)
            self.assertEqual(work["kind"], "quote")
            self.assertEqual(existing["hours_hundredths"], 275)
            self.assertEqual(existing["material_pence"], 1925)
            self.assertEqual(existing["consumables_pence"], 600)
            self.assertEqual(existing["revision"], 0)
        self.assertIn(b"Synthetic scope", self.owner.get(self.path).data)
        changed = {**self.form, "operation_id": uuid.uuid4().hex,
                   "materials": "24.70", "expected_revision": "0"}
        self.assertEqual(self.owner.post(self.path, data=changed).status_code, 303)
        self.assertEqual(self.owner.post(self.path, data=changed).status_code, 303)
        with app.app_context():
            self.assertEqual(estimate_snapshot(self.work_id)[1]["material_pence"], 2470)
        stale = {**self.form, "operation_id": uuid.uuid4().hex}
        self.assertEqual(self.owner.post(self.path, data=stale).status_code, 200)
        self.assertIn(b"Estimate changed", self.owner.get(self.path).data) if False else None
        archived = {"csrf": self.csrf, "operation_id": uuid.uuid4().hex,
                    "action": "archive", "work_revision": "0", "expected_revision": "1"}
        self.assertEqual(self.owner.post(self.path, data=archived).status_code, 303)
        self.assertEqual(self.owner.post(self.path, data=archived).status_code, 303)
        self.assertEqual(self.owner.post(self.path, data={
            **changed, "operation_id": uuid.uuid4().hex,
            "expected_revision": "2"}).status_code, 200)
        restored = {**archived, "operation_id": uuid.uuid4().hex,
                    "action": "restore", "expected_revision": "2"}
        self.assertEqual(self.owner.post(self.path, data=restored).status_code, 303)
        with app.app_context(), connect() as db:
            self.assertEqual(
                [r[0] for r in db.execute(
                    "SELECT action FROM hq_work_cost_estimate_events "
                    "WHERE work_id=? ORDER BY revision", (self.work_id,))],
                ["save", "save", "archive", "restore"])
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM hq_job_sheet_links").fetchone()[0], 0
            ) if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='hq_job_sheet_links'"
            ).fetchone() else None
            self.assertIsNone(db.execute(
                "SELECT * FROM hq_work_records WHERE id=?", (self.work_id,)
            ).fetchone()["price_pence"])

    def test_source_is_snapshot_and_approved_work_locks(self):
        self.save()
        with app.app_context():
            apply_change(
                action="save", item_id=self.item_id, expected_revision=0,
                operation_id=uuid.uuid4().hex,
                fields={**self.library, "material_cost": "88.80"},
                owner="synthetic-owner", secret=app.secret_key)
            work, saved, active = estimate_snapshot(self.work_id)
            self.assertEqual(saved["material_pence"], 1925)
            self.assertEqual(saved["source_item_revision"], 0)
            self.assertEqual(active[0]["revision"], 1)
        # Reopening the original version remains editable, even if source later changes.
        self.assertEqual(self.owner.post(self.path, data={
            **self.form, "note": "Revised plan",
            "operation_id": uuid.uuid4().hex,
        }).status_code, 303)
        # A fresh attempt to claim a stale source version is rejected.
        with app.app_context():
            with self.assertRaises(ValueError):
                apply_estimate(self.work_id, "save", {
                    **self.form, "operation_id": uuid.uuid4().hex,
                    "source_item_id": "f" * 32,
                }, "synthetic-owner", app.secret_key)
            set_draft_financials(self.work_id, 20000, 0, expected_revision=0)
            change_work_status(self.work_id, "ready_for_review", expected_revision=1)
            change_work_status(self.work_id, "approved", expected_revision=2)
        page = self.owner.get(self.path)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"remains read-only", page.data)
        self.assertEqual(self.owner.post(self.path, data={
            **self.form, "operation_id": uuid.uuid4().hex,
            "expected_revision": "1", "work_revision": "3",
        }).status_code, 200)
        with app.app_context():
            self.assertEqual(estimate_snapshot(self.work_id)[1]["material_pence"], 1925)
            with connect() as db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM hq_work_cost_estimates"
                ).fetchone()[0], 1)

    def test_invalid_submission_cannot_mutate_real_work_or_public_app(self):
        self.login()
        for patch_values in (
            {"hours": "-1"}, {"hours": "201"}, {"materials": "1.999"},
            {"consumables": ""}, {"source_item_id": "bogus"},
            {"source_item_revision": "-1"}, {"note": "x"*301},
        ):
            with self.subTest(patch=patch_values):
                response = self.owner.post(self.path, data={
                    **self.form, **patch_values, "operation_id": uuid.uuid4().hex})
                self.assertEqual(response.status_code, 200)
        with app.app_context():
            self.assertIsNone(estimate_snapshot(self.work_id)[1])
        self.assertEqual(self.other.get("/book").status_code, 200)
        self.assertNotIn(b"Synthetic scope", self.other.get("/book").data)


if __name__ == "__main__":
    unittest.main()
