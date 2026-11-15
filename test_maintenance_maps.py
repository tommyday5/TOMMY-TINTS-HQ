import unittest
from unittest.mock import patch
from maintenance_maps import driving_matrix
class MapsTests(unittest.TestCase):
 def test_requires_full_addresses(self):
  with self.assertRaises(ValueError):driving_matrix(["SS11","RM14"])
 def test_requires_key(self):
  with patch.dict("os.environ",{},clear=True):
   with self.assertRaises(RuntimeError):driving_matrix(["10 Example Road, RM14 3TJ, UK","22 Sample Road, SS11 7LL, UK"])
if __name__=="__main__":unittest.main()
