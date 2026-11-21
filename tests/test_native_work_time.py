"""Synthetic end-to-end acceptance for canonical HQ actual-time ledger."""
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
from hq_core_records import change_work_status, read_work_record, set_draft_financials, StaleWorkRecordError
from hq_job_sync import status as sheet_sync_status
from hq_native_work import create_native_work
from hq_work_time import append_work_time, time_ledger_snapshot, void_work_time

ROOT = Path(__file__).resolve().parents[1]


class ActualWorkTimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.directory.name,
            "HQ_SESSION_SECRET": "synthetic-time-ledger-secret",
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
                "customer_name": "Synthetic workshop customer",
                "email": "worktime@example.invalid", "phone": "07700 900002",
                "make": "Hyundai", "model": "i30", "model_year": "2022",
                "registration": "TS22 ABC", "kind": "job",
                "service": "Bodywork & Paint", "customer_tier": "trade",
                "notes": "Synthetic test only", "preferred_date": "", "preferred_time": "",
            })
        self.url = "/hq/work/" + self.work_id + "/time"

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "time-owner"),
                        csrf="time-csrf", google_sub="time-owner")

    def approve(self):
        set_draft_financials(self.work_id, 35000, 5000, expected_revision=0)
        change_work_status(self.work_id, "ready_for_review", expected_revision=1)
        change_work_status(self.work_id, "approved", expected_revision=2)

    def form(self, revision="3", operation=None, rate="", minutes="120", stage="repair"):
        return {"csrf": "time-csrf", "revision": revision, "operation_id": operation or uuid.uuid4().hex,
                "minutes": minutes, "stage": stage, "hourly_cost_pence": rate,
                "note": "Synthetic test labour"}

    def test_owner_ui_actual_time_save_reload_unknown_rate_and_corrected_cost(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.login()
        self.assertEqual(self.client.post(self.url, data=self.form(revision="0")).status_code, 400)
        self.approve()
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(b"Save actual time", page.data)
        self.assertIn(b'name="revision" value="3"', page.data)
        first = self.form()
        response = self.client.post(self.url, data=first)
        self.assertEqual(response.status_code, 303, response.data)
        saved = self.client.get(self.url)
        self.assertIn(b"120 minutes", saved.data)
        self.assertIn(b"unknown", saved.data)
        snapshot = time_ledger_snapshot(self.work_id)
        self.assertEqual(snapshot["minutes_total"], 120)
        self.assertEqual(snapshot["unknown_rate_count"], 1)
        self.assertIsNone(snapshot["actual_labour_cost_pence"])
        self.assertEqual(snapshot["revision"], 4)
        self.assertEqual(self.client.post(self.url, data=first).status_code, 303)
        self.assertEqual(time_ledger_snapshot(self.work_id)["entry_count"], 1)

        second = self.form(revision="4", rate="1800", minutes="90", stage="finishing")
        self.assertEqual(self.client.post(self.url, data=second).status_code, 303)
        snapshot = time_ledger_snapshot(self.work_id)
        self.assertEqual(snapshot["entry_count"], 2)
        self.assertEqual(snapshot["minutes_total"], 210)
        self.assertEqual(snapshot["known_cost_subtotal_pence"], 2700)
        self.assertIsNone(snapshot["actual_labour_cost_pence"])
        self.assertEqual(read_work_record(self.work_id)["price_pence"], 35000)
        self.assertEqual(read_work_record(self.work_id)["deposit_pence"], 5000)
        self.assertFalse(snapshot["payment_verified"])
        self.assertEqual(sheet_sync_status(self.work_id)["state"], "not_linked")
        self.assertIn(b"Record actual workshop time",
                      self.client.get("/hq/work/" + self.work_id + "/edit").data)
        self.assertIn(b"Record actual workshop time",
                      self.client.get("/hq/work/" + self.work_id + "/progress").data)

        unknown = next(e for e in snapshot["entries"] if e["hourly_cost_pence"] is None)
        self.assertEqual(self.client.post(
            self.url + "/" + unknown["entry_id"] + "/void",
            data={"csrf": "time-csrf", "revision": "5"}
        ).status_code, 303)
        latest = time_ledger_snapshot(self.work_id)
        self.assertEqual(latest["minutes_total"], 90)
        self.assertEqual(latest["actual_labour_cost_pence"], 2700)
        self.assertEqual(latest["entry_count"], 1)
        self.assertTrue(any(e["voided_at"] for e in latest["entries"]))
        with connect() as db:
            actions = [r["action"] for r in db.execute(
                "SELECT action FROM hq_work_audit WHERE work_id=? ORDER BY revision",
                (self.work_id,))]
            self.assertEqual(actions[-3:], ["actual_time_added", "actual_time_added", "actual_time_voided"])
            serialized = " ".join(r["after_value"] for r in db.execute(
                "SELECT after_value FROM hq_work_audit WHERE work_id=?", (self.work_id,)))
            self.assertNotIn("Synthetic test labour", serialized)
        self.assertEqual(self.client.post(
            self.url + "/" + unknown["entry_id"] + "/void",
            data={"csrf": "time-csrf", "revision": "5"}
        ).status_code, 303)
        self.assertEqual(time_ledger_snapshot(self.work_id)["revision"], 6)

    def test_validation_csrf_no_public_access_and_closed_job_lock(self):
        self.login()
        self.approve()
        self.assertEqual(self.client.post(self.url, data=self.form() | {"csrf": "bad"}).status_code, 403)
        for overrides in (
            {"minutes": "0"}, {"minutes": "1441"}, {"minutes": "1.5"},
            {"stage": "custom"}, {"hourly_cost_pence": "-1"},
            {"hourly_cost_pence": "1000001"}, {"note": "x" * 161},
            {"operation_id": "bad"},
        ):
            with self.subTest(overrides=overrides):
                response = self.client.post(self.url, data=self.form() | overrides)
                self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.client.post(
            self.url, data=self.form(revision="stale")).status_code, 400)
        self.assertEqual(self.client.get("/hq/work/" + uuid.uuid4().hex + "/time").status_code, 404)
        self.assertEqual(time_ledger_snapshot(self.work_id)["entry_count"], 0)
        first = self.form()
        self.assertEqual(self.client.post(self.url, data=first).status_code, 303)
        change_work_status(self.work_id, "in_progress", expected_revision=4)
        change_work_status(self.work_id, "completed", expected_revision=5)
        locked = self.client.get(self.url)
        self.assertIn(b"New time entries and corrections", locked.data)
        self.assertNotIn(b"Save actual time", locked.data)
        self.assertEqual(self.client.post(self.url, data=self.form(revision="6")).status_code, 400)
        # Authentication is mandatory even for reading private staff costs.
        other = app.test_client()
        self.assertEqual(other.get(self.url).status_code, 302)
        self.assertEqual(other.post(self.url, data=self.form(revision="6")).status_code, 302)

    def test_stale_browser_and_conflicting_replay_fail_closed(self):
        self.login()
        self.approve()
        first = self.form()
        self.assertEqual(self.client.post(self.url, data=first).status_code, 303)
        self.assertEqual(self.client.post(self.url, data=self.form(revision="3")).status_code, 409)
        same_op_changed = self.form(revision="4", operation=first["operation_id"], minutes="40")
        self.assertEqual(self.client.post(self.url, data=same_op_changed).status_code, 409)
        self.assertEqual(time_ledger_snapshot(self.work_id)["entry_count"], 1)
        entry = time_ledger_snapshot(self.work_id)["entries"][0]
        self.assertEqual(self.client.post(
            self.url + "/" + entry["entry_id"] + "/void",
            data={"csrf": "time-csrf", "revision": "3"}
        ).status_code, 303)
        self.assertIsNone(time_ledger_snapshot(self.work_id)["entries"][0]["voided_at"])
        self.assertEqual(self.client.post(
            self.url + "/" + entry["entry_id"] + "/void",
            data={"csrf": "time-csrf", "revision": "4"}
        ).status_code, 303)
        self.assertEqual(time_ledger_snapshot(self.work_id)["entry_count"], 0)
        self.assertIsNone(time_ledger_snapshot(self.work_id)["actual_labour_cost_pence"])

    def test_simultaneous_writes_single_revision_winner(self):
        self.approve()
        def append():
            with app.app_context():
                try:
                    append_work_time(self.work_id, self.form(), 3, "time-owner")
                    return "saved"
                except StaleWorkRecordError:
                    return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: append(), range(2)))
        self.assertEqual(sorted(outcomes), ["conflict", "saved"])
        self.assertEqual(time_ledger_snapshot(self.work_id)["entry_count"], 1)
        self.assertEqual(time_ledger_snapshot(self.work_id)["revision"], 4)

    def test_public_customer_submission_converts_to_same_canonical_job(self):
        with patch.dict(os.environ, {"HQ_PUBLIC_REQUESTS_ENABLED": "true"}):
            submitted = self.client.post("/api/public/customer-request", json={
                "kind": "quote", "name": "Synthetic public enquiry",
                "email": "visitor-timelog@example.invalid", "phone": "07700 900999",
                "service": "Window Tinting", "make": "Ford", "model": "Fiesta",
                "year": "2020", "registration": "EF20 ABC",
                "notes": "Synthetic quote", "privacy_acknowledged": True,
            })
        self.assertEqual(submitted.status_code, 201, submitted.json)
        self.assertFalse(submitted.json["confirmed_booking"])
        self.login()
        reference = submitted.json["reference"]
        headers = {"X-HQ-CSRF": "time-csrf"}
        self.assertEqual(self.client.post(
            "/api/hq/customer-requests/" + reference + "/draft",
            json={"draft_type": "quote"}, headers=headers).status_code, 201)
        converted = self.client.post(
            "/api/hq/customer-requests/" + reference + "/work-record",
            json={}, headers=headers)
        self.assertEqual(converted.status_code, 201, converted.json)
        linked = converted.json["id"]
        set_draft_financials(linked, 15000, 0, expected_revision=0)
        change_work_status(linked, "ready_for_review", expected_revision=1)
        change_work_status(linked, "approved", expected_revision=2)
        self.assertEqual(self.client.post(
            "/hq/work/" + linked + "/time",
            data=self.form(rate="2000", minutes="30", stage="installation")).status_code, 303)
        self.assertEqual(time_ledger_snapshot(linked)["actual_labour_cost_pence"], 1000)
        self.assertEqual(read_work_record(linked)["source_request_id"], reference)
        self.assertEqual(read_work_record(linked)["kind"], "job")
        self.assertEqual(app.test_client().get("/hq/work/" + linked + "/time").status_code, 302)


if __name__ == "__main__":
    unittest.main()
