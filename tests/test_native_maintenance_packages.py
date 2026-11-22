"""Synthetic regression of canonical HQ maintenance plans and customer privacy."""
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
from hq_maintenance_packages import save_plan, archive_plan, plan_snapshot, plan_history, plan_index

ROOT = Path(__file__).resolve().parents[1]


class NativeMaintenancePlanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": temp.name, "HQ_SESSION_SECRET": "synthetic-plans-only",
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

    def details(self, **overrides):
        data = dict(operation_id=uuid.uuid4().hex, customer_id=self.customer_a,
                    vehicle_id=self.vehicle_a, plan_name="Weekly maintenance package",
                    service="Interior & exterior valet", frequency="weekly",
                    next_due="2026-12-15", status="active", visit_price="",
                    visits_included="", internal_notes="PRIVATE COST 99")
        data.update(overrides)
        return data

    def test_owner_can_create_edit_reopen_and_review_private_history(self):
        self.assertEqual(self.owner.get("/hq/maintenance/plans").status_code, 302)
        self.login()
        self.assertIn(b"Create maintenance plan", self.owner.get("/hq/maintenance/plans").data)
        data = self.details()
        post = self.owner.post("/hq/maintenance/plans/new", data={**data, "csrf": "fixture-csrf"})
        self.assertEqual(post.status_code, 303, post.get_data(as_text=True))
        ident = post.headers["Location"].split("/")[-1].split("?")[0]
        self.assertEqual(len(ident), 32)
        self.assertIsNone(plan_snapshot(ident)["visit_price_pence"])
        self.assertEqual(save_plan(data), ident)
        self.assertIn(b"Weekly maintenance package", self.owner.get("/hq/maintenance/plans/" + ident).data)
        changed = self.owner.post("/hq/maintenance/plans/" + ident, data={
            **self.details(frequency="fortnightly", next_due="2026-12-24",
                           visit_price="80.00", visits_included="6",
                           portal_visible="1"),
            "csrf": "fixture-csrf", "revision": "0",
        })
        self.assertEqual(changed.status_code, 303, changed.get_data(as_text=True))
        saved = plan_snapshot(ident)  # new sqlite connection = reload persistence
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["visit_price_pence"], 8000)
        self.assertEqual(saved["visits_included"], 6)
        self.assertEqual(saved["next_due"], "2026-12-24")
        self.assertEqual(saved["portal_visible"], 1)
        self.assertEqual(len(plan_history(ident)), 2)
        self.assertIn(b"fortnightly", self.owner.get("/hq/maintenance/plans/" + ident).data)
        with connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_maintenance_plan_audit WHERE plan_id=?", (ident,)
            ).fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_maintenance_visits"
                                        if _has_visits(db) else "SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)

    def test_stale_updates_duplicate_constraints_and_restore_collisions(self):
        self.login()
        first = save_plan(self.details())
        self.assertEqual(self.owner.post("/hq/maintenance/plans/" + first, data={
            **self.details(status="paused"), "revision": "9", "csrf": "fixture-csrf",
        }).status_code, 409)
        with self.assertRaises(ValueError):
            save_plan(self.details(plan_name="Another active plan"))
        self.assertEqual(self.owner.post("/hq/maintenance/plans/" + first, data={
            "csrf": "fixture-csrf", "revision": "0", "action": "archive",
        }).status_code, 303)
        second = save_plan(self.details(plan_name="Another active plan"))
        self.assertNotEqual(first, second)
        self.assertEqual(self.owner.post("/hq/maintenance/plans/" + first, data={
            "csrf": "fixture-csrf", "revision": "1", "action": "restore",
        }).status_code, 409)
        self.assertEqual(plan_snapshot(first)["archived"], 1)
        self.assertEqual(len(plan_index(show_archived=True)), 1)

    def test_customer_isolation_and_hidden_pricing(self):
        visible = save_plan(self.details(portal_visible="1", visit_price="137.50",
                                         visits_included="6"))
        other = save_plan(self.details(customer_id=self.customer_b, vehicle_id=self.vehicle_b,
                                       plan_name="Other", portal_visible="1",
                                       internal_notes="SECOND PRIVATE SECRET"))
        hidden = save_plan(self.details(service="Different service", portal_visible="0"))
        self.assertEqual(self.public.get("/customer/maintenance/plans").status_code, 302)
        with patch("hq_maintenance_packages.active_customer", return_value=self.customer_a):
            page = self.public.get("/customer/maintenance/plans")
        self.assertEqual(page.status_code, 200)
        self.assertIn(visible.encode(), page.data)
        self.assertNotIn(hidden.encode(), page.data)
        self.assertNotIn(other.encode(), page.data)
        self.assertNotIn(b"137.50", page.data)
        self.assertNotIn(b"PRIVATE COST", page.data)
        self.assertNotIn(b"SECRET", page.data)
        self.assertIn(b"not booked", page.data)
        self.assertIn("no-store", page.headers["Cache-Control"])
        with patch("hq_maintenance_packages.active_customer", return_value=self.customer_b):
            other_page = self.public.get("/customer/maintenance/plans")
        self.assertIn(other.encode(), other_page.data)
        self.assertNotIn(visible.encode(), other_page.data)

    def test_invalid_references_csrf_and_required_owner_auth(self):
        self.login()
        self.assertEqual(self.owner.post("/hq/maintenance/plans/new", data={
            **self.details(), "csrf": "invalid",
        }).status_code, 403)
        self.assertEqual(self.owner.post("/hq/maintenance/plans/new", data={
            **self.details(vehicle_id=self.vehicle_b), "csrf": "fixture-csrf",
        }).status_code, 400)
        self.assertEqual(self.owner.post("/hq/maintenance/plans/new", data={
            **self.details(), "csrf": "fixture-csrf",
        }).status_code, 303)
        self.assertEqual(self.owner.get("/hq/maintenance/plans/not-an-id").status_code, 404)
        self.assertEqual(self.owner.get("/hq/maintenance/plans?archived=1").status_code, 200)

    def test_money_count_date_and_submission_reference_validation(self):
        for price in ("NaN", "-2", "1.234", "100001", "1e1000"):
            with self.subTest(price=price), self.assertRaises(ValueError):
                save_plan(self.details(visit_price=price))
        for count in ("0", "-1", "1.2", "1001"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                save_plan(self.details(visits_included=count))
        for due in ("2026-02-30", "2026-15-01", "not-a-date"):
            with self.subTest(due=due), self.assertRaises(ValueError):
                save_plan(self.details(next_due=due))
        with self.assertRaises(ValueError):
            save_plan(self.details(customer_id=self.customer_b, vehicle_id=self.vehicle_a))
        original = self.details()
        first = save_plan(original)
        self.assertEqual(save_plan(original), first)
        with self.assertRaises(ValueError):
            save_plan({**original, "plan_name": "Replayed with changed details"})

def _has_visits(db):
    return bool(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='hq_maintenance_visits'").fetchone())

if __name__ == "__main__":
    unittest.main()
