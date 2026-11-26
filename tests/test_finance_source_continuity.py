import unittest
from pathlib import Path

class FinanceSourceContinuityTests(unittest.TestCase):
 def test_same_existing_endpoint_and_source_labels_preserved(self):
  script=Path('static/finance-command.js').read_text()
  self.assertIn("fetch('/api/hq/finance',{cache:'no-store',credentials:'same-origin'})",script)
  self.assertIn('source.stale',script);self.assertIn('metric.source',script)
  self.assertIn('source.bank_data_date',script);self.assertIn('source.review_count',script)
  self.assertIn('never added together',script)
  # Forecast authority moved to the native server response. Never fall back
  # to local arithmetic when the server withholds an uncertain estimate.
  self.assertIn("fetch('/api/hq/finance/native',{cache:'no-store'})",script)
  self.assertIn("const forecast=data.forecast",script)
  self.assertIn("$('available').textContent=money(forecast.estimate_pence)",script)
  self.assertIn("p===null?'Unavailable'",script)
  self.assertIn('forecast.reason',script)
  self.assertIn('forecast.planned_pence',script)
  self.assertNotIn("data.plan.balance===null||!data.bills.length?null",script)
  self.assertIn("document.addEventListener('visibilitychange'",script)
 def test_overview_has_existing_source_and_refresh(self):
  html=Path('templates/finance_command.html').read_text()
  self.assertIn('id="connectedMetrics"',html);self.assertIn('id="refreshConnected"',html)
  self.assertIn('/hq/finance/sheets',html)
