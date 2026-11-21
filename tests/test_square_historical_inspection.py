"""One-off Square GetPayment inspection is owner-only, read-only and non-persistent."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
import hq_square_payments as square


class SquareHistoricalInspectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name,
            "HQ_SQUARE_READ_TOKEN": "synthetic-read-only-token",
            "HQ_SQUARE_LOCATION_ID": "owned-location",
            "HQ_SESSION_SECRET": "test-session-only",
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

    def payment(self, ref="older-square-payment", location="owned-location"):
        created = datetime.now(timezone.utc) - timedelta(days=90)
        updated = datetime.now(timezone.utc) - timedelta(hours=1)
        return {
            "id": ref, "location_id": location, "status": "COMPLETED",
            "total_money": {"amount": 30000, "currency": "GBP"},
            "refunded_money": {"amount": 4500, "currency": "GBP"},
            "created_at": created.isoformat(), "updated_at": updated.isoformat(),
            "buyer_email_address": "private@example.invalid",
            "card_details": {"card": {"last_4": "1234"}},
        }

    def test_owner_can_inspect_older_payment_without_persistence_or_receipt_claim(self):
        self.owner()
        response = Mock()
        response.json.return_value = {"payment": self.payment()}
        with patch.object(square.requests, "get", return_value=response) as get:
            result = self.client.post("/hq/square-payment-evidence", data={
                "csrf": "owner-csrf", "action": "inspect-exact",
                "payment_id": "older-square-payment",
            })
        self.assertEqual(result.status_code, 200)
        body = result.get_data(as_text=True)
        self.assertIn("older-square-payment", body)
        self.assertIn("4500", body)
        self.assertIn("not bank verified", body)
        self.assertNotIn("private@example.invalid", body)
        self.assertNotIn("1234", body)
        self.assertEqual(get.call_args.args[0],
                         "https://connect.squareup.com/v2/payments/older-square-payment")
        self.assertNotIn("params", get.call_args.kwargs)
        self.assertIn("Inspect an older payment", self.client.get(
            "/hq/square-payment-evidence").get_data(as_text=True))
        self.assertFalse(square._path().exists())
        self.assertFalse((Path(os.environ["HQ_DATA_DIR"]) /
                          "integration_health.sqlite3").exists())

    def test_get_and_unauthenticated_or_bad_csrf_never_call_provider(self):
        with patch.object(square.requests, "get") as get:
            self.assertEqual(self.client.post("/hq/square-payment-evidence", data={
                "action": "inspect-exact", "payment_id": "older-square-payment",
            }).status_code, 302)
            self.owner()
            self.assertEqual(self.client.get("/hq/square-payment-evidence").status_code, 200)
            self.assertEqual(self.client.post("/hq/square-payment-evidence", data={
                "csrf": "wrong", "action": "inspect-exact",
                "payment_id": "older-square-payment",
            }).status_code, 403)
            get.assert_not_called()

    def test_invalid_id_rejects_provider_request_and_cannot_leak_input(self):
        self.owner()
        with patch.object(square.requests, "get") as get:
            for bad in ("../payment", "abcd?query=x", "x" * 193, "", "a b", "test%2Fid"):
                resp = self.client.post("/hq/square-payment-evidence", data={
                    "csrf": "owner-csrf", "action": "inspect-exact",
                    "payment_id": bad,
                })
                self.assertEqual(resp.status_code, 502)
                self.assertIn("Unable to inspect", resp.get_data(as_text=True))
            get.assert_not_called()
        self.assertFalse(square._path().exists())

    def test_mismatched_payment_or_seller_location_fails_closed(self):
        self.owner()
        for bad in (self.payment(ref="other-payment"),
                    self.payment(location="different-seller")):
            response = Mock()
            response.json.return_value = {"payment": bad}
            with patch.object(square.requests, "get", return_value=response):
                resp = self.client.post("/hq/square-payment-evidence", data={
                    "csrf": "owner-csrf", "action": "inspect-exact",
                    "payment_id": "older-square-payment",
                })
            self.assertEqual(resp.status_code, 502)
            self.assertNotIn("4500 minor units", resp.get_data(as_text=True))
        self.assertFalse(square._path().exists())

    def test_bad_provider_shape_or_status_cannot_advance_integration(self):
        self.owner()
        for payload in ({}, {"errors": [{"code": "UNAUTHORIZED"}]},
                        {"payment": {"id": "older-square-payment"}}):
            response = Mock()
            response.json.return_value = payload
            with patch.object(square.requests, "get", return_value=response):
                resp = self.client.post("/hq/square-payment-evidence", data={
                    "csrf": "owner-csrf", "action": "inspect-exact",
                    "payment_id": "older-square-payment",
                })
            self.assertEqual(resp.status_code, 502)
        self.assertFalse((Path(os.environ["HQ_DATA_DIR"]) /
                          "integration_health.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
