import unittest
from service_analytics import summarise_services

class ServiceAnalyticsTests(unittest.TestCase):
    def test_completed_only_and_cash_separate(self):
        rows = summarise_services([
            {"id":"a","service":"Tinting","status":"booked","price":"500","cash_received":"50"},
            {"id":"b","service":"Tinting","status":"completed","price":"200","material_cost":"30","other_direct_cost":"10","labour_hours":"2","cash_received":"150"},
            {"id":"c","service":"Paint","status":"completed","price":"400","material_cost":"200","labour_hours":"8"}
        ])
        tint = next(x for x in rows if x["service"] == "Tinting")
        self.assertEqual(tint["earned_revenue"], "200.00")
        self.assertEqual(tint["cash_received"], "200.00")
        self.assertEqual(tint["gross_contribution"], "160.00")
        self.assertEqual(tint["contribution_per_hour"], "80.00")
        self.assertEqual(tint["booked_jobs"], 1)

    def test_duplicate_rejected(self):
        with self.assertRaises(ValueError):
            summarise_services([{"id":"a","service":"Tinting","status":"booked"},
                                {"id":"a","service":"Paint","status":"completed"}])

    def test_zero_hours_no_fake_hourly_rate(self):
        result = summarise_services([{"id":"a","service":"Tinting","status":"completed","price":"100"}])
        self.assertIsNone(result[0]["contribution_per_hour"])

    def test_invalid_negative_rejected(self):
        with self.assertRaises(ValueError):
            summarise_services([{"id":"a","service":"Tinting","status":"completed","price":"-1"}])

if __name__ == "__main__":
    unittest.main()
