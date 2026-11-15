import unittest
from maintenance_economics import travel_charge,route_economics
class RouteEconomicsTests(unittest.TestCase):
 def test_free_travel(self):
  self.assertEqual(travel_charge("8")["travel_surcharge"],"0.00")
 def test_chargeable_round_trip(self):
  self.assertEqual(travel_charge("27.2")["travel_surcharge"],"19.26")
 def test_route(self):
  result=route_economics({"stops":[{"price":"80","clean_minutes":"60","materials_cost":"5"},{"price":"80","clean_minutes":"60","materials_cost":"5"}],"total_route_miles":"20","total_drive_minutes":"30","actual_labour_cost_per_hour":"18","overhead_per_hour":"10"})
  self.assertEqual(result["estimated_contribution"],"68.80")
 def test_reject_unknown_distance(self):
  with self.assertRaises(ValueError):route_economics({"stops":[{"price":"80","clean_minutes":"60","materials_cost":"5"}],"total_drive_minutes":"10","actual_labour_cost_per_hour":"18","overhead_per_hour":"10"})
if __name__=="__main__":unittest.main()
