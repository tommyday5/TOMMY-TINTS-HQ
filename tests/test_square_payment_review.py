"""Regression tests for owner-only, non-mutating exact payment/job comparisons."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import app
from customer_request_store import create_request, create_review_draft
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import (create_internal_work_record, create_test_job,
                             change_work_status, read_work_record, set_draft_financials)
import hq_square_payments as square
from hq_payment_review import compare_references


class PaymentReviewTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(root.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": root.name, "HQ_SQUARE_READ_TOKEN": "read-token",
            "HQ_SQUARE_LOCATION_ID": "location-one",
            "HQ_SESSION_SECRET": "test-secret",
            "GOOGLE_CLIENT_ID": "test", "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()

    def owner(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "owner"), csrf="csrf")

    def job(self, price=35000, deposit=7600, approved=True):
        payload = dict(
            kind="quote", name="Private Customer", email="private@example.invalid",
            phone="07123456789", service="Window Tinting",
            make="Ford", model="Focus", year="2020", registration="XX20ABC",
            date="", time="", notes="Private request", privacy_acknowledged=True,
        )
        request_id = create_request(payload, "test-fingerprint")
        create_review_draft(request_id, "quote")
        work_id = create_internal_work_record(request_id)
        set_draft_financials(work_id, price, deposit, expected_revision=0)
        if approved:
            change_work_status(work_id, "ready_for_review", expected_revision=1)
            change_work_status(work_id, "approved", expected_revision=2)
        return work_id

    def seed(self, amount=7600, status="COMPLETED", refund=None, payment_id="square-01"):
        created = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=2)
        payment = {
            "id": payment_id, "location_id": "location-one", "status": status,
            "total_money": {"amount": amount, "currency": "GBP"},
            "created_at": created.isoformat(),
            "updated_at": (created + timedelta(minutes=5)).isoformat(),
            "buyer_email_address": "private@example.invalid",
            "card_details": {"card": {"last_4": "1234"}},
        }
        if refund is not None:
            payment["refunded_money"] = {"amount": refund, "currency": "GBP"}
        response = Mock(json=Mock(return_value={"payments": [payment]}))
        with patch.object(square.requests, "get", return_value=response):
            square.refresh_payments()

    def api(self, work_id, payment_id="square-01", csrf="csrf"):
        return self.client.post("/api/hq/square-payment-review",
                                json={"work_id": work_id, "payment_id": payment_id},
                                headers={"X-HQ-CSRF": csrf})

    def test_exact_deposit_comparison_never_confirms_receipt_or_mutates_data(self):
        job_id = self.job()
        self.seed()
        self.owner()
        with patch.object(square.requests, "get") as upstream:
            result = self.api(job_id)
            upstream.assert_not_called()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        data = result.get_json()
        self.assertTrue(data["payment_evidence_found"])
        self.assertTrue(data["snapshot_current"])
        self.assertTrue(data["matches_planned_deposit"])
        self.assertFalse(data["matches_approved_price"])
        self.assertTrue(data["candidate_evidence_complete"])
        for key in ("payment_verified_for_job", "bank_settlement_verified",
                    "job_marked_paid"):
            self.assertFalse(data[key])
        self.assertIsNone(data["verified_job_receipts_pence"])
        self.assertIsNone(data["verified_job_outstanding_pence"])
        self.assertNotIn("private@example.invalid", result.get_data(as_text=True))
        self.assertNotIn("1234", result.get_data(as_text=True))
        self.assertNotIn("registration", result.get_data(as_text=True))
        self.assertEqual(read_work_record(job_id)["status"], "approved")
        with sqlite3.connect(square._path()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 1)
        self.assertFalse(any("match" in x.name.lower() for x in Path(os.environ["HQ_DATA_DIR"]).iterdir()))

    def test_exact_full_price_and_mismatch_are_only_candidates(self):
        job_id = self.job()
        self.seed(amount=35000)
        data = compare_references(job_id, "square-01")
        self.assertTrue(data["matches_approved_price"])
        self.assertFalse(data["matches_planned_deposit"])
        self.assertFalse(data["payment_verified_for_job"])
        self.seed(amount=9000)
        mismatch = compare_references(job_id, "square-01")
        self.assertFalse(mismatch["candidate_evidence_complete"])
        self.assertIn("Amount does not equal", " ".join(mismatch["warnings"]))

    def test_noncompleted_refunded_unapproved_synthetic_and_missing(self):
        job_id = self.job()
        self.seed(status="PENDING")
        self.assertFalse(compare_references(job_id, "square-01")["candidate_evidence_complete"])
        self.seed(refund=1000)
        refunded = compare_references(job_id, "square-01")
        self.assertFalse(refunded["candidate_evidence_complete"])
        self.assertIn("refund", " ".join(refunded["warnings"]).lower())
        missing = compare_references(job_id, "different-reference")
        self.assertFalse(missing["payment_evidence_found"])
        self.assertIsNone(missing["verified_job_receipts_pence"])
        self.assertIn("not proof of non-payment", " ".join(missing["warnings"]))
        draft = self.job(approved=False)
        self.assertFalse(compare_references(draft, "square-01")["candidate_evidence_complete"])
        fake = create_test_job()
        set_draft_financials(fake, 1000, 500, expected_revision=0)
        change_work_status(fake, "ready_for_review", expected_revision=1)
        change_work_status(fake, "approved", expected_revision=2)
        self.assertFalse(compare_references(fake, "square-01")["candidate_evidence_complete"])
        self.assertIn("TEST jobs", " ".join(compare_references(fake, "square-01")["warnings"]))

    def test_outdated_or_wrong_location_snapshot_is_not_current(self):
        job_id = self.job()
        self.seed()
        with sqlite3.connect(square._path()) as db:
            db.execute("UPDATE snapshot SET refreshed_at=?",
                       ((datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),))
        self.assertFalse(compare_references(job_id, "square-01")["snapshot_current"])
        self.seed()
        self.owner()
        for current_location in ("another-location", "locatiön-one"):
            with self.subTest(location=current_location), patch.dict(
                    os.environ, {"HQ_SQUARE_LOCATION_ID": current_location}):
                data = compare_references(job_id, "square-01")
                self.assertFalse(data["snapshot_current"])
                self.assertFalse(data["candidate_evidence_complete"])
                self.assertFalse(data["payment_evidence_found"])
                self.assertIsNone(data["provider_amount_pence"])
                self.assertIsNone(data["reported_refund_pence"])
                self.assertIsNone(data["provider_status"])
                self.assertFalse(data["matches_planned_deposit"])
                self.assertIn("location has changed", " ".join(data["warnings"]))
                # Both owner-only review entrypoints must withhold the old
                # provider evidence, not merely show a wrong-location warning.
                response = self.api(job_id)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.json["payment_evidence_found"])
                self.assertIsNone(response.json["provider_amount_pence"])
                html = self.client.post("/hq/square-payment-review", data={
                    "csrf": "csrf", "work_id": job_id, "payment_id": "square-01",
                })
                self.assertEqual(html.status_code, 200)
                self.assertIn(b"location has changed", html.data)
                self.assertNotIn(b"<dd>COMPLETED</dd>", html.data)
        # The original snapshot is preserved and remains reviewable once
        # the owner restores the same configured location.
        restored = compare_references(job_id, "square-01")
        self.assertTrue(restored["payment_evidence_found"])
        self.assertEqual(restored["provider_amount_pence"], 7600)

    def test_local_snapshot_integrity_failure_never_returns_matching_receipt_candidate(self):
        job_id = self.job()
        self.seed()
        self.owner()
        with sqlite3.connect(square._path()) as db:
            db.execute("UPDATE snapshot SET item_count=2")
        with patch.object(square.requests, "get") as upstream:
            result = compare_references(job_id, "square-01")
            response = self.api(job_id)
            upstream.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(result["snapshot_current"])
        self.assertFalse(result["payment_evidence_found"])
        self.assertIsNone(result["provider_amount_pence"])
        self.assertFalse(result["matches_planned_deposit"])
        self.assertFalse(result["candidate_evidence_complete"])
        self.assertFalse(result["bank_settlement_verified"])
        self.assertIn("row coverage cannot be verified", " ".join(result["warnings"]))
        self.assertFalse(response.json["payment_evidence_found"])
        self.assertIsNone(response.json["provider_amount_pence"])
        self.assertFalse(response.json["job_marked_paid"])

    def test_invalid_cached_row_cannot_be_offered_as_job_payment_candidate(self):
        job_id = self.job()
        self.seed()
        self.owner()
        with sqlite3.connect(square._path()) as db:
            # The count and payment ID still match; only the signed financial
            # evidence is contradictory. Withhold the entire local snapshot.
            db.execute("UPDATE payments SET refunded_minor=9000 "
                       "WHERE id='square-01'")
        with patch.object(square.requests, "get") as provider:
            result = compare_references(job_id, "square-01")
            api = self.api(job_id)
            provider.assert_not_called()
        for evidence in (result, api.json):
            self.assertFalse(evidence["snapshot_current"])
            self.assertFalse(evidence["payment_evidence_found"])
            self.assertFalse(evidence["matches_planned_deposit"])
            self.assertFalse(evidence["candidate_evidence_complete"])
            self.assertIsNone(evidence["provider_amount_pence"])
            self.assertIsNone(evidence["reported_refund_pence"])
            self.assertFalse(evidence["bank_settlement_verified"])
            self.assertIsNone(evidence["verified_job_receipts_pence"])
        self.assertIn("row coverage cannot be verified",
                      " ".join(result["warnings"]))

    def test_auth_csrf_reference_validation_and_error_handling(self):
        job_id = self.job()
        self.seed()
        self.assertEqual(self.api(job_id).status_code, 302)
        self.assertEqual(self.client.get("/hq/square-payment-review").status_code, 302)
        self.owner()
        self.assertEqual(self.api(job_id, csrf="wrong").status_code, 403)
        self.assertEqual(self.client.post("/hq/square-payment-review", data={
            "csrf": "wrong", "work_id": job_id, "payment_id": "square-01"
        }).status_code, 403)
        self.assertEqual(self.api("not-a-job").status_code, 400)
        self.assertEqual(self.api(job_id, "' OR 1=1 --").status_code, 400)
        self.assertEqual(self.api("f"*32).status_code, 404)
        page = self.client.get("/hq/square-payment-review")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Exact reference review", page.data)
        posted = self.client.post("/hq/square-payment-review", data={
            "csrf": "csrf", "work_id": job_id, "payment_id": "square-01",
        })
        self.assertEqual(posted.status_code, 200)
        self.assertIn(b"Review result", posted.data)
        self.assertNotIn(b"Private Customer", posted.data)
        self.assertNotIn(b"XX20ABC", posted.data)
        self.assertEqual(posted.headers["Cache-Control"], "no-store")

    def test_no_credentials_or_snapshot_means_unknown_not_zero(self):
        job_id = self.job()
        missing = compare_references(job_id, "square-01")
        self.assertFalse(missing["payment_evidence_found"])
        self.assertFalse(missing["candidate_evidence_complete"])
        self.assertIsNone(missing["verified_job_receipts_pence"])
        self.seed()
        with patch.dict(os.environ, {"HQ_SQUARE_READ_TOKEN": ""}):
            inaccessible = compare_references(job_id, "square-01")
            self.assertFalse(inaccessible["candidate_evidence_complete"])
            self.assertFalse(inaccessible["snapshot_current"])


if __name__ == "__main__":
    unittest.main()
