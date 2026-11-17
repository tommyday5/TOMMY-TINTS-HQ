import unittest
from app import app

class InstallTests(unittest.TestCase):
    def test_install_page_exposes_branded_apple_icon_without_login(self):
        with app.test_client() as client:
            r=client.get("/install-hq")
            self.assertEqual(r.status_code,200)
            self.assertIn(b'apple-touch-icon',r.data)
            self.assertIn(b'hq-icon-180.png?v=20261117b',r.data)
            self.assertNotIn(b'customerRequestsList',r.data)
    def test_png_is_valid(self):
        with app.test_client() as client:
            r=client.get("/hq-icon-180.png?v=20261117b")
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.data[:8],b"\x89PNG\r\n\x1a\n")
if __name__=="__main__":unittest.main()
