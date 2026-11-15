import os, tempfile, unittest
from job_material_store import read_log,write_log
class StoreTests(unittest.TestCase):
    def test_persistence_and_owner_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous=os.environ.get("HQ_DATA_DIR")
            os.environ["HQ_DATA_DIR"]=tmp
            try:
                self.assertIsNone(read_log("owner-a","J0001")["data"])
                write_log("owner-a","J0001",{"lines":[{"name":"Clearcoat","cost":"4.00"}],"sale":"350","labour":"3"})
                self.assertEqual(read_log("owner-a","J0001")["data"]["lines"][0]["cost"],"4.00")
                self.assertIsNone(read_log("owner-b","J0001")["data"])
                write_log("owner-a","J0001",{"lines":[],"sale":"350","labour":"3"})
                self.assertEqual(read_log("owner-a","J0001")["data"]["lines"],[])
            finally:
                if previous is None: os.environ.pop("HQ_DATA_DIR",None)
                else: os.environ["HQ_DATA_DIR"]=previous
if __name__=="__main__": unittest.main()
