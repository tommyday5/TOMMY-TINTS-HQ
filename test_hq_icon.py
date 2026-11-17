import unittest
from app import app

class IconTests(unittest.TestCase):
    def test_ios_png(self):
        with app.test_client() as client:
            response=client.get("/hq-icon-180.png")
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.mimetype,"image/png")
            self.assertEqual(response.data[:8],b"\x89PNG\r\n\x1a\n")
            self.assertEqual(int.from_bytes(response.data[16:20],"big"),180)
            self.assertEqual(int.from_bytes(response.data[20:24],"big"),180)
    def test_large_png(self):
        with app.test_client() as client:
            response=client.get("/hq-icon-512.png")
            self.assertEqual(response.status_code,200)
            self.assertEqual(int.from_bytes(response.data[16:20],"big"),512)
if __name__=="__main__":unittest.main()
