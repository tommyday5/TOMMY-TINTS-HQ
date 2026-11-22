"""Synthetic-only coverage of native maintenance visits and customer visibility."""
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
from hq_core_records import _schema
from hq_native_maintenance import (save_visit, archive_visit, visit_snapshot,
                                   visit_index)

ROOT = Path(__file__).resolve().parents[1]


class NativeMaintenanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": temp.name, "HQ_SESSION_SECRET": "synthetic-maintenance",
            "GOOGLE_CLIENT_ID": "fixture", "GOOGLE_CLIENT_SECRET": "fixture",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner = app.test_client()
        self.public = app.test_client()
        self.customer_a, self.vehicle_a = uuid.uuid4().hex, uuid.uuid4().hex
        self.customer_b, self.vehicle_b = uuid.uuid4().hex, uuid.uuid4().hex
        with connect() as db:
            _schema(db)
            now = "2026-11-22T00:00:00+00:00"
            for customer, vehicle, name in (
                (self.customer_a, self.vehicle_a, "Synthetic One"),
                (self.customer_b, self.vehicle_b, "Synthetic Two"),
            ):
                db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                           (customer, None, name, "fixture@example.invalid", "07700 900000", now))
                db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                           (vehicle, customer, "Ford", "Fiesta", "2020", "TEST ONLY", now))

    def login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="fixture-csrf", google_sub="synthetic-owner")

    def details(self, **updates):
        value = {
            "operation_id": uuid.uuid4().hex,
            "customer_id": self.customer_a, "vehicle_id": self.vehicle_a,
            "service": "Synthetic maintenance valet", "visit_day": "2026-12-10",
            "visit_time": "11:00", "status": "planned", "planned_price": "",
            "actual_minutes": "", "internal_notes": "PRIVATE MARGIN 99",
        }
        value.update(updates)
        return value

    def test_owner_create_edit_save_reload_and_audit_with_no_receipt(self):
        self.assertEqual(self.owner.get("/hq/maintenance/visits").status_code, 302)
        self.login()
        self.assertIn(b"Create maintenance visit",
                      self.owner.get("/hq/maintenance/visits").data)
        value = self.details()
        created = self.owner.post("/hq/maintenance/visits/new", data={
            **value, "csrf": "fixture-csrf",
        })
        self.assertEqual(created.status_code, 303, created.get_data(as_text=True))
        ref = created.headers["Location"].split("/")[-1].split("?")[0]
        self.assertEqual(len(ref), 32)
        self.assertEqual(visit_snapshot(ref)["revision"], 0)
        self.assertIsNone(visit_snapshot(ref)["planned_price_pence"])
        self.assertEqual(save_visit(value), ref)  # safe network replay
        self.assertIn(b"Synthetic maintenance valet",
                      self.owner.get("/hq/maintenance/visits/" + ref).data)
        self.assertEqual(self.owner.post("/hq/maintenance/visits/new", data={
            **self.details(), "csrf": "fixture-csrf",
        }).status_code, 400)  # same active customer/vehicle/time collision
        edit = self.owner.post("/hq/maintenance/visits/" + ref, data={
            **self.details(status="completed", planned_price="80.00",
                           actual_minutes="62", portal_visible="1"),
            "csrf": "fixture-csrf", "revision": "0",
        })
        self.assertEqual(edit.status_code, 303, edit.get_data(as_text=True))
        updated = visit_snapshot(ref)
        self.assertEqual(updated["revision"], 1)
        self.assertEqual(updated["planned_price_pence"], 8000)
        self.assertEqual(updated["actual_minutes"], 62)
        self.assertEqual(updated["portal_visible"], 1)
        self.assertEqual(self.owner.post("/hq/maintenance/visits/" + ref, data={
            **self.details(status="cancelled"), "csrf": "fixture-csrf", "revision": "0",
        }).status_code, 409)
        with connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_maintenance_visit_audit WHERE visit_id=?", (ref,)
            ).fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)
        self.assertEqual(visit_snapshot(ref)["status"], "completed")
        self.assertEqual(len(visit_index()), 1)

    def test_invalid_link_csrf_and_concurrent_archive_restore_collision(self):
        self.login()
        value = self.details(vehicle_id=self.vehicle_b)
        self.assertEqual(self.owner.post("/hq/maintenance/visits/new", data={
            **value, "csrf": "fixture-csrf",
        }).status_code, 400)
        self.assertEqual(self.owner.post("/hq/maintenance/visits/new", data={
            **self.details(), "csrf": "bad",
        }).status_code, 403)
        ref = save_visit(self.details(), actor="fixture-owner")
        self.assertEqual(self.owner.post("/hq/maintenance/visits/" + ref, data={
            "csrf": "fixture-csrf", "action": "archive", "revision": "0",
        }).status_code, 303)
        self.assertEqual(visit_snapshot(ref)["archived"], 1)
        second = save_visit(self.details(), actor="fixture-owner")
        self.assertNotEqual(ref, second)
        self.assertEqual(self.owner.post("/hq/maintenance/visits/" + ref, data={
            "csrf": "fixture-csrf", "action": "restore", "revision": "1",
        }).status_code, 409)
        self.assertEqual(visit_snapshot(ref)["archived"], 1)
        self.assertEqual(self.owner.get("/hq/maintenance/visits?archived=1").status_code, 200)

    def test_customer_only_sees_explicitly_shared_own_fields(self):
        one = save_visit(self.details(portal_visible="1", planned_price="150.00"))
        two = save_visit(self.details(customer_id=self.customer_b, vehicle_id=self.vehicle_b,
                                      visit_time="12:00", portal_visible="1",
                                      internal_notes="SECOND CUSTOMER SECRET"))
        hidden = save_visit(self.details(visit_time="13:00"))
        self.assertEqual(self.public.get("/customer/maintenance").status_code, 302)
        with patch("hq_native_maintenance.active_customer", return_value=self.customer_a):
            page = self.public.get("/customer/maintenance")
        self.assertEqual(page.status_code, 200)
        self.assertIn(one.encode(), page.data)
        self.assertNotIn(two.encode(), page.data)
        self.assertNotIn(hidden.encode(), page.data)
        self.assertNotIn(b"PRIVATE MARGIN", page.data)
        self.assertNotIn(b"150.00", page.data)
        self.assertIn(b"provisional", page.data)
        self.assertEqual(page.headers["Cache-Control"], "private, no-store")
        with patch("hq_native_maintenance.active_customer", return_value=self.customer_b):
            other = self.public.get("/customer/maintenance")
        self.assertIn(two.encode(), other.data)
        self.assertNotIn(one.encode(), other.data)

    def test_price_precision_and_fields_are_validated(self):
        for bad in ("3.001", "-1", "NaN", "1e900", "100001"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                save_visit(self.details(planned_price=bad))
        for bad in ("09:60", "25:00", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                save_visit(self.details(visit_time=bad))
        with self.assertRaises(ValueError):
            save_visit(self.details(actual_minutes="1441"))


if __name__ == "__main__":
    unittest.main()
