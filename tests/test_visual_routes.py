import os
from datetime import date
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from maintenance_visual import calculate_route, route_customers


class VisualRouteTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        env = patch.dict(os.environ, HQ_DATA_DIR=root.name, HQ_SESSION_SECRET="test",
                         GOOGLE_CLIENT_ID="test", GOOGLE_CLIENT_SECRET="test",
                         HQ_ALLOWED_GOOGLE_EMAIL="owner@example.invalid",
                         HQ_GOOGLE_MAPS_API_KEY="", HQ_GOOGLE_MAPS_BROWSER_KEY="",
                         HQ_ROUTE_BASE_ADDRESS="")
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()
        self.data = {"clients": [
            {"Client ID": "A", "Customer": "One", "Address": "1 Sample Street",
             "Postcode": "AB1 2CD", "Next Due": "17/11/2026", "Selected Visit £": "£80"},
            {"Client ID": "B", "Customer": "Two", "Address": "2 Sample Street",
             "Postcode": "AB1 2CD", "Next Due": "14/11/2026", "Normal Visit £": "100"},
            {"Client ID": "C", "Postcode": "AB1 2CD", "Next Due": "14/11/2026"},
        ], "visits": [{"Client ID": "A", "Visit Date": "17/11/2026", "Visit Status": "completed"}]}

    def sign_in(self):
        with self.client.session_transaction() as session, auth_db() as db:
            session["hq_sid"] = issue_session(db, "owner")
            session["csrf"] = "test"

    def test_status_labels_and_no_postcode_only_address(self):
        customers = route_customers(self.data, date(2026, 11, 17))
        self.assertEqual(customers["A"]["status"], "completed")
        self.assertEqual(customers["B"]["status"], "overdue")
        self.assertEqual(customers["C"]["address"], "")
        self.assertIsNone(customers["C"]["price"])

    def test_verified_geometry_and_order_use_provider_value(self):
        customers = route_customers(self.data)
        route = {"distanceMeters": 16093, "duration": "1800s",
                 "polyline": {"encodedPolyline": "provider-fixture"}, "optimizedIntermediateWaypointIndex": [1, 0]}
        with patch("maintenance_visual.requests.post", return_value=Mock(json=Mock(return_value={"routes": [route]}))) as request:
            result = calculate_route(customers, ["A", "B"], True, "Workshop", "test")
            self.assertEqual(result["stop_ids"], ["B", "A"])
            self.assertEqual(result["polyline"], "provider-fixture")
            self.assertEqual((result["miles"], result["minutes"], result["revenue"]), ("10.0", "30.0", "180.00"))
            self.assertTrue(request.call_args.kwargs["json"]["optimizeWaypointOrder"])
            self.assertEqual(calculate_route(customers, ["A", "B"], False, "Workshop", "test")["stop_ids"], ["A", "B"])
            route["optimizedIntermediateWaypointIndex"] = [1, 1]
            with self.assertRaises(RuntimeError):
                calculate_route(customers, ["A", "B"], True, "Workshop", "test")
        with self.assertRaises(ValueError):
            calculate_route(customers, ["C"], True, "Workshop", "test")

    def test_owner_only_and_clear_unconfigured_state(self):
        page = "/hq/maintenance/routes"
        api = "/api/hq/maintenance/visual-route"
        self.assertEqual(self.client.get(page).status_code, 302)
        self.assertEqual(self.client.get(api).status_code, 302)
        self.sign_in()
        result = self.client.get(page)
        self.assertIn(b"Map unavailable", result.data)
        self.assertIn(b"Driving estimates unavailable", result.data)
        with patch("maintenance_visual.requests.post") as external:
            self.assertEqual(self.client.post(api, json={}).status_code, 403)
            self.assertEqual(self.client.post(api, json={}, headers={"X-HQ-CSRF": "test"}).status_code, 503)
            external.assert_not_called()
