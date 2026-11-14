import unittest
from google_sheets_adapter import jobs_from_values, summarise_sheet_values

HEADERS = ["Job ID","Status","Quoted Price","Total Hours","Materials £",
"Consumables £","Actual Hours","Actual Materials £","Actual Consumables £",
"Actual Extra Costs £","Service 1","Service 2","Service 3","Service 4","Service 5"]

class SheetsAdapterTests(unittest.TestCase):
    def test_actuals_and_completed_status(self):
        row = ["j0082","Completed","£150","2.5","£10","£2.97","2.1","£8.27","","",
               "Quarter Repair & Paint","","","",""]
        output = summarise_sheet_values([HEADERS,row])
        self.assertEqual(output[0]["earned_revenue"],"150.00")
        self.assertEqual(output[0]["direct_cost"],"11.24")
        self.assertEqual(output[0]["contribution_per_hour"],"66.08")
        self.assertEqual(output[0]["cash_received"],"0.00")
    def test_unpaid_completed_job_recognised_not_cash(self):
        row = ["J1","Awaiting Payment","£350","5","£18","£10","","","","",
               "Door Repair & Paint","","","",""]
        job = jobs_from_values([HEADERS,row])[0]
        self.assertEqual(job["status"],"completed")
        self.assertEqual(job["cash_received"],"0")
    def test_multiservice_not_double_counted(self):
        row = ["J2","Paid","£500","8","£30","£8","","","","",
               "Door Repair & Paint","Roof Paint / Respray","","",""]
        result = summarise_sheet_values([HEADERS,row])
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]["service"],"Multi-service (unallocated)")
        self.assertEqual(result[0]["earned_revenue"],"500.00")
    def test_booked_not_earned(self):
        row = ["J3","In Progress","£500","8","£30","£8","","","","",
               "Door Repair & Paint","","","",""]
        result = summarise_sheet_values([HEADERS,row])
        self.assertEqual(result[0]["earned_revenue"],"0.00")
    def test_duplicate_ids_rejected_case_insensitive(self):
        row = ["j4","Completed","£100","1","£10","£2","","","","",
               "Door Repair & Paint","","","",""]
        with self.assertRaises(ValueError):
            summarise_sheet_values([HEADERS,row,["J4"]+row[1:]])

if __name__ == "__main__":
    unittest.main()
