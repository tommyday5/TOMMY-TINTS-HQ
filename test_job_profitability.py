import unittest
from job_profitability import profitability

class ProfitabilityTests(unittest.TestCase):
    def test_job_margin(self):
        result=profitability({"sale":"350","labour":"3","actual_labour_rate":"18","overhead":"25","other_costs":"5","lines":[{"cost":"4.00"},{"cost":"6.00"}]})
        self.assertEqual(result["total_recorded_cost"],"94.00")
        self.assertEqual(result["estimated_contribution"],"256.00")
        self.assertEqual(result["margin_percent"],"73.14")
        self.assertTrue(result["complete_costs"])
    def test_missing_costs_flagged(self):
        result=profitability({"sale":"100","labour":"1","lines":[]})
        self.assertFalse(result["complete_costs"])
    def test_invalid_inputs(self):
        for bad in ("NaN","Infinity","-1"):
            with self.assertRaises(ValueError):
                profitability({"sale":bad,"lines":[]})
    def test_zero_sale(self):
        result=profitability({"sale":"0","lines":[]})
        self.assertIsNone(result["margin_percent"])
if __name__=="__main__": unittest.main()
