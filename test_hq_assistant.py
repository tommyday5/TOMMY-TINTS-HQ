import unittest
from hq_assistant import plan_request

class AssistantTests(unittest.TestCase):
    def test_daily_operations_plan_never_executes(self):
        result=plan_request({"message":"Sort out my business today and check my emails"})
        self.assertEqual(result["actions_executed"],0)
        self.assertTrue(any(t["category"]=="email" for t in result["tasks"]))
    def test_sensitive_action_requires_approval(self):
        result=plan_request({"message":"Send a legal email to my creditor"})
        self.assertTrue(result["approval_required"])
        self.assertTrue(all(t["status"]=="not_executed" for t in result["tasks"]))
    def test_external_write_verbs_require_approval(self):
        for message in ("Forward the customer email", "Reschedule the appointment",
                        "Archive the invoice", "Submit the HMRC form",
                        "Update the customer address", "Order new supplies"):
            with self.subTest(message=message):
                result=plan_request({"message":message})
                self.assertTrue(result["approval_required"])
                self.assertEqual(result["actions_executed"],0)
                self.assertTrue(all(task["approval_required"] for task in result["tasks"]))

    def test_read_only_request_does_not_require_blanket_approval(self):
        result=plan_request({"message":"Show my daily operations overview"})
        self.assertFalse(result["approval_required"])
        self.assertEqual(result["actions_executed"],0)

    def test_bad_input(self):
        with self.assertRaises(ValueError):plan_request({"message":""})

if __name__=="__main__":unittest.main()
