import unittest
from hq_creditor_capacity import calculate, case_summary, _fields

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
