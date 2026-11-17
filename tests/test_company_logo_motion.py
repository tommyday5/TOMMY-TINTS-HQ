import re
import unittest
from app import app


class CompanyLogoMotionTests(unittest.TestCase):
    def test_company_artwork_and_install_icon_remain_distinct(self):
        with app.test_client() as client, client.get("/book", follow_redirects=True) as response:
            html = response.get_data(as_text=True)
            logo = re.search(r'<img class="hero-logo"[^>]+>', html).group()
            self.assertIn('src="/static/tommy-tints-logo.webp"', logo)
            self.assertIn('width="160" height="160"', logo)
            self.assertIn('rel="apple-touch-icon" href="/static/booking/icon-180.png"', html)
            self.assertIn('@media(prefers-reduced-motion:reduce)', html)
            self.assertIn('animation:company-chrome-sweep 1.3s ease-in-out .7s both', html)
            self.assertNotRegex(html, r'animation:[^;}]*infinite')
            self.assertIn('id="begin"', html)
            self.assertIn('id="quote"', html)
            self.assertNotRegex(html, r'<button[^>]+(?:id="begin"|id="quote")[^>]*disabled')
