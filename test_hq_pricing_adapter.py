import unittest
from hq_pricing_adapter import settings_from_values,library_from_values,jobs_for_timer
class AdapterTests(unittest.TestCase):
    def test_rates_from_settings(self):
        rows=[["Setting","Value"],["Retail Labour Rate","£85.00"],["Trader Labour Rate","£70.00"]]
        self.assertEqual(str(settings_from_values(rows)["retail"]),"85.00")
    def test_library(self):
        rows=[["Service","Default Hours","Material £","Consumables £","Retail Mult.","Trader Mult."],
              ["Door Repair & Paint","3.5","£14.00","£6.00","1.40","1.05"]]
        x=library_from_values(rows)["Door Repair & Paint"]
        self.assertEqual(str(x["hours"]),"3.5")
        self.assertEqual(str(x["retail_multiplier"]),"1.40")
    def test_duplicate_service_rejected(self):
        rows=[["Service","Default Hours"],["Tinting","1"],["Tinting","2"]]
        with self.assertRaises(ValueError): library_from_values(rows)
    def test_job_hours(self):
        rows=[["Job ID","Status","Service 1","Quoted Hours","Total Hours"],
              ["J0012","Booked","Front Bumper Repair & Paint","4.5","5"]]
        self.assertEqual(jobs_for_timer(rows)["J0012"]["estimated_hours"],"4.5")
    def test_duplicate_job_ids_preserve_both_rows(self):
        rows=[["Job ID","Status","Quoted Hours"],["J0057","Booked","3"],["J0057","Complete","4"]]
        jobs=jobs_for_timer(rows)
        self.assertEqual(len(jobs),2)
        self.assertEqual(jobs["J0057"]["estimated_hours"],"3")
        self.assertTrue(jobs["J0057 (sheet row 6)"]["duplicate_reference"])
        self.assertEqual(jobs["J0057 (sheet row 6)"]["estimated_hours"],"4")
    def test_missing_rates(self):
        with self.assertRaises(ValueError):settings_from_values([["Setting","Value"]])
if __name__=="__main__":
    unittest.main()
