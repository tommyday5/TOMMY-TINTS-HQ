import unittest
from quote_engine import calculate_quote
class QuoteTests(unittest.TestCase):
    def test_retail(self):
        x=calculate_quote({"tier":"retail","hours":2,"materials":20,"consumables":5,"overhead":10})
        self.assertEqual(x["cost_based_target"],"205.00")
        self.assertTrue(x["requires_owner_approval"])
    def test_trade(self):
        self.assertEqual(calculate_quote({"tier":"trade","hours":2})["cost_based_target"],"140.00")
    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            calculate_quote({"hours":-1})
    def test_invalid_tier_rejected(self):
        with self.assertRaises(ValueError):
            calculate_quote({"tier":"custom"})
    def test_quote_gap(self):
        self.assertEqual(calculate_quote({"hours":1,"quoted_price":80})["difference_to_target"],"-5.00")
if __name__=="__main__":
    unittest.main()
