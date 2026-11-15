import unittest
from maintenance_planner import rows
class MaintenancePlannerTests(unittest.TestCase):
 def test_header_mapping_and_empty_rows(self):
  result=rows([["Client ID","Customer","Postcode"],["M001","JJ","SS11 7LL"],[],["M002","Nick"]])
  self.assertEqual(len(result),2)
  self.assertEqual(result[0]["Postcode"],"SS11 7LL")
  self.assertEqual(result[1]["Postcode"],"")
 def test_empty(self):
  self.assertEqual(rows([]),[])
if __name__=="__main__":unittest.main()
