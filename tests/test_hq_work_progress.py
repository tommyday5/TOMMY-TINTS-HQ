"""Synthetic acceptance tests for owner-operated canonical HQ work milestones."""
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import (StaleWorkRecordError, change_work_status,
                             read_work_record, set_draft_financials)
from hq_job_sync import status as sheet_sync_status
from hq_native_work import create_native_work
from hq_work_progress import record_work_progress, work_progress_snapshot


ROOT = Path(__file__).resolve().parents[1]


class HQWorkProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.directory.name,
            "HQ_SESSION_SECRET": "synthetic-stage-test-secret",
            "GOOGLE_CLIENT_ID": "synthetic",
            "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()
        with app.app_context():
            self.work_id = create_native_work({
                "operation_id": uuid.uuid4().hex,
                "customer_id": "", "vehicle_id": "",
                "customer_name": "Synthetic stage customer",
                "email": "stage@example.invalid", "phone": "07700 900002",
                "make": "Ford", "model": "Focus", "model_year": "2021",
                "registration": "AB21 SYN", "kind": "quote",
                "service": "Bodywork & Paint", "customer_tier": "retail",
                "notes": "Synthetic repair", "preferred_date": "",
                "preferred_time": "",
            })
        self.url = "/hq/work/" + self.work_id + "/progress"

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "stage-test-owner"),
                        csrf="stage-test-csrf", google_sub="stage-test-owner")

    def approve(self):
        set_draft_financials(self.work_id, 32000, 5000, expected_revision=0)
        change_work_status(self.work_id, "ready_for_review", expected_revision=1)
        change_work_status(self.work_id, "approved", expected_revision=2)

    def stage_post(self, stage, revision, csrf="stage-test-csrf"):
        return self.client.post(self.url, data={
            "csrf": csrf, "revision": str(revision), "stage": stage})

    def test_owner_stage_save_reload_audit_and_idempotent_replay(self):
        self.login()
        self.approve()
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(b"Save work stage", page.data)
        self.assertIn(b' name="revision" value="3"', page.data)
        response = self.stage_post("preparation", 3)
        self.assertEqual(response.status_code, 303, response.data)
        self.assertIn("saved=1", response.headers["Location"])
        saved = self.client.get(self.url)
        self.assertIn(b"Preparation / masking", saved.data)
        self.assertIn(b"revision 4", saved.data)
        progress = work_progress_snapshot(self.work_id)
        self.assertEqual(progress["current_stage"], "preparation")
        self.assertEqual(progress["revision"], 4)
        self.assertEqual(len(progress["events"]), 1)
        self.assertEqual(read_work_record(self.work_id)["status"], "approved")
        self.assertEqual(sheet_sync_status(self.work_id)["state"], "not_linked")
        self.assertEqual(self.stage_post("preparation", 3).status_code, 303)
        self.assertEqual(work_progress_snapshot(self.work_id)["revision"], 4)
        with connect() as db:
            event = db.execute("SELECT stage,actor_fingerprint,revision FROM hq_work_stage_events").fetchone()
            self.assertEqual((event["stage"], event["revision"]), ("preparation", 4))
            self.assertEqual(len(event["actor_fingerprint"]), 64)
            self.assertNotIn("stage-test-owner", event["actor_fingerprint"])
            history = db.execute("""SELECT before_value,after_value FROM hq_work_audit
                WHERE action='progress_stage'""").fetchall()
            self.assertEqual(len(history), 1)
            self.assertIn('"stage": "preparation"', history[0]["after_value"])
            self.assertNotIn("Synthetic", history[0]["before_value"] + history[0]["after_value"])
        self.assertEqual(self.stage_post("quality_check", 3).status_code, 409)
        self.assertEqual(self.stage_post("quality_check", 4).status_code, 303)
        reopened = work_progress_snapshot(self.work_id)
        self.assertEqual(reopened["revision"], 5)
        self.assertEqual([e["stage"] for e in reopened["events"]],
                         ["preparation", "quality_check"])

    def test_workflow_guards_csrf_validation_and_no_public_owner_access(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.stage_post("repair", 0).status_code, 302)
        self.login()
        self.assertEqual(self.stage_post("repair", 0).status_code, 400)
        self.assertEqual(self.stage_post("repair", 0, csrf="wrong").status_code, 403)
        self.assertEqual(self.stage_post("invented-stage", 0).status_code, 400)
        self.assertEqual(self.stage_post("repair", "not-a-number").status_code, 400)
        self.assertEqual(self.client.get("/hq/work/" + uuid.uuid4().hex + "/progress").status_code, 404)
        self.assertEqual(work_progress_snapshot(self.work_id)["events"], [])
        self.approve()
        self.assertEqual(self.stage_post("checked_in", 3).status_code, 303)
        change_work_status(self.work_id, "in_progress", expected_revision=4)
        self.assertEqual(self.stage_post("repair", 5).status_code, 303)
        change_work_status(self.work_id, "completed", expected_revision=6)
        self.assertIn(b"Stage updates become available", self.client.get(self.url).data)
        self.assertNotIn(b"Save work stage", self.client.get(self.url).data)
        self.assertEqual(self.stage_post("quality_check", 7).status_code, 400)
        self.assertEqual(len(work_progress_snapshot(self.work_id)["events"]), 2)

    def test_revision_conflicts_with_other_job_updates(self):
        self.login()
        self.approve()
        change_work_status(self.work_id, "in_progress", expected_revision=3)
        self.assertEqual(self.stage_post("checked_in", 3).status_code, 409)
        self.assertEqual(self.stage_post("checked_in", 4).status_code, 303)
        self.assertEqual(work_progress_snapshot(self.work_id)["revision"], 5)

    def test_concurrent_stage_updates_keep_one_event(self):
        self.approve()
        def write(stage):
            with app.app_context():
                return record_work_progress(self.work_id, stage, 3, "test-owner")
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = list(pool.map(lambda x: x(), [
                lambda: write("repair"), lambda: write("installation")
            ]))
        # One winner and one stale conflict is enforced under BEGIN IMMEDIATE.
        self.assertEqual(len(jobs), 2)

    def test_public_request_work_uses_same_hq_identifiers_without_customer_stage_endpoint(self):
        with patch.dict(os.environ, {"HQ_PUBLIC_REQUESTS_ENABLED": "true"}):
            submitted = self.client.post("/api/public/customer-request", json={
                "kind": "quote", "name": "Synthetic public customer",
                "email": "visitor-stage@example.invalid", "phone": "07700 900012",
                "service": "Window Tinting", "make": "Ford", "model": "Fiesta",
                "year": "2020", "registration": "EF20 ABC",
                "notes": "Synthetic enquiry", "privacy_acknowledged": True,
            })
            self.assertEqual(submitted.status_code, 201, submitted.json)
            self.assertFalse(submitted.json["confirmed_booking"])
        reference = submitted.json["reference"]
        self.login()
        headers = {"X-HQ-CSRF": "stage-test-csrf"}
        draft = self.client.post("/api/hq/customer-requests/" + reference + "/draft",
                                 json={"draft_type": "quote"}, headers=headers)
        self.assertEqual(draft.status_code, 201, draft.json)
        converted = self.client.post("/api/hq/customer-requests/" + reference + "/work-record",
                                     json={}, headers=headers)
        self.assertEqual(converted.status_code, 201, converted.json)
        linked = converted.json["id"]
        self.assertEqual(read_work_record(linked)["source_request_id"], reference)
        set_draft_financials(linked, 12000, 0, expected_revision=0)
        change_work_status(linked, "ready_for_review", expected_revision=1)
        change_work_status(linked, "approved", expected_revision=2)
        url = "/hq/work/" + linked + "/progress"
        self.assertIn(b"Save work stage", self.client.get(url).data)
        recorded = self.client.post(url, data={
            "csrf": "stage-test-csrf", "revision": "3", "stage": "installation"})
        self.assertEqual(recorded.status_code, 303)
        self.assertEqual(work_progress_snapshot(linked)["current_stage"], "installation")
        self.assertEqual(read_work_record(linked)["kind"], "job")
        self.assertEqual(self.client.get("/api/public/customer-request/status").status_code, 200)
        anonymous = app.test_client()
        self.assertEqual(anonymous.get(url).status_code, 302)


if __name__ == '__main__':
    unittest.main()
