import unittest
from materials_costing import cost_line,calculate_materials

class MaterialTests(unittest.TestCase):
    def test_clearcoat(self):
        self.assertEqual(cost_line({"name":"Clear","purchase_price":100,"package_quantity":5000,"used_quantity":300})["cost"],"6.00")
    def test_clearcoat_complete_kit(self):
        self.assertEqual(cost_line({"method":"clearcoat_kit","purchase_price":100,"clear_ml":5000,"hardener_ml":2500,"used_quantity":300})["cost"],"4.00")
    def test_clearcoat_waste(self):
        self.assertEqual(cost_line({"method":"clearcoat_kit","purchase_price":100,"clear_ml":5000,"hardener_ml":2500,"used_quantity":300,"waste_percent":10})["cost"],"4.40")
    def test_discs(self):
        self.assertEqual(cost_line({"name":"P320","purchase_price":35,"package_quantity":50,"used_quantity":5})["cost"],"3.50")
    def test_basecoat(self):
        self.assertEqual(cost_line({"name":"Basecoat","method":"direct","cost":8.61})["cost"],"8.61")
    def test_filler(self):
        self.assertEqual(cost_line({"name":"Filler","method":"filler_preset","purchase_price":20,"package_quantity":1000,"preset":"medium"})["cost"],"1.80")
    def test_waste(self):
        self.assertEqual(cost_line({"purchase_price":100,"package_quantity":5000,"used_quantity":300,"waste_percent":10})["cost"],"6.60")
    def test_invalid(self):
        with self.assertRaises(ValueError): cost_line({"purchase_price":100,"package_quantity":0,"used_quantity":20})
    def test_total(self):
        result=calculate_materials([{"method":"direct","cost":8.61},{"purchase_price":35,"package_quantity":50,"used_quantity":5}])
        self.assertEqual(result["total_cost"],"12.11")
if __name__=="__main__": unittest.main()
