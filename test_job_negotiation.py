import unittest
from job_negotiation import evaluate
class NegotiationTests(unittest.TestCase):
    def data(self,price):
        return dict(proposed_price=price,materials="20",labour_cost="60",overhead="30",other_costs="10",minimum_margin_percent="20")
    def test_sustainable(self):
        r=evaluate(self.data("150"))
        self.assertEqual(r["sustainable_floor"],"150.00")
        self.assertEqual(r["status"],"SUSTAINABLE")
    def test_cash_only(self):
        r=evaluate(self.data("110"))
        self.assertEqual(r["status"],"CASH_ONLY_NOT_SUSTAINABLE")
        self.assertFalse(r["sustainable"])
    def test_below_cash_cost(self):
        self.assertEqual(evaluate(self.data("89"))["status"],"BELOW_INCREMENTAL_COST")
    def test_missing_policy_holds(self):
        d=self.data("200");d["minimum_margin_percent"]=""
        with self.assertRaises(ValueError):evaluate(d)
    def test_invalid_margin(self):
        d=self.data("200");d["minimum_margin_percent"]="100"
        with self.assertRaises(ValueError):evaluate(d)
if __name__=="__main__":unittest.main()
