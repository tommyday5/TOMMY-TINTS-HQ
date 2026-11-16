import unittest
from enquiry_agent import analyse_enquiry

class EnquiryAgentTests(unittest.TestCase):
    def test_multiple_services_and_draft(self):
        result = analyse_enquiry({"description": "Refurbish my wheels gloss black and paint my calipers red and fit a splitter", "vehicle": {"make": "BMW", "model": "3 Series"}})
        self.assertIn("wheel_refurbishment", [x["service_id"] for x in result["existing_services"]])
        self.assertIn("caliper_painting", [x["service_id"] for x in result["new_service_proposals"]])
        self.assertIn("splitter_installation", [x["service_id"] for x in result["new_service_proposals"]])
        self.assertEqual(result["pricing_status"], "not_priced")
        self.assertTrue(result["requires_owner_approval"])
    def test_unknown_service_asks_for_details(self):
        result = analyse_enquiry({"description": "I need something unusual done"})
        self.assertEqual(result["existing_services"], [])
        self.assertTrue(result["clarifying_questions"])
    def test_rejects_invalid_payload(self):
        with self.assertRaises(ValueError):
            analyse_enquiry({"description": ""})
        with self.assertRaises(ValueError):
            analyse_enquiry({"description": "paint bumper", "vehicle": []})

if __name__ == "__main__":
    unittest.main()
