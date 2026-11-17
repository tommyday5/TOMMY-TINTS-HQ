import unittest

from app import app


class InstallTests(unittest.TestCase):
    def test_install_page_exposes_branded_apple_icon_without_login(self):
        with app.test_client() as client:
            response = client.get("/install-hq")
            self.assertEqual(response.status_code, 200)
            self.assertIn(
                b'rel="apple-touch-icon" sizes="180x180" href="/hq-icon-180.png?v=20261117d"',
                response.data,
            )
            self.assertIn(b'<img src="/hq-icon-180.png?v=20261117d"', response.data)
            self.assertIn(b'href="/static/manifest.webmanifest?v=20261117d"', response.data)
            self.assertIn(b'href="/favicon.ico?v=20261117d"', response.data)
            self.assertNotIn(b'customerRequestsList', response.data)
