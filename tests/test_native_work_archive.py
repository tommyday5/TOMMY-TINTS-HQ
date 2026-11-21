"""Synthetic end-to-end owner archive/restore acceptance, no production records."""
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app import app
from customer_request_store import create_request, create_review_draft
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import (
    create_internal_work_record, list_work_records, read_work_record,
    search_work_records, recent_work_records, set_draft_financials,
    change_work_status, StaleWorkRecordError,
)
from hq_native_work import create_native_work, update_native_work
from hq_work_archive import archived_work_snapshot, set_work_archived


class NativeWorkArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": temp.name,
            "HQ_SESSION_SECRET": "synthetic-archive-test",
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
                "customer_name": "Synthetic archive customer",
                "email": "archive@example.invalid", "phone": "07700 900087",
                "make": "Ford", "model": "Focus", "model_year": "2021",
                "registration": "ZZ21 TST", "kind": "quote",
                "service": "Window Tinting", "customer_tier": "retail",
                "notes": "Archive fixture", "preferred_date": "", "preferred_time": "",
            })
        self.path = "/hq/work/" + self.work_id + "/archive"

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "archive-owner"),
                        csrf="archive-csrf", google_sub="archive-owner")

    def post(self, action, revision, operation=None, **kwargs):
        payload = {
            "csrf": "archive-csrf", "action": action,
            "revision": str(revision), "operation_id": operation or uuid.uuid4().hex,
        }
        payload.update(kwargs)
        return self.client.post(self.path, data=payload)

    def test_owner_only_ui_archive_reload_restore_and_retained_history(self):
        self.assertEqual(self.client.get(self.path).status_code, 302)
        self.assertEqual(self.client.get("/hq/work/archived").status_code, 302)
        self.assertEqual(self.client.post(self.path, data={}).status_code, 302)
        self.login()
        first = self.client.get(self.path)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["Cache-Control"], "no-store")
        self.assertIn(b"Archive HQ work", first.data)
        self.assertIn(b"Archive / restore", self.client.get(
            "/hq/work/" + self.work_id + "/edit").data)
        self.assertIn(self.work_id, {r["id"] for r in list_work_records()})
        self.assertEqual(search_work_records()["total"], 1)
        self.assertEqual(self.client.get("/api/hq/work-records/search").json["total"], 1)

        operation = uuid.uuid4().hex
        saved = self.post("archive", 0, operation=operation)
        self.assertEqual(saved.status_code, 303)
        self.assertEqual(archived_work_snapshot(self.work_id)["revision"], 1)
        self.assertEqual(read_work_record(self.work_id)["status"], "draft")
        self.assertTrue(archived_work_snapshot(self.work_id)["is_archived"])
        self.assertEqual(search_work_records()["total"], 0)
        self.assertEqual(search_work_records(archived=True)["total"], 1)
        self.assertEqual(list_work_records(), [])
        self.assertEqual(recent_work_records("quote"), [])
        self.assertEqual(self.client.get("/api/hq/work-records/search").json["total"], 0)
        archived = self.client.get("/hq/work/archived")
        self.assertIn(self.work_id[:10].encode(), archived.data)
        self.assertIn(b"Restore HQ work", self.client.get(self.path).data)
        self.assertIn(b"Restore it before editing", self.client.get(
            "/hq/work/" + self.work_id + "/edit").data)
        self.assertEqual(self.post("archive", 0, operation=operation).status_code, 303)
        self.assertEqual(archived_work_snapshot(self.work_id)["revision"], 1)
        with self.assertRaisesRegex(ValueError, "Restore"):
            set_draft_financials(self.work_id, 15000, 1000, expected_revision=1)
        with self.assertRaisesRegex(ValueError, "Restore"):
            change_work_status(self.work_id, "ready_for_review", expected_revision=1)
        with self.assertRaisesRegex(ValueError, "Restore"):
            update_native_work(self.work_id, {
                "service": "Window Tinting", "customer_tier": "retail",
                "notes": "Edited", "preferred_date": "", "preferred_time": "",
            }, expected_revision=1)

        restored = self.post("restore", 1)
        self.assertEqual(restored.status_code, 303)
        self.assertFalse(archived_work_snapshot(self.work_id)["is_archived"])
        self.assertEqual(archived_work_snapshot(self.work_id)["revision"], 2)
        self.assertEqual([e["action"] for e in archived_work_snapshot(
            self.work_id)["events"]], ["restore", "archive"])
        self.assertEqual(search_work_records()["total"], 1)
        self.assertEqual(self.client.get("/hq/work/archived").status_code, 200)
        self.assertEqual(self.client.get("/api/hq/work-records/search").json["total"], 1)
        # An old replay cannot re-archive after a later restoration.
        self.assertEqual(self.post("archive", 0, operation=operation).status_code, 303)
        self.assertFalse(archived_work_snapshot(self.work_id)["is_archived"])
        self.assertEqual(archived_work_snapshot(self.work_id)["revision"], 2)
        self.assertEqual(read_work_record(self.work_id)["source_request_id"], "HQ-" + self.work_id)

    def test_csrf_revisions_and_conflicting_replays_fail_closed(self):
        self.login()
        self.assertEqual(self.post("archive", 0, csrf="wrong").status_code, 403)
        self.assertEqual(self.post("archive", "bad").status_code, 400)
        self.assertEqual(self.post("hack", 0).status_code, 400)
        self.assertEqual(self.post("restore", 0).status_code, 400)
        self.assertEqual(self.post("archive", 50).status_code, 409)
        key = uuid.uuid4().hex
        self.assertEqual(self.post("archive", 0, operation=key).status_code, 303)
        self.assertEqual(self.post("restore", 1, operation=key).status_code, 409)
        self.assertEqual(self.post("archive", 0).status_code, 409)
        self.assertEqual(self.client.get("/hq/work/archived?offset=-1").status_code, 400)
        self.assertEqual(self.client.get("/hq/work/not-an-id/archive").status_code, 404)
        with self.assertRaises(ValueError):
            search_work_records(archived="true")

    def test_only_terminal_or_draft_native_work_can_be_archived(self):
        self.login()
        set_draft_financials(self.work_id, 20000, 5000, expected_revision=0)
        change_work_status(self.work_id, "ready_for_review", expected_revision=1)
        change_work_status(self.work_id, "approved", expected_revision=2)
        self.assertEqual(self.post("archive", 3).status_code, 400)
        change_work_status(self.work_id, "in_progress", expected_revision=3)
        self.assertEqual(self.post("archive", 4).status_code, 400)
        change_work_status(self.work_id, "completed", expected_revision=4)
        self.assertEqual(self.post("archive", 5).status_code, 303)
        original = read_work_record(self.work_id)
        self.assertEqual(original["kind"], "job")
        self.assertEqual(original["status"], "completed")
        self.assertEqual(original["price_pence"], 20000)
        self.assertEqual(original["deposit_pence"], 5000)
        self.assertEqual(self.post("restore", 6).status_code, 303)
        self.assertEqual(read_work_record(self.work_id)["status"], "completed")

    def test_public_enquiry_conversion_is_not_silently_reclassified_native(self):
        req = create_request({
            "kind": "quote", "name": "Synthetic public customer",
            "email": "public-archive@example.invalid", "phone": "07700 900001",
            "service": "Window Tinting", "make": "Ford", "model": "Fiesta",
            "year": "2020", "registration": "AA20 TST", "date": "", "time": "",
            "notes": "Public request", "privacy_acknowledged": True,
        }, "public-archive-test")
        create_review_draft(req, "quote")
        work_id = create_internal_work_record(req)
        self.login()
        self.assertEqual(self.client.get("/hq/work/" + work_id + "/archive").status_code, 404)
        with self.assertRaises(LookupError):
            set_work_archived(work_id, "archive", 0, uuid.uuid4().hex)
        self.assertEqual(read_work_record(work_id)["source_request_id"], req)
        self.assertEqual(search_work_records()["total"], 2)

    def test_concurrent_archive_uses_one_canonical_revision(self):
        def worker():
            return set_work_archived(self.work_id, "archive", 0, uuid.uuid4().hex)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            results = []
            for f in futures:
                try:
                    results.append(f.result())
                except StaleWorkRecordError:
                    results.append("conflict")
        self.assertCountEqual(results, [1, "conflict"])
        self.assertEqual(len(archived_work_snapshot(self.work_id)["events"]), 1)


if __name__ == "__main__":
    unittest.main()
