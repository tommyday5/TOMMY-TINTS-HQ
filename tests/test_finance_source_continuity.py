import unittest
from pathlib import Path

class FinanceSourceContinuityTests(unittest.TestCase):
 def test_same_existing_endpoint_and_source_labels_preserved(self):
  script=Path('static/finance-command.js').read_text()
  self.assertIn("fetch('/api/hq/finance',{cache:'no-store',credentials:'same-origin'})",script)
  self.assertIn('source.stale',script);self.assertIn('metric.source',script)
  self.assertIn('source.bank_data_date',script);self.assertIn('source.review_count',script)
  self.assertIn('never added together',script)
  self.assertIn("data.plan.balance===null||!data.bills.length?null",script)
  self.assertIn("document.addEventListener('visibilitychange'",script)
 def test_overview_has_existing_source_and_refresh(self):
  html=Path('templates/finance_command.html').read_text()
  self.assertIn('id="connectedMetrics"',html);self.assertIn('id="refreshConnected"',html)
  self.assertIn('/hq/finance/sheets',html)
