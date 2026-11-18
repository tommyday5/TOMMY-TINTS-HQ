import os,tempfile,unittest
from unittest.mock import patch,Mock
from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
import hq_speech
class SpeechTests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
  p=patch.dict(os.environ,HQ_DATA_DIR=t.name,HQ_SESSION_SECRET='test',GOOGLE_CLIENT_ID='test',GOOGLE_CLIENT_SECRET='test',HQ_ALLOWED_GOOGLE_EMAIL='owner@example.invalid',OPENAI_API_KEY='synthetic');p.start();self.addCleanup(p.stop);self.c=app.test_client()
 def login(self):
  with self.c.session_transaction() as s,auth_db() as db:s.update(hq_sid=issue_session(db,'owner'),csrf='token',google_sub='owner')
 def test_security_validation_no_store(self):
  path='/api/hq/assistant/speech'
  self.assertEqual(self.c.post(path,json={'text':'Hello'}).status_code,302);self.login()
  self.assertEqual(self.c.post(path,json={'text':'Hello'}).status_code,403)
  h={'X-HQ-CSRF':'token'}
  self.assertEqual(self.c.post(path,json={'text':'x'*2001},headers=h).status_code,400)
  with patch('hq_speech.generate',return_value=b'ID3synthetic'):
   r=self.c.post(path,json={'text':'Hello'},headers=h);self.assertEqual(r.status_code,200);self.assertEqual(r.mimetype,'audio/mpeg');self.assertEqual(r.headers['Cache-Control'],'no-store')
  with patch('hq_speech.generate',side_effect=RuntimeError('secret')):
   r=self.c.post(path,json={'text':'Hello'},headers=h);self.assertEqual(r.status_code,502);self.assertNotIn(b'secret',r.data)
 def test_durable_limit(self):
  self.assertTrue(all(hq_speech.reserve('owner') for _ in range(5)));self.assertFalse(hq_speech.reserve('owner'))
 def test_provider_contract(self):
  response=Mock();response.headers={'Content-Type':'audio/mpeg'};response.iter_content.return_value=[b'ID3test']
  with patch('hq_speech.requests.post',return_value=response) as post:
   self.assertEqual(hq_speech.generate('Hello'),b'ID3test');payload=post.call_args.kwargs['json'];self.assertEqual(payload['voice'],'cedar');self.assertIn('British',payload['instructions']);self.assertEqual(payload['response_format'],'mp3');response.close.assert_called_once()
