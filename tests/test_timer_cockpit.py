import os,tempfile,unittest
from unittest.mock import patch
from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import create_test_job

class TimerCockpitTests(unittest.TestCase):
 def test_owner_selector_and_real_job_cockpit(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,HQ_DATA_DIR=tmp,HQ_SESSION_SECRET='test',GOOGLE_CLIENT_ID='test',GOOGLE_CLIENT_SECRET='test',HQ_ALLOWED_GOOGLE_EMAIL='owner@example.invalid'):
   c=app.test_client();self.assertEqual(c.get('/hq/timer').status_code,302)
   with c.session_transaction() as s,auth_db() as db:s['hq_sid']=issue_session(db,'owner');s['csrf']='test'
   work=create_test_job()
   with patch('requests.get') as get,patch('requests.post') as post:
    chooser=c.get('/hq/timer');self.assertEqual(chooser.status_code,200)
    r=c.get('/hq/work/'+work+'/time');self.assertEqual(r.status_code,200)
   self.assertEqual(r.headers['Cache-Control'],'no-store');self.assertIn(b'allowanceRing',r.data)
   self.assertIn(b'Estimate not recorded',r.data);self.assertNotIn(b'DEMO 01',r.data)
   self.assertIn(b'Time entry history',r.data);get.assert_not_called();post.assert_not_called()
