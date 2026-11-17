import re
import struct
from pathlib import Path
import unittest
from app import app


class CompanyLogoMotionTests(unittest.TestCase):
    def test_company_artwork_and_install_icon_remain_distinct(self):
        with app.test_client() as client, client.get("/book", follow_redirects=True) as response:
            html = response.get_data(as_text=True)
            logo = re.search(r'<img class="hero-logo"[^>]+>', html).group()
            self.assertIn('src="/static/company-logo-approved.webp"', logo)
            self.assertIn('width="1536" height="1024"', logo)
            self.assertIn('rel="apple-touch-icon" href="/static/booking/icon-180.png?v=20261117d"', html)
            self.assertIn('@media(prefers-reduced-motion:reduce)', html)
            self.assertIn('animation:company-chrome-sweep 1.3s ease-in-out .7s both', html)
            self.assertNotRegex(html, r'animation:[^;}]*infinite')
            self.assertIn('id="begin"', html)
            self.assertIn('id="quote"', html)
            self.assertNotRegex(html, r'<button[^>]+(?:id="begin"|id="quote")[^>]*disabled')

    def test_approved_logo_is_served_with_webp_alpha(self):
        with app.test_client() as client:
            for name in ("company-logo-approved.webp", "company-logo-header.webp"):
                response = client.get("/static/" + name)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "image/webp")
                self.assertEqual(response.data[:4], b"RIFF")
                self.assertEqual(response.data[8:12], b"WEBP")
                self.assertTrue(response.data[20] & 16)  # VP8X alpha flag
                response.close()
