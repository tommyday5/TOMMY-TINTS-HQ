import unittest
from maintenance_calendar import check_booking_window
class CalendarTests(unittest.TestCase):
 def test_overlap(self):
  result=check_booking_window({"start":"2026-11-18T10:00:00+00:00","end":"2026-11-18T11:00:00+00:00","events":[{"start":"2026-11-18T10:30:00+00:00","end":"2026-11-18T12:00:00+00:00"}]})
  self.assertFalse(result["available"])
 def test_adjacent_is_not_conflict(self):
  result=check_booking_window({"start":"2026-11-18T10:00:00+00:00","end":"2026-11-18T11:00:00+00:00","events":[{"start":"2026-11-18T11:00:00+00:00","end":"2026-11-18T12:00:00+00:00"}]})
  self.assertTrue(result["available"])
 def test_naive_rejected(self):
  with self.assertRaises(ValueError):check_booking_window({"start":"2026-11-18T10:00","end":"2026-11-18T11:00"})
if __name__=="__main__":unittest.main()
