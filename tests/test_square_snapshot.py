import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from hq_auth import issue_session
from hq_auth_store import auth_db
from app import app
import hq_square_bookings as square


class SquareSnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, HQ_DATA_DIR=directory.name, HQ_SQUARE_READ_TOKEN="test",
                         HQ_SQUARE_LOCATION_ID="location", HQ_SESSION_SECRET="test",
                         GOOGLE_CLIENT_ID="test", GOOGLE_CLIENT_SECRET="test",
                         HQ_ALLOWED_GOOGLE_EMAIL="owner@example.invalid")
        env.start()
        self.addCleanup(env.stop)

    def response(self, version, status, cursor=None):
        return Mock(json=Mock(return_value={"bookings": [{
            "id": "booking", "status": status, "version": version,
            "location_id": "location", "start_at": "2099-07-10T10:00:00+01:00",
            "customer_note": "private",
        }], "cursor": cursor}))

    def test_replay_and_out_of_order_versions_preserve_cancellation(self):
        with patch.object(square.requests, "get", side_effect=[
            self.response(3, "CANCELLED_BY_SELLER"), self.response(3, "CANCELLED_BY_SELLER"),
            self.response(2, "ACCEPTED"),
        ]) as get:
            self.assertEqual([square.refresh_bookings() for _ in range(3)], [1, 0, 0])
        self.assertEqual(get.call_args.args[0], "https://connect.squareup.com/v2/bookings")
        row, = square.upcoming_bookings()
        self.assertEqual(dict(row), {"id": "booking", "status": "CANCELLED_BY_SELLER",
                                     "start_at": "2099-07-10T09:00:00+00:00"})

    def test_pagination_and_failure_do_not_write_a_partial_snapshot(self):
        with patch.object(square.requests, "get", side_effect=[
            self.response(3, "ACCEPTED", "next"), RuntimeError("failed page"),
        ]) as get:
            with self.assertRaises(RuntimeError):
                square.refresh_bookings()
        self.assertEqual(get.call_args.kwargs["params"]["cursor"], "next")
        self.assertFalse(square._path().exists())

    def test_provider_errors_leave_existing_snapshot_untouched(self):
        error = Mock(json=Mock(return_value={"errors": [{"code": "ERROR"}]}))
        with patch.object(square.requests, "get", side_effect=[self.response(3, "ACCEPTED"), error]):
            square.refresh_bookings()
            with self.assertRaises(RuntimeError):
                square.refresh_bookings()
        with sqlite3.connect(square._path()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0], 1)

    def test_owner_refresh_requires_csrf_and_success_updates_health(self):
        path = "/hq/square-bookings"
        with app.test_client() as client:
            self.assertEqual(client.get(path).status_code, 302)
            with client.session_transaction() as session:
                with auth_db() as db:
                    session["hq_sid"] = issue_session(db, "test-owner")
                session.update(csrf="csrf")
            self.assertEqual(client.post(path).status_code, 403)
            with patch.object(square.requests, "get", return_value=self.response(1, "ACCEPTED")):
                result = client.post(path, data={"csrf": "csrf"})
            self.assertEqual(result.status_code, 200)
            self.assertIn(b"ACCEPTED", result.data)
            health = client.get("/api/hq/integrations").json["integrations"]
            status = next(item for item in health if item["name"] == "Square")
            self.assertEqual(status["status"], "Last read succeeded")
