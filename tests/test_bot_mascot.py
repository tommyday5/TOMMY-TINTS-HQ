import unittest
from pathlib import Path
from app import app

class BotMascotTests(unittest.TestCase):
 def test_assets_and_customer_pages(self):
  with app.test_client() as c:
   for name in ('tommy-full.webp','tommy-chat.webp','tommy-avatar-192.webp','tommy-avatar-64.webp'):
    with c.get('/static/bot/'+name) as r:
     self.assertEqual(r.status_code,200);self.assertEqual(r.mimetype,'image/webp');self.assertEqual(r.data[:4],b'RIFF')
   with c.get('/book',follow_redirects=True) as r:self.assertIn(b'tommy-bot.js',r.data)
 def test_accessibility_preferences_and_manual_handover(self):
  root=Path(__file__).resolve().parents[1]
  js=(root/'static/tommy-bot.js').read_text();css=(root/'static/tommy-bot.css').read_text()
  for required in ('aria-expanded','aria-controls','role="dialog"','aria-labelledby','Escape','localStorage.getItem','localStorage.setItem','dismissed','target.click()','AI chat not connected'):
   self.assertIn(required,js)
  self.assertNotIn('/api/hq/',js)
  self.assertIn('prefers-reduced-motion',css);self.assertIn('safe-area-inset-bottom',css)
  self.assertNotIn('fetch(',js) # No pretend or private AI requests.
