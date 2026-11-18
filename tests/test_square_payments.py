"""Square payment evidence must never be mistaken for verified HQ job receipts."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
import hq_square_payments as square


class SquarePaymentEvidenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name,
            "HQ_SQUARE_READ_TOKEN": "mock-read-only",
            "HQ_SQUARE_LOCATION_ID": "location-one",
            "HQ_SESSION_SECRET": "testing-only",
            "GOOGLE_CLIENT_ID": "test", "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()

    def owner(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess["hq_sid"] = issue_session(db, "test-owner")
            sess["csrf"] = "owner-csrf"

    def payment(self, status="COMPLETED", ref="square-pmt-01", refund=None):
        record = {"id": ref, "status": status, "location_id": "location-one",
                  "total_money": {"amount": 35000, "currency": "GBP"},
                  "created_at": "2026-11-17T11:00:00Z",
                  "updated_at": "2026-11-17T11:01:00Z",
                  "card_details": {"card": {"last_4": "9999"}},
                  "buyer_email_address": "private@example.invalid",
                  "reference_id": "untrusted-free-text"}
        if refund is not None:
            record["refunded_money"] = {"amount": refund, "currency": "GBP"}
        return record

    def page(self, items, cursor=None):
        return Mock(json=Mock(return_value={"payments": items, "cursor": cursor}))

    def test_complete_read_only_snapshot_minimises_data_without_job_matching(self):
        with patch.object(square.requests, "get", return_value=self.page([self.payment(refund=1000)])) as get:
            self.assertEqual(square.refresh_payments(), 1)
            self.assertEqual(square.refresh_payments(), 1)
        self.assertEqual(get.call_args.args[0], "https://connect.squareup.com/v2/payments")
        self.assertEqual(get.call_args.kwargs["params"]["location_id"], "location-one")
        self.assertEqual(get.call_args.kwargs["params"]["limit"], 100)
        self.assertEqual(get.call_args.kwargs["timeout"], (3, 7))
        self.assertEqual(get.call_args.kwargs["headers"]["Square-Version"], "2026-09-16")
        state = square.read_snapshot()
        self.assertEqual(state["payment_count"], 1)
        self.assertFalse(state["stale"])
        self.assertEqual(state["payments"][0]["total_minor"], 35000)
        self.assertEqual(state["payments"][0]["refunded_minor"], 1000)
        self.assertEqual(state["reconciliation_status"], "not_linked")
        self.assertIsNone(state["verified_job_receipts_pence"])
        self.assertIsNone(state["verified_job_outstanding_pence"])
        self.assertFalse(state["bank_settlement_verified"])
        self.assertFalse(state["automatic_job_matching"])
        with sqlite3.connect(square._path()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 1)
        contents = square._path().read_bytes()
        self.assertNotIn(b"private@example.invalid", contents)
        self.assertNotIn(b"9999", contents)
        self.assertNotIn(b"untrusted-free-text", contents)
        self.assertNotIn(b"mock-read-only", contents)

    def test_pagination_failure_preserves_previous_good_snapshot(self):
        with patch.object(square.requests, "get", return_value=self.page([self.payment()])):
            square.refresh_payments()
        with patch.object(square.requests, "get", side_effect=[
            self.page([self.payment(ref="second")], cursor="next"),
            RuntimeError("upstream failed"),
        ]):
            with self.assertRaises(RuntimeError):
                square.refresh_payments()
        state = square.read_snapshot()
        self.assertEqual(state["payment_count"], 1)
        self.assertEqual(state["payments"][0]["id"], "square-pmt-01")

    def test_truncated_duplicate_and_corrupt_provider_pages_fail_closed(self):
        bad = self.payment()
        bad["total_money"] = {"amount": -100, "currency": "GBP"}
        cases = [
            [self.page([bad])],
            [self.page([self.payment()], "next"), self.page([self.payment(ref="two")], "next")],
            [self.page([self.payment(), self.payment()])],
            [self.page([dict(self.payment(), location_id="other-location")])],
            [self.page([dict(self.payment(), updated_at="invalid")])],
            [self.page([self.payment(refund=35001)])],
        ]
        for responses in cases:
            with self.subTest(responses=len(responses)):
                with patch.object(square.requests, "get", side_effect=responses):
                    with self.assertRaises((ValueError, RuntimeError)):
                        square.refresh_payments()
                self.assertFalse(square._path().exists())

    def test_owner_auth_csrf_and_explicit_refresh_only(self):
        url = "/hq/square-payment-evidence"
        api = "/api/hq/square-payment-evidence"
        self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.client.get(api).status_code, 302)
        self.owner()
        with patch.object(square.requests, "get") as get:
            self.assertEqual(self.client.get(url).status_code, 200)
            self.assertEqual(self.client.get(api).status_code, 200)
            get.assert_not_called()
        self.assertEqual(self.client.post(url).status_code, 403)
        with patch.object(square.requests, "get", return_value=self.page([self.payment()])):
            response = self.client.post(url, data={"csrf": "owner-csrf"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn(b"Square payment evidence", response.data)
        self.assertIn(b"not confirmed bank deposits", response.data)
        data = self.client.get(api).get_json()
        self.assertEqual(data["payment_count"], 1)
        self.assertNotIn("customer", self.client.get(api).get_data(as_text=True).lower())
        self.assertNotIn("card_details", self.client.get(api).get_data(as_text=True))
        health = self.client.get("/api/hq/integrations").get_json()
        square_health = next(item for item in health["integrations"] if item["name"] == "Square payments")
        self.assertEqual(square_health["status"], "Last read succeeded")

    def test_storage_commit_failure_sets_sync_warning_not_verified_receipt(self):
        from hq_integration_health import PersistedSyncError, record_check
        self.owner()
        record_check("square_payments")
        record_check("square_payments_sync")
        with patch.object(square, "refresh_payments",
                          side_effect=PersistedSyncError("Fixture disk full")):
            response = self.client.post("/hq/square-payment-evidence",
                                        data={"csrf": "owner-csrf"})
        self.assertEqual(response.status_code, 502)
        row = next(item for item in self.client.get("/api/hq/integrations")
                   .get_json()["readiness"] if item["key"] == "square_payments")
        self.assertTrue(row["sync_needs_attention"])
        self.assertTrue(row["sync_tested"])  # historical test, not current success
        self.assertEqual(row["blocker"], "Square payment snapshot commit failed")
        self.assertFalse(row["reconciled"])

    def test_storage_error_is_classified_after_successful_provider_read(self):
        from hq_integration_health import PersistedSyncError
        with patch.object(square.requests, "get",
                          return_value=self.page([self.payment()])), patch.object(
                              square.sqlite3, "connect",
                              side_effect=sqlite3.OperationalError("fixture disk error")):
            with self.assertRaises(PersistedSyncError):
                square.refresh_payments()

    def test_no_credentials_means_no_verified_zero_or_automatic_fetch(self):
        self.owner()
        with patch.dict(os.environ, {"HQ_SQUARE_READ_TOKEN": ""}):
            data = self.client.get("/api/hq/square-payment-evidence").get_json()
            self.assertFalse(data["configured"])
            self.assertFalse(data["snapshot_available"])
            self.assertIsNone(data["payment_count"])
            self.assertIsNone(data["verified_job_receipts_pence"])
            self.assertEqual(self.client.post("/hq/square-payment-evidence",
                             data={"csrf": "owner-csrf"}).status_code, 503)
            self.assertFalse(square._path().exists())


if __name__ == "__main__":
    unittest.main()
