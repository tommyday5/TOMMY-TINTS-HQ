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

 def test_depth_layers_remain_decorative_and_activity_driven(self):
  root=Path(__file__).resolve().parents[1]
  js=(root/'static/hq-reactor.js').read_text();css=(root/'static/hq-reactor.css').read_text()
  self.assertIn('class="hq-reactor-stage" aria-hidden="true"',js)
  self.assertIn('PRIVATE HQ · ACTIVITY LINKED',js)
  self.assertIn('.hq-core-console:not([data-state=standby]) .hq-orbital',css)
  self.assertIn('infinite paused',css)
  self.assertIn('perspective:650px',css)
  self.assertNotIn('requestAnimationFrame',js)

 def test_realistic_gyro_assets_and_activity_guards(self):
  root=Path(__file__).resolve().parents[1]
  js=(root/'static/hq-reactor.js').read_text();css=(root/'static/hq-reactor.css').read_text()
  for name in ('gyro-ring.webp','gyro-eye.webp'):
   data=(root/'static/ai'/name).read_bytes();self.assertEqual(data[:4],b'RIFF');self.assertEqual(data[8:12],b'WEBP')
  self.assertIn('hq-motion-paused',js)
  self.assertIn('hq-gimbal-gold',css);self.assertIn('hq-gimbal-red',css);self.assertIn('hq-gimbal-inner',css)
  self.assertIn('hq-optical-eye',js);self.assertIn('hq-code-rain',js)
