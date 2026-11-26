import unittest
import os
import tempfile
from datetime import date
from unittest.mock import patch

from hq_creditor_capacity import calculate, case_summary, _fields, business_affordability, snapshot
from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db

class CapacityFormulaTests(unittest.TestCase):
    def fixture(self):
        return dict(trading=300000, opex=100000, growth=20000, observed_debt=40000,
                    registered_debt=50000, director_funding=0, director_allowance=30000,
                    reserve_target=60000, cash=0, reserve_min=10000, reserve_rate=2000,
                    safe_rate=5000, max_rate=8000, mode="conservative")

    def test_cash_engine_formula_subset(self):
        result=calculate(self.fixture())
        self.assertEqual(result, dict(debt_used=50000, rebuild=10000, free=90000,
                          reserve=18000, critical=False, safe=36000, maximum=57600))

    def test_support_and_reserve_clamps(self):
        d=self.fixture()
        d.update(trading=120000, director_funding=20000)
        self.assertTrue(calculate(d)["critical"])
        self.assertEqual(calculate(d)["maximum"],0)
        d=self.fixture()
        d["reserve_min"]=200000
        self.assertEqual(calculate(d)["safe"],0)
        self.assertEqual(calculate(d)["reserve"],90000)

    def test_partial_case_summary(self):
        cases=[dict(archived=False,scope="business",inclusion="include",committed_monthly=1000,proposed_monthly=1500),
               dict(archived=False,scope="business",inclusion="include",committed_monthly=None,proposed_monthly=4000),
               dict(archived=False,scope="personal",inclusion="include"),
               dict(archived=True,scope="business",inclusion="include"),
               dict(archived=False,scope="joint_or_guarantee",inclusion="include")]
        self.assertEqual(case_summary(cases),dict(included=2,outside=1,archived=1,
            unresolved=1,committed_known=1000,incremental_known=500,incomplete=1))

    def test_missing_inputs_rejected(self):
        with self.assertRaises(ValueError):
            _fields({})

    def test_business_affordability_exact_formula_subset_and_optional_unknowns(self):
        d=self.fixture()
        d["director_funding"]=25000
        result=business_affordability(d)
        self.assertEqual(result, dict(trading=300000, director_funding=25000,
            outgoings=160000, net_self_funded=140000, director_drawings=None,
            reserve_shortfall=60000, safe_capacity=36000, requested_monthly=None,
            requested_excess=None, request_within_capacity=None))
        # H18/H19 always include growth, even if the capacity model excludes it.
        d.update(mode="exclude_growth", requested_monthly=45000, director_drawings=0)
        result=business_affordability(d)
        self.assertEqual(result["outgoings"],160000)
        self.assertEqual(result["net_self_funded"],140000)
        self.assertEqual(result["director_drawings"],0)
        self.assertEqual(result["safe_capacity"],44000)
        self.assertEqual(result["requested_excess"],1000)
        self.assertFalse(result["request_within_capacity"])
        d["requested_monthly"]=44000
        self.assertTrue(business_affordability(d)["request_within_capacity"])
        d.update(trading=120000, cash=70000)
        result=business_affordability(d)
        self.assertEqual(result["net_self_funded"],-40000)
        self.assertEqual(result["reserve_shortfall"],0)
        self.assertEqual(result["safe_capacity"],0)


class BusinessAffordabilityHttpTests(unittest.TestCase):
    def test_owner_save_reload_history_and_optional_field_validation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "HQ_DATA_DIR":tmp, "HQ_SESSION_SECRET":"synthetic",
            "GOOGLE_CLIENT_ID":"test", "GOOGLE_CLIENT_SECRET":"test",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid",
        }), patch("hq_creditor_capacity.storage_ready", return_value=True):
            client=app.test_client()
            url="/hq/finance/creditor-capacity"
            self.assertEqual(client.get(url).status_code,302)
            with client.session_transaction() as sess, auth_db() as db:
                sess["hq_sid"]=issue_session(db,"synthetic-owner")
                sess["google_sub"]="synthetic-owner"
                sess["csrf"]="test"
            self.assertEqual(client.get(url).status_code,200)
            amounts=CapacityFormulaTests().fixture()
            form={k:str(v/100) for k,v in amounts.items() if k!="mode"}
            form.update(mode=amounts["mode"],as_of=date.today().isoformat(),
                        csrf="test",revision="0",operation="a"*32,confirm="yes")
            self.assertEqual(client.post(url,data=dict(form,csrf="wrong")).status_code,403)
            self.assertEqual(client.post(url,data=form).status_code,303)
            self.assertNotIn("requested_monthly",snapshot()[0][0]["state"])
            self.assertEqual(client.post(url,data=form).status_code,303)
            self.assertEqual(len(snapshot()[0]),1)
            html=client.get(url).data
            self.assertIn(b"Net self-funded operating cash after all business outgoings",html)
            self.assertIn(b"Not entered",html)
            update=dict(form,revision="1",operation="b"*32,
                        requested_monthly="400.00",director_drawings="0.00")
            self.assertEqual(client.post(url,data=dict(update,requested_monthly="-1")).status_code,409)
            self.assertEqual(len(snapshot()[0]),1)
            self.assertEqual(client.post(url,data=update).status_code,303)
            self.assertEqual(client.post(url,data=dict(update,operation="c"*32)).status_code,409)
            response=client.get(url)
            self.assertEqual(response.headers["Cache-Control"],"no-store")
            self.assertIn(b"Request exceeds modelled safe capacity by",response.data)
            self.assertIn("£40.00".encode(),response.data)
            history=snapshot()[0]
            self.assertEqual([row["revision"] for row in history],[2,1])
            self.assertEqual(history[0]["state"]["requested_monthly"],40000)
            self.assertEqual(history[0]["state"]["director_drawings"],0)
            self.assertNotIn("director_drawings",history[1]["state"])
            self.assertEqual(app.test_client().get(url).status_code,302)
