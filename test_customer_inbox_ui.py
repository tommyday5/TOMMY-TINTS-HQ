"""Static contract tests for customer request navigation and draft UI wiring."""
import unittest
from pathlib import Path

class CustomerInboxUiTests(unittest.TestCase):
    def test_navigation_and_draft_controls(self):
        source=Path(__file__).with_name("app.py").read_text()
        self.assertIn('data-page="customer-requests"',source)
        self.assertIn('"customer-requests":"Customer Requests"',source)
        self.assertIn('id="customerDraftsList"',source)
        self.assertIn('async function refreshCustomerDrafts()',source)
        self.assertIn('"/api/hq/customer-request-drafts"',source)
        self.assertIn('"/draft","POST",{draft_type:type}',source)
        self.assertIn('Nothing sent or booked.',source)

if __name__=="__main__": unittest.main()
