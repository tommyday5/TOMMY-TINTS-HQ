"""Synthetic end-to-end tests for native maintenance plan visit entitlements.

No production records, Sheets cells, messages, bookings or payment providers are touched.
"""
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
from hq_maintenance_packages import save_plan, plan_snapshot
from hq_native_maintenance import save_visit, visit_snapshot, archive_visit
from hq_maintenance_entitlements import (
    _tables, change_link, usage, plan_links, history
)

ROOT = Path(__file__).resolve().parents[1]


class MaintenanceEntitlementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": temp.name,
            "HQ_SESSION_SECRET": "synthetic-entitlement-only",
            "GOOGLE_CLIENT_ID": "fixture", "GOOGLE_CLIENT_SECRET": "fixture",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner = app.test_client()
        self.customer_client = app.test_client()
        self.customer_a, self.vehicle_a = uuid.uuid4().hex, uuid.uuid4().hex
        self.customer_b, self.vehicle_b = uuid.uuid4().hex, uuid.uuid4().hex
        with connect() as db:
            _schema(db)
            now = "2026-11-22T00:00:00+00:00"
            for customer, vehicle, label in [
                (self.customer_a, self.vehicle_a, "Synthetic One"),
                (self.customer_b, self.vehicle_b, "Synthetic Two"),
            ]:
                db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                           (customer, None, label, "fake@example.invalid", "07700 900000", now))
                db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                           (vehicle, customer, "Ford", "Fiesta", "2020", "TEST ONLY", now))

    def login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="fixture-csrf", google_sub="synthetic-owner")

    def plan(self, **updates):
        data = dict(operation_id=uuid.uuid4().hex, customer_id=self.customer_a,
                    vehicle_id=self.vehicle_a, plan_name="Valet package",
                    service="Maintenance valet", frequency="fortnightly", next_due="2026-12-15",
                    status="active", visits_included="2", visit_price="80.00",
                    internal_notes="PRIVATE PLAN MARGIN", portal_visible="1")
        data.update(updates)
        return data

    def visit(self, **updates):
        data = dict(operation_id=uuid.uuid4().hex, customer_id=self.customer_a,
                    vehicle_id=self.vehicle_a, service="Maintenance valet",
                    visit_day="2026-12-10", visit_time="10:00", status="completed",
                    planned_price="80.00", actual_minutes="60",
                    internal_notes="PRIVATE SHOP COST", portal_visible="1")
        data.update(updates)
        return data

    def link(self, plan, visit, **kwargs):
        return change_link(plan, visit, action="link", operation_id=uuid.uuid4().hex,
                           plan_revision=plan_snapshot(plan)["revision"], **kwargs)

    def test_owner_link_and_customer_usage_are_persistent_and_private(self):
        plan = save_plan(self.plan())
        visit = save_visit(self.visit())
        self.login()
        page = self.owner.get("/hq/maintenance/plans/" + plan)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Link completed visit to plan", page.data)
        self.assertIn(visit.encode(), page.data)
        form = dict(csrf="fixture-csrf", revision="0", operation_id=uuid.uuid4().hex,
                    action="link_visit", visit_id=visit)
        self.assertEqual(self.owner.post("/hq/maintenance/plans/" + plan, data=form).status_code, 303)
        reopened = self.owner.get("/hq/maintenance/plans/" + plan)
        self.assertIn(b"1 remaining", reopened.data)
        with connect() as db:
            _tables(db)
            self.assertEqual(usage(db, plan), {"used": 1, "included": 2, "remaining": 1})
            self.assertEqual(len(plan_links(db, plan)), 1)
            self.assertEqual(len(history(db, plan)), 1)
        with patch("hq_maintenance_packages.active_customer", return_value=self.customer_a):
            portal = self.customer_client.get("/customer/maintenance/plans")
        self.assertEqual(portal.status_code, 200)
        self.assertIn(b"1 of 2", portal.data)
        self.assertNotIn(b"PRIVATE PLAN", portal.data)
        self.assertNotIn(b"PRIVATE SHOP", portal.data)
        self.assertNotIn(b"80.00", portal.data)
        self.assertIn(b"not confirm any payment", portal.data)
        with patch("hq_maintenance_packages.active_customer", return_value=self.customer_b):
            other = self.customer_client.get("/customer/maintenance/plans")
        self.assertNotIn(plan.encode(), other.data)
        self.assertNotIn(b"1 of 2", other.data)

    def test_duplicate_link_submit_and_replay_are_idempotent(self):
        plan = save_plan(self.plan())
        visit = save_visit(self.visit())
        op = uuid.uuid4().hex
        self.assertEqual(change_link(plan, visit, action="link", operation_id=op,
                                     plan_revision=0), 0)
        self.assertEqual(change_link(plan, visit, action="link", operation_id=op,
                                     plan_revision=0), 0)
        # Another exact link is a no-op; it does not double-consume allowance.
        self.link(plan, visit)
        with connect() as db:
            self.assertEqual(usage(db, plan)["used"], 1)
            self.assertEqual(len(history(db, plan)), 1)
        with self.assertRaises(ValueError):
            change_link(plan, uuid.uuid4().hex, action="link",
                        operation_id=op, plan_revision=0)

    def test_invalid_visit_identity_stage_service_and_quota_are_blocked(self):
        plan = save_plan(self.plan(visits_included="1"))
        wrong_customer = save_visit(self.visit(customer_id=self.customer_b, vehicle_id=self.vehicle_b))
        wrong_service = save_visit(self.visit(service="Wheel polishing", visit_time="11:00"))
        planned = save_visit(self.visit(status="planned", visit_time="12:00"))
        correct = save_visit(self.visit(visit_time="13:00"))
        extra = save_visit(self.visit(visit_time="14:00"))
        for visit in (wrong_customer, wrong_service, planned):
            with self.subTest(visit=visit), self.assertRaises(ValueError):
                self.link(plan, visit)
        self.assertEqual(self.link(plan, correct), 0)
        with self.assertRaisesRegex(ValueError, "allowance exhausted"):
            self.link(plan, extra)
        with self.assertRaises(ValueError):
            save_plan(self.plan(visits_included="0"), plan_id=plan, expected_revision=0)
        with self.assertRaisesRegex(ValueError, "Unlink"):
            save_plan(self.plan(service="Changed"), plan_id=plan, expected_revision=0)
        with self.assertRaisesRegex(ValueError, "Unlink"):
            save_visit(self.visit(status="cancelled", visit_time="13:00"),
                       visit_id=correct, expected_revision=0)
        with self.assertRaisesRegex(ValueError, "Unlink"):
            save_visit(self.visit(service="Changed", visit_time="13:00"),
                       visit_id=correct, expected_revision=0)
        self.assertEqual(visit_snapshot(correct)["status"], "completed")

    def test_unlink_restore_relink_and_archive_preserve_audit_and_count(self):
        plan = save_plan(self.plan(visits_included="1"))
        visit = save_visit(self.visit())
        self.link(plan, visit)
        op = uuid.uuid4().hex
        self.assertEqual(change_link(plan, visit, action="unlink", operation_id=op,
                                     plan_revision=0, link_revision=0), 1)
        self.assertEqual(change_link(plan, visit, action="unlink", operation_id=op,
                                     plan_revision=0, link_revision=0), 1)
        with connect() as db:
            self.assertEqual(usage(db, plan)["remaining"], 1)
        with self.assertRaises(ValueError):
            change_link(plan, visit, action="unlink", operation_id=uuid.uuid4().hex,
                        plan_revision=0, link_revision=0)
        self.assertEqual(self.link(plan, visit), 2)
        archive_visit(visit, 0, archive=True)
        with connect() as db:
            self.assertEqual(usage(db, plan)["used"], 1)
            self.assertEqual([event["revision"] for event in history(db, plan)],
                             [2, 1, 0])
        self.assertEqual(visit_snapshot(visit)["archived"], 1)

    def test_owner_only_csrf_and_stale_plan_or_link_revision(self):
        plan = save_plan(self.plan())
        visit = save_visit(self.visit())
        form = dict(csrf="fixture-csrf", revision="0", operation_id=uuid.uuid4().hex,
                    action="link_visit", visit_id=visit)
        self.assertEqual(self.owner.post("/hq/maintenance/plans/" + plan, data=form).status_code, 302)
        self.login()
        self.assertEqual(self.owner.post("/hq/maintenance/plans/" + plan,
                                         data={**form, "csrf": "wrong"}).status_code, 403)
        self.assertEqual(self.owner.post("/hq/maintenance/plans/" + plan,
                                         data={**form, "revision": "9"}).status_code, 409)
        self.assertEqual(self.owner.post("/hq/maintenance/plans/" + plan, data=form).status_code, 303)
        unlink_form = dict(csrf="fixture-csrf", revision="0", action="unlink_visit",
                           operation_id=uuid.uuid4().hex, visit_id=visit, link_revision="9")
        self.assertEqual(self.owner.post("/hq/maintenance/plans/" + plan,
                                         data=unlink_form).status_code, 409)
        self.assertEqual(self.owner.post("/hq/maintenance/plans/" + plan,
                                         data={**unlink_form, "link_revision": "0"}).status_code, 303)
        self.assertEqual(self.owner.get("/hq/maintenance/plans/" + plan).status_code, 200)


if __name__ == "__main__":
    unittest.main()
