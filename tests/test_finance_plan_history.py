"""Synthetic owner finance plan retention and conservative forecast."""
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_native_finance import schema, planning_forecast


class FinancePlanHistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name, "HQ_SESSION_SECRET": "synthetic",
            "GOOGLE_CLIENT_ID": "test", "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()

    def test_owner_history_legacy_edit_reload_and_conflict(self):
        url = "/api/hq/finance/native"
        self.assertEqual(self.client.get(url).status_code, 302)
        with connect() as db:
            schema(db)
            db.execute("INSERT INTO finance_plan VALUES(1,100000,'2026-01-01',200000,4)")
        with self.client.session_transaction() as sess, auth_db() as db:
            sess["hq_sid"] = issue_session(db, "synthetic-owner")
            sess["google_sub"] = "synthetic-owner"
            sess["csrf"] = "test"
        self.assertIn(b"planHistory", self.client.get("/hq/finance/command").data)
        self.assertEqual(self.client.get(url).json["plan_history"], [])
        data = dict(kind="plan", revision=4, balance=90000,
                    balance_date="2026-01-02", target=300000)
        self.assertEqual(self.client.post(url, json=data).status_code, 403)
        headers = {"X-HQ-CSRF": "test"}
        with patch("requests.get") as get, patch("requests.post") as post:
            self.assertEqual(self.client.post(url, json=data, headers=headers).status_code, 200)
            self.assertEqual(self.client.post(url, json=data, headers=headers).status_code, 409)
            get.assert_not_called()
            post.assert_not_called()
        response = self.client.get(url)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertFalse(response.json["bank_verified"])
        self.assertEqual(response.json["plan"]["revision"], 5)
        history = response.json["plan_history"]
        self.assertEqual([r["revision"] for r in history], [5, 4])
        self.assertEqual([r["snapshot"]["balance"] for r in history], [90000,100000])
        self.assertEqual([r["capture_kind"] for r in history], ["saved","before_edit"])
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_plan_history").fetchone()[0], 2)
        self.assertEqual(app.test_client().get(url).status_code, 302)

    def test_exact_forecast_overdue_boundary_and_uncertain_balance(self):
        today = date(2026,11,26)
        plan = dict(balance=100000, balance_date="2026-11-25")
        def bill(day, amount, status="planned", archived=0, status_date=None):
            return dict(due=day, amount=amount, status=status, archived=archived,
                        status_date=status_date)
        bills = [bill("2026-11-01",20000),bill("2026-12-26",30000),
                 bill("2026-12-27",40000),bill("2026-12-01",9000,archived=1),
                 bill("2026-12-01",8000,status="cancelled")]
        forecast = planning_forecast(plan,bills,today)
        self.assertEqual(forecast["through"],"2026-12-26")
        self.assertEqual(forecast["planned_pence"],50000)
        self.assertEqual(forecast["estimate_pence"],50000)
        self.assertFalse(forecast["bank_verified"])
        self.assertIsNone(planning_forecast(plan,[],today)["estimate_pence"])
        self.assertIsNone(planning_forecast(dict(plan,balance_date="2026-11-27"),bills,today)["estimate_pence"])
        self.assertIsNone(planning_forecast(plan,bills+[bill("2026-11-25",1,"reported_paid",status_date="2026-11-25")],today)["estimate_pence"])
        self.assertEqual(planning_forecast(plan,bills+[bill("2026-11-24",1,"reported_paid",status_date="2026-11-24")],today)["estimate_pence"],50000)

    def test_owner_forecast_response_is_recomputed_after_save_and_reload(self):
        url = "/api/hq/finance/native"
        with self.client.session_transaction() as sess, auth_db() as db:
            sess["hq_sid"] = issue_session(db, "synthetic-owner")
            sess["google_sub"] = "synthetic-owner"
            sess["csrf"] = "test"
        headers = {"X-HQ-CSRF": "test"}
        with patch("hq_native_finance.datetime", wraps=__import__("datetime").datetime) as clock:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            clock.now.return_value = datetime(2026, 11, 26, 12, tzinfo=ZoneInfo("Europe/London"))
            def save(**data):
                return self.client.post(url, json=data, headers=headers)
            self.assertEqual(save(kind="plan", revision=0, balance=100000,
                                  balance_date="2026-11-25", target=200000).status_code, 200)
            self.assertIsNone(self.client.get(url).json["forecast"]["estimate_pence"])
            bill = dict(kind="bill", id="a"*32, revision=0, name="Synthetic rent",
                        due="2026-11-24", amount=30000, status="planned")
            self.assertEqual(save(**bill).status_code, 200)
            forecast = self.client.get(url).json["forecast"]
            self.assertEqual(forecast["estimate_pence"], 70000)
            self.assertFalse(forecast["bank_verified"])
            self.assertEqual(save(**dict(bill, revision=1, status="reported_paid",
                                         status_date="2026-11-25")).status_code, 200)
            forecast = self.client.get(url).json["forecast"]
            self.assertIsNone(forecast["estimate_pence"])
            self.assertIn("reported payment", forecast["reason"])
            self.assertEqual(save(kind="plan", revision=1, balance=70000,
                                  balance_date="2026-11-26", target=200000).status_code, 200)
            self.assertEqual(self.client.get(url).json["forecast"]["estimate_pence"], 70000)
