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
    def test_bad_input(self):
        with self.assertRaises(ValueError):plan_request({"message":""})

if __name__=="__main__":unittest.main()
