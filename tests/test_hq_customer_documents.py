"""Synthetic customer document/quote PDF preview tests, no genuine records or messages."""
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
from hq_core_records import read_work_record, set_draft_financials, change_work_status
from hq_customer_documents import document_snapshot, save_public_description
from hq_native_work import create_native_work

ROOT = Path(__file__).resolve().parents[1]


class CustomerDocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.tmp.name,
            "HQ_SESSION_SECRET": "synthetic-document-secret",
            "GOOGLE_CLIENT_ID": "synthetic",
            "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "test-owner"),
                        csrf="test-csrf", google_sub="test-owner")

    def create_work(self):
        with app.app_context():
            return create_native_work({
                "operation_id": uuid.uuid4().hex, "customer_id": "",
                "vehicle_id": "", "customer_name": "Document Test Customer",
                "email": "doc@example.invalid", "phone": "07700 900012",
                "make": "Ford", "model": "Focus", "model_year": "2020",
                "registration": "AB20 AAA", "kind": "quote",
                "service": "Bodywork & Paint", "customer_tier": "retail",
                "notes": "PRIVATE COST AND SENSITIVE INTERNAL SCOPE",
                "preferred_date": "2026-12-01", "preferred_time": "10:00",
            })

    def post(self, work_id, revision, description, csrf="test-csrf"):
        return self.client.post(f"/hq/work/{work_id}/customer-document", data={
            "csrf": csrf, "revision": str(revision),
            "public_description": description,
        })

    def test_owner_document_save_reload_print_price_gate_and_audit_redaction(self):
        work_id = self.create_work()
        path = f"/hq/work/{work_id}/customer-document"
        self.assertEqual(self.client.get(path).status_code, 302)
        self.login()
        page = self.client.get(path)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"INTERNAL PREVIEW", page.data)
        self.assertNotIn(b"PRIVATE COST", page.data)
        self.assertNotIn(b"Print / Save as PDF", page.data)
        self.assertIn(b"not a confirmed appointment", page.data)
        self.assertEqual(self.post(work_id, 0, "Repair and repaint bonnet.").status_code, 303)
        # Independent new connection and request verify on-disk persistence.
        snapshot = document_snapshot(work_id)
        self.assertEqual(snapshot["public_description"], "Repair and repaint bonnet.")
        self.assertEqual(snapshot["revision"], 1)
        self.assertFalse(snapshot["can_print"])
        refreshed = self.client.get(path)
        self.assertIn(b"Repair and repaint bonnet.", refreshed.data)
        self.assertEqual(refreshed.headers["Cache-Control"], "private, no-store")
        self.assertEqual(self.post(work_id, 0, "Repair and repaint bonnet.").status_code, 303)
        self.assertEqual(read_work_record(work_id)["revision"], 1)
        with connect() as db:
            audit = db.execute("""SELECT action,before_value,after_value
                FROM hq_work_audit WHERE work_id=?""", (work_id,)).fetchone()
            self.assertEqual(audit["action"], "customer_document")
            self.assertNotIn("Repair", audit["before_value"] + audit["after_value"])
            self.assertEqual(db.execute("""SELECT COUNT(*) FROM hq_work_customer_documents
                WHERE work_id=?""", (work_id,)).fetchone()[0], 1)
        set_draft_financials(work_id, 25000, 5000, expected_revision=1)
        change_work_status(work_id, "ready_for_review", expected_revision=2)
        self.assertNotIn(b"Print / Save as PDF", self.client.get(path).data)
        change_work_status(work_id, "approved", expected_revision=3)
        page = self.client.get(path)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Print / Save as PDF", page.data)
        self.assertIn(b"250.00", page.data)
        self.assertNotIn(b"PRIVATE COST", page.data)
        self.assertNotIn(b"hourly", page.data.lower())
        self.assertNotIn(b"planned deposit", page.data.lower())
        self.assertTrue(document_snapshot(work_id)["can_print"])
        self.assertEqual(read_work_record(work_id)["kind"], "job")

    def test_stale_edits_csrf_validation_and_archived_work_lock(self):
        work_id = self.create_work()
        self.login()
        self.assertEqual(self.post(work_id, 0, "Draft A", "incorrect").status_code, 403)
        self.assertEqual(self.post(work_id, 0, "X" * 1201).status_code, 400)
        self.assertEqual(self.post(work_id, 0, "Draft A").status_code, 303)
        self.assertEqual(self.post(work_id, 0, "Draft B").status_code, 409)
        self.assertEqual(document_snapshot(work_id)["public_description"], "Draft A")
        self.assertEqual(self.post(work_id, 1, "Draft B").status_code, 303)
        self.assertEqual(document_snapshot(work_id)["revision"], 2)
        self.assertEqual(self.client.get("/hq/work/nope/customer-document").status_code, 404)
        self.assertEqual(self.client.get(f"/hq/work/{uuid.uuid4().hex}/customer-document").status_code, 404)

    def test_public_enquiry_uses_same_canonical_hq_document_not_publicly_reachable(self):
        with patch.dict(os.environ, {"HQ_PUBLIC_REQUESTS_ENABLED": "true"}):
            response = self.client.post("/api/public/customer-request", json={
                "kind": "quote", "name": "External synthetic customer",
                "email": "external@example.invalid", "phone": "07700 900013",
                "service": "Window Tinting", "make": "Ford", "model": "Fiesta",
                "year": "2021", "registration": "AB21 AAA",
                "notes": "PRIVATE PUBLIC ENQUIRY COMMENT",
                "privacy_acknowledged": True,
            })
        self.assertEqual(response.status_code, 201, response.json)
        self.assertFalse(response.json["confirmed_booking"])
        ref = response.json["reference"]
        self.login()
        headers = {"X-HQ-CSRF": "test-csrf"}
        draft = self.client.post("/api/hq/customer-requests/" + ref + "/draft",
            json={"draft_type": "quote"}, headers=headers)
        self.assertEqual(draft.status_code, 201, draft.json)
        created = self.client.post("/api/hq/customer-requests/" + ref + "/work-record",
            json={}, headers=headers)
        self.assertEqual(created.status_code, 201, created.json)
        work_id = created.json["id"]
        self.assertEqual(read_work_record(work_id)["source_request_id"], ref)
        path = f"/hq/work/{work_id}/customer-document"
        self.assertEqual(self.post(work_id, 0, "Private approved summary in preparation").status_code, 303)
        self.assertIn(b"Private approved summary in preparation", self.client.get(path).data)
        self.assertNotIn(b"PRIVATE PUBLIC ENQUIRY COMMENT", self.client.get(path).data)
        isolated = app.test_client()
        self.assertEqual(isolated.get(path).status_code, 302)
        self.assertEqual(isolated.post(path, data={"revision": "1", "public_description": "Hacked"}).status_code, 302)
        self.assertEqual(document_snapshot(work_id)["revision"], 1)
        self.assertFalse(document_snapshot(work_id)["can_print"])


if __name__ == "__main__":
    unittest.main()
