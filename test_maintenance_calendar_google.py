import unittest
from unittest.mock import patch,Mock
from maintenance_calendar_google import live_conflicts
class LiveCalendarTests(unittest.TestCase):
 def test_busy(self):
  mock=Mock();mock.json.return_value={"items":[{"start":{"dateTime":"2026-11-18T10:30:00+00:00"},"end":{"dateTime":"2026-11-18T11:30:00+00:00"}}]}
  with patch("maintenance_calendar_google.requests.get",return_value=mock):
   result=live_conflicts("secret","2026-11-18T10:00:00+00:00","2026-11-18T11:00:00+00:00")
  self.assertFalse(result["available"])
 def test_empty_calendar_not_square_confirmation(self):
  mock=Mock();mock.json.return_value={"items":[]}
  with patch("maintenance_calendar_google.requests.get",return_value=mock):
   result=live_conflicts("secret","2026-11-18T10:00:00+00:00","2026-11-18T11:00:00+00:00")
  self.assertTrue(result["available"])
  self.assertIn("Square",result["warning"])
 def test_reject_naive_time(self):
  with self.assertRaises(ValueError):live_conflicts("secret","2026-11-18T10:00","2026-11-18T11:00")
if __name__=="__main__":unittest.main()
