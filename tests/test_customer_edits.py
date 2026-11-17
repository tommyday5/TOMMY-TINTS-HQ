import os
import tempfile
import unittest
from unittest.mock import patch

from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import create_test_job, list_work_records


class CustomerEditTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, HQ_DATA_DIR=directory.name, HQ_SESSION_SECRET="test",
                         GOOGLE_CLIENT_ID="test", GOOGLE_CLIENT_SECRET="test",
                         HQ_ALLOWED_GOOGLE_EMAIL="owner@example.invalid", HQ_GOOGLE_MAPS_BROWSER_KEY="")
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()
        self.form = {"csrf": "token", "name": "Updated customer", "email": "client@example.invalid",
                     "phone": "07700 900123", "address": "10 Example Street", "postcode": "rm1 1aa"}

    def sign_in(self):
        with self.client.session_transaction() as session, auth_db() as db:
            session.update(hq_sid=issue_session(db, "test-owner"), csrf="token")

    def test_edit_updates_linked_work_record_and_persists_address(self):
        work = create_test_job()
        customer = list_work_records()[0]["customer_id"]
        path = "/hq/customers/" + customer + "/edit"
        self.assertEqual(self.client.get(path).status_code, 302)
        self.sign_in()
        self.assertIn(b"Google address suggestions are unavailable", self.client.get(path).data)
        with patch.dict(os.environ, HQ_GOOGLE_MAPS_BROWSER_KEY="restricted-browser-key"):
            self.assertIn(b'id="findAddress"', self.client.get(path).data)
        self.assertEqual(self.client.post(path, data={**self.form, "csrf": "bad"}).status_code, 403)
        self.assertEqual(self.client.post(path, data={**self.form, "email": "bad"}).status_code, 400)
        result = self.client.post(path, data=self.form, follow_redirects=True)
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"Customer details saved", result.data)
        self.assertIn(b"10 Example Street", result.data)
        self.assertIn(b"RM1 1AA", result.data)
        row, = list_work_records()
        self.assertEqual((row["id"], row["name"], row["email"]), (work, "Updated customer", "client@example.invalid"))

    def test_maintenance_edit_is_used_by_route_planner(self):
        self.sign_in()
        source = {"clients": [{"Client ID": "M-1", "Customer": "Sheet name", "Postcode": "RM1 1AA"}], "visits": []}
        with patch.dict(app.extensions, hq_sheets_connection=lambda: ("token", 9999999999)), patch(
                "maintenance_visual.maintenance_data", return_value=source):
            path = "/hq/maintenance/customer/edit?id=M-1"
            self.assertIn(b"Sheet name", self.client.get(path).data)
            self.assertEqual(self.client.post("/hq/maintenance/customer/edit?id=unknown", data=self.form).status_code, 404)
            self.assertEqual(self.client.post(path, data=self.form).status_code, 303)
            customer, = self.client.get("/api/hq/maintenance/visual-route").json["customers"]
            self.assertEqual((customer["name"], customer["address"]), ("Updated customer", "10 Example Street, RM1 1AA"))
            self.assertEqual(source["clients"][0]["Customer"], "Sheet name")
