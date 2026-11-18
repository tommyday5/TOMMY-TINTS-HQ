import unittest
from pathlib import Path
class ReactorTests(unittest.TestCase):
 def test_real_activity_hooks_and_private_scope(self):
  root=Path(__file__).resolve().parents[1];app=(root/'app.py').read_text();js=(root/'static/hq-reactor.js').read_text();css=(root/'static/hq-reactor.css').read_text()
  for event in ('hq-assistant-thinking','hq-assistant-reply','hq-assistant-finished'):self.assertIn(event,app);self.assertIn(event,js)
  for control in ('hqMicConsent','onstart','onend','onerror','onboundary','visibilitychange','pagehide'):self.assertIn(control,js)
  self.assertIn('infinite paused',css);self.assertIn('prefers-reduced-motion',css)
  self.assertNotIn('hq-reactor',(root/'static/customer-booking-preview.html').read_text())
  self.assertIn("fetch('/api/hq/assistant/speech'",js);self.assertIn('X-HQ-CSRF',js);self.assertIn('a.controls=true',js);self.assertIn('AbortController',js)
