import os
import unittest
from unittest.mock import patch
from app import app

class IosLandingTests(unittest.TestCase):
    def test_unauthenticated_home_exposes_apple_icon_without_private_data(self):
        with patch.dict(os.environ,{"HQ_SESSION_SECRET":"test","GOOGLE_CLIENT_ID":"test","GOOGLE_CLIENT_SECRET":"test","HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.com"}):
            with app.test_client() as client:
                r=client.get("/")
                self.assertEqual(r.status_code,200)
                self.assertIn(b'apple-touch-icon',r.data)
                self.assertIn(b'/hq-icon-180.png',r.data)
                self.assertIn(b'/login',r.data)
                self.assertNotIn(b'customerRequestsList',r.data)
    def test_unconfigured_remains_closed(self):
        with patch("app.configured",return_value=False):
            with app.test_client() as client:
                self.assertEqual(client.get("/").status_code,503)
if __name__=="__main__":unittest.main()
