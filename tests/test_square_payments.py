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

    def test_payment_pagination_rejects_oversized_pages_and_ambiguous_cursors(self):
        # Never replace a previously complete, source-scoped snapshot merely
        # because the provider responds with an ambiguous second-page token.
        with patch.object(square.requests, "get",
                          return_value=self.page([self.payment()])):
            self.assertEqual(square.refresh_payments(), 1)
        bad_pages = [
            {"payments": [self.payment(ref=f"p-{i}") for i in range(101)]},
            {"payments": [self.payment(ref="replacement")], "cursor": ""},
            {"payments": [self.payment(ref="replacement")], "cursor": " "},
            {"payments": [self.payment(ref="replacement")], "cursor": "next page"},
            {"payments": [self.payment(ref="replacement")], "cursor": chr(10)},
            {"payments": [self.payment(ref="replacement")], "cursor": chr(9731)},
            {"payments": [self.payment(ref="replacement")], "cursor": "a" * 2001},
            {"payments": [self.payment(ref="replacement")], "cursor": 123},
        ]
        for bad_page in bad_pages:
            with self.subTest(cursor=repr(bad_page.get("cursor")),
                              count=len(bad_page["payments"])):
                with patch.object(square.requests, "get", side_effect=[
                        Mock(json=Mock(return_value=bad_page)),
                        self.page([self.payment(ref="unexpected")])]) as provider:
                    with self.assertRaises(ValueError):
                        square.refresh_payments()
                    self.assertEqual(provider.call_count, 1)
                snapshot = square.read_snapshot()
                self.assertEqual(snapshot["payment_count"], 1)
                self.assertEqual(snapshot["payments"][0]["id"], "square-pmt-01")
                self.assertFalse(snapshot["bank_settlement_verified"])

    def test_valid_two_page_payment_refresh_is_bounded_and_not_reconciled(self):
        with patch.object(square.requests, "get", side_effect=[
                self.page([self.payment(ref="first")], cursor="next_123"),
                self.page([self.payment(ref="second")])]) as provider:
            self.assertEqual(square.refresh_payments(), 2)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(provider.call_args.kwargs["params"]["cursor"], "next_123")
        snapshot = square.read_snapshot()
        self.assertEqual(snapshot["payment_count"], 2)
        self.assertEqual({p["id"] for p in snapshot["payments"]}, {"first", "second"})
        self.assertFalse(snapshot["bank_settlement_verified"])
        self.assertIsNone(snapshot["verified_job_receipts_pence"])

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
        from hq_integration_health import PersistedSyncError, record_committed_sync
        self.owner()
        self.assertTrue(record_committed_sync("square_payments", source_location="location-one"))
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


    def test_cached_square_evidence_is_hidden_on_location_or_access_change(self):
        # An existing /data snapshot may belong to a previous configured
        # seller/location. Do not show its amounts or payment identifiers to
        # the currently configured owner until the intended scope is restored.
        with patch.object(square.requests, "get",
                          return_value=self.page([self.payment(refund=1200)])):
            self.assertEqual(square.refresh_payments(), 1)
        self.owner()
        self.assertEqual(square.read_snapshot()["payment_count"], 1)
        for overrides in (
            {"HQ_SQUARE_LOCATION_ID": "another-location"},
            {"HQ_SQUARE_READ_TOKEN": ""},
            {"HQ_SQUARE_LOCATION_ID": "locatiön-one"},
        ):
            with self.subTest(overrides=overrides), patch.dict(os.environ, overrides):
                with patch.object(square.requests, "get") as provider:
                    state = square.read_snapshot()
                    self.assertFalse(state["snapshot_available"])
                    self.assertTrue(state["scope_unverified"])
                    self.assertTrue(state["stale"])
                    self.assertEqual(state["payments"], [])
                    for field in ("payment_count", "last_refreshed_at",
                                  "window_start", "window_end",
                                  "verified_job_receipts_pence"):
                        self.assertIsNone(state[field])
                    api = self.client.get("/api/hq/square-payment-evidence")
                    page = self.client.get("/hq/square-payment-evidence")
                    self.assertEqual(api.status_code, 200)
                    self.assertEqual(page.status_code, 200)
                    self.assertTrue(api.json["scope_unverified"])
                    self.assertIn(b"evidence is withheld", page.data)
                    for response in (api, page):
                        self.assertNotIn(b"square-pmt-01", response.data)
                        self.assertNotIn(b"35000", response.data)
                    provider.assert_not_called()
        # Scope switches never delete or modify the previous good snapshot.
        self.assertFalse(square.read_snapshot()["scope_unverified"])
        self.assertEqual(square.read_snapshot()["payments"][0]["id"],
                         "square-pmt-01")
        with sqlite3.connect(square._path()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
