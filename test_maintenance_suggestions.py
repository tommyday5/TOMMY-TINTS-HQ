import unittest
from datetime import date
from maintenance_suggestions import booking_suggestions
class SuggestionTests(unittest.TestCase):
 def test_groups_due_customers_only(self):
  clients=[{"Client ID":"M1","Customer":"A","Status":"Active","Area / Route Cluster":"SS11","Next Due":"2026-11-16"},
           {"Client ID":"M2","Customer":"B","Status":"Active","Area / Route Cluster":"SS11","Next Due":"17/11/2026"},
           {"Client ID":"M3","Customer":"C","Status":"Paused","Area / Route Cluster":"SS11","Next Due":"2026-11-16"}]
  result=booking_suggestions(clients,[],date(2026,11,15))
  self.assertEqual(result["groups"][0]["customer_count"],2)
 def test_existing_visit_excluded(self):
  clients=[{"Client ID":"M1","Customer":"A","Status":"Active","Area / Route Cluster":"SS11","Next Due":"2026-11-16"},
           {"Client ID":"M2","Customer":"B","Status":"Active","Area / Route Cluster":"SS11","Next Due":"2026-11-16"}]
  visits=[{"Client ID":"M1","Visit Date":"2026-11-16","Visit Status":"Confirmed"}]
  self.assertEqual(booking_suggestions(clients,visits,date(2026,11,15))["groups"],[])
 def test_missing_dates_not_invented(self):
  clients=[{"Client ID":"M1","Customer":"A","Status":"Active","Area / Route Cluster":"SS11","Next Due":""}]
  self.assertEqual(booking_suggestions(clients,[],date(2026,11,15))["groups"],[])
if __name__=="__main__":unittest.main()
