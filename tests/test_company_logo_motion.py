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
            self.assertIn('src="/static/tommy-tints-company-retina.png"', logo)
            self.assertIn('width="640" height="450"', logo)
            self.assertIn('rel="apple-touch-icon" href="/static/booking/icon-180.png"', html)
            self.assertIn('@media(prefers-reduced-motion:reduce)', html)
            self.assertIn('animation:company-chrome-sweep 1.3s ease-in-out .7s both', html)
            self.assertNotRegex(html, r'animation:[^;}]*infinite')
            self.assertIn('id="begin"', html)
            self.assertIn('id="quote"', html)
            self.assertNotRegex(html, r'<button[^>]+(?:id="begin"|id="quote")[^>]*disabled')

    def test_transparent_png_assets_have_alpha_and_retina_dimensions(self):
        root = Path(__file__).resolve().parents[1]
        for filename, width, height in [
            ("tommy-tints-company-transparent.png", 148, 104),
            ("tommy-tints-company-retina.png", 640, 450),
        ]:
            data = (root / "static" / filename).read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", data[16:24]), (width, height))
            self.assertEqual(data[25], 6)  # RGBA PNG
