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
            # Zero is a known request, not an omitted amount; exact capacity
            # is permitted in the planning model, and a later blank clears
            # only the new revision, preserving the historical evidence.
            for revision, operation, requested in [(2, "d"*32, "360.00"),
                                                   (3, "e"*32, "0.00"),
                                                   (4, "f"*32, "")]:
                self.assertEqual(client.post(url, data=dict(update, revision=str(revision),
                    operation=operation, requested_monthly=requested)).status_code, 303)
                page=client.get(url).data
                if requested:
                    self.assertIn(b"Request is within modelled shared capacity", page)
                    self.assertNotIn(b"No requested amount entered;", page)
                else:
                    self.assertIn(b"No requested amount entered;", page)
                    self.assertNotIn("requested_monthly", snapshot()[0][0]["state"])
            self.assertEqual(snapshot()[0][2]["state"]["requested_monthly"],36000)
            self.assertEqual(snapshot()[0][3]["state"]["requested_monthly"],40000)
            self.assertEqual(app.test_client().get(url).status_code,302)


class PeriodObservationTests(unittest.TestCase):
    def form(self):
        amounts=CapacityFormulaTests().fixture()
        form={k:str(v/100) for k,v in amounts.items() if k!="mode"}
        form.update(mode="conservative", as_of="2026-01-30", input_basis="period",
                    period_start="2026-01-01", period_trading="3000.00",
                    period_opex="1000.00", period_growth="200.00",
                    period_observed_debt="400.00", period_director_funding="0.00",
                    period_director_drawings="", trading="9999.00",
                    director_drawings="99.00", csrf="test", revision="0",
                    operation="a"*32, confirm="yes")
        return form

    def test_inclusive_period_normalization_and_unknowns(self):
        form=self.form()
        d=_fields(form)
        self.assertEqual(d["period"]["days"],30)
        self.assertEqual(d["period"]["totals"]["trading"],300000)
        self.assertEqual(d["trading"],304375)
        self.assertEqual(d["opex"],101458)
        self.assertEqual(d["growth"],20292)
        self.assertEqual(d["observed_debt"],40583)
        self.assertEqual(d["director_funding"],0)
        self.assertNotIn("director_drawings",d)
        for changes in (dict(period_start="2026-01-31"),dict(period_start="2024-01-01"),
                        dict(period_trading=""),dict(period_growth="-1"),dict(input_basis="other")):
            with self.assertRaises(ValueError):
                _fields(dict(form,**changes))
        known_zero=_fields(dict(form,period_director_drawings="0"))
        self.assertEqual(known_zero["director_drawings"],0)
        self.assertEqual(known_zero["period"]["totals"]["director_drawings"],0)
        direct=_fields(dict(form,input_basis="monthly"))
        self.assertNotIn("period",direct)
        self.assertEqual(direct["trading"],999900)

    def test_owner_period_save_reload_replay_and_switch_to_monthly(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "HQ_DATA_DIR":tmp, "HQ_SESSION_SECRET":"synthetic",
            "GOOGLE_CLIENT_ID":"test", "GOOGLE_CLIENT_SECRET":"test",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid",
        }), patch("hq_creditor_capacity.storage_ready", return_value=True):
            client=app.test_client()
            url="/hq/finance/creditor-capacity"
            with client.session_transaction() as sess, auth_db() as db:
                sess["hq_sid"]=issue_session(db,"synthetic-owner")
                sess["google_sub"]="synthetic-owner"
                sess["csrf"]="test"
            form=self.form()
            self.assertEqual(client.post(url,data=dict(form,csrf="bad")).status_code,403)
            self.assertEqual(client.post(url,data=form).status_code,303)
            self.assertEqual(client.post(url,data=form).status_code,303)
            html=client.get(url).data
            self.assertIn(b"Saved observation period",html)
            self.assertIn(b"30 inclusive days",html)
            self.assertIn("£3043.75".encode(),html)
            self.assertEqual(len(snapshot()[0]),1)
            self.assertEqual(client.post(url,data=dict(form,operation="b"*32)).status_code,409)
            self.assertEqual(client.post(url,data=dict(form,revision="1",operation="c"*32,
                input_basis="monthly")).status_code,303)
            history=snapshot()[0]
            self.assertNotIn("period",history[0]["state"])
            self.assertEqual(history[1]["state"]["period"]["totals"]["trading"],300000)
            self.assertIn(b"original period total",client.get(url).data)
            self.assertEqual(app.test_client().get(url).status_code,302)


class NativePeriodReviewTests(unittest.TestCase):
    def test_preview_save_stale_and_replay_with_split_and_unknowns(self):
        from customer_request_store import connect
        from hq_native_finance import schema
        from hq_finance_splits import tables, save as split_save
        from hq_creditor_capacity import preview_native_period
        from contextlib import closing
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "HQ_DATA_DIR":tmp, "HQ_SESSION_SECRET":"synthetic",
            "GOOGLE_CLIENT_ID":"test", "GOOGLE_CLIENT_SECRET":"test",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid",
        }), patch("hq_creditor_capacity.storage_ready", return_value=True), patch(
            "hq_finance_splits.storage_ready", return_value=True):
            client=app.test_client()
            url="/hq/finance/creditor-capacity"
            with client.session_transaction() as sess, auth_db() as db:
                sess["hq_sid"]=issue_session(db,"synthetic-owner")
                sess["google_sub"]="synthetic-owner"
                sess["csrf"]="test"
            with closing(connect()) as db, db:
                schema(db)
                tables(db)
                entries=[
                    ("1"*32,"2026-01-01","synthetic income",300000,"customer_income",1,0),
                    ("2"*32,"2026-01-02","synthetic split",-120000,"materials",1,0),
                    ("3"*32,"2026-01-03","synthetic transfer",99000,"internal_transfer",1,0),
                    ("4"*32,"2026-01-04","synthetic unreviewed",70000,"customer_income",0,0),
                    ("5"*32,"2026-01-05","synthetic reversed",5000,"insurance",1,0),
                    ("6"*32,"2026-02-01","outside",50000,"customer_income",1,0),
                    ("7"*32,"2026-01-07","archived",20000,"customer_income",1,0),
                ]
                db.executemany("INSERT INTO finance_transactions VALUES(?,?,?,?,?,?,?)",entries)
                db.execute("INSERT INTO finance_transaction_archive VALUES(?,?,?,?,?)",
                           ("7"*32,1,1,"synthetic","2026-01-08"))
            with app.app_context():
                split_save("2"*32,0,"save",[dict(category="fuel",pence=-100000),
                           dict(category="materials",pence=-20000)],"synthetic-owner")
            preview=preview_native_period("2026-01-01","2026-01-30")
            self.assertEqual(preview["mapped"],dict(trading=300000,opex=100000,growth=20000))
            self.assertEqual(preview["archived"],1)
            self.assertEqual(preview["rows"],5)
            self.assertEqual(preview["excluded"]["unreviewed"]["incoming"],70000)
            self.assertEqual(preview["excluded"]["internal_transfer"]["incoming"],99000)
            self.assertEqual(preview["excluded"]["insurance"]["incoming"],5000)
            self.assertFalse(preview["bank_verified"])
            query="?review_start=2026-01-01&review_end=2026-01-30"
            page=client.get(url+query)
            self.assertEqual(page.status_code,200)
            self.assertIn(preview["digest"].encode(),page.data)
            self.assertIn(b'Partial native evidence',page.data)
            self.assertIn(b'Unknown',page.data)
            self.assertEqual(app.test_client().get(url+query).status_code,302)
            form=PeriodObservationTests().form()
            form.update(native_review_digest=preview["digest"],native_review_confirm="yes",
                        period_trading="3100.00")
            self.assertEqual(client.post(url,data=dict(form,native_review_confirm="")).status_code,409)
            with closing(connect()) as db, db:
                db.execute("UPDATE finance_transactions SET revision=1 WHERE id=?",("1"*32,))
            self.assertEqual(client.post(url,data=form).status_code,409)
            self.assertEqual(snapshot()[0],[])
            form["native_review_digest"]=preview_native_period("2026-01-01","2026-01-30")["digest"]
            self.assertEqual(client.post(url,data=form).status_code,303)
            with closing(connect()) as db, db:
                db.execute("UPDATE finance_transactions SET revision=2 WHERE id=?",("1"*32,))
            self.assertEqual(client.post(url,data=form).status_code,303)
            state=snapshot()[0][0]["state"]
            self.assertEqual(state["period"]["totals"]["trading"],310000)
            self.assertEqual(state["native_review"]["mapped"]["trading"],300000)
            self.assertEqual(len(snapshot()[0]),1)
            self.assertIn(b"Saved native observation provenance",client.get(url).data)
