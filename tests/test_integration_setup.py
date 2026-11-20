import os,tempfile,unittest
from unittest.mock import patch
from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_integration_setup import setup_inventory

class IntegrationSetupTests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
  p=patch.dict(os.environ,HQ_DATA_DIR=t.name,HQ_SESSION_SECRET='test',GOOGLE_CLIENT_ID='public-test',GOOGLE_CLIENT_SECRET='never-display-secret',HQ_ALLOWED_GOOGLE_EMAIL='owner@example.invalid');p.start();self.addCleanup(p.stop)
 def test_owner_only_local_and_no_secrets(self):
  c=app.test_client();url='/hq/integrations/setup'
  self.assertEqual(c.get(url).status_code,302)
  with c.session_transaction() as s:s['customer_id']='retail'
  self.assertEqual(c.get(url).status_code,302)
  with c.session_transaction() as s,auth_db() as db:s.clear();s['hq_sid']=issue_session(db,'owner');s['google_sub']='owner'
  with patch('requests.get') as get,patch('requests.post') as post:
   r=c.get(url)
  self.assertEqual(r.status_code,200);self.assertEqual(r.headers['Cache-Control'],'no-store')
  self.assertNotIn(b'never-display-secret',r.data);self.assertIn(b'Implementation and verified source required',r.data)
  get.assert_not_called();post.assert_not_called()
 def test_presence_never_certifies_unimplemented_providers(self):
  with patch.dict(os.environ,{'HQ_GOOGLE_MAPS_BROWSER_KEY':'browser-secret','HQ_GOOGLE_MAPS_API_KEY':'server-secret','HQ_ROUTE_BASE_ADDRESS':'','HQ_SQUARE_READ_TOKEN':'token-secret','HQ_SQUARE_LOCATION_ID':'location-secret'}):
   rows={r['key']:r for r in setup_inventory([])}
  self.assertFalse(rows['maps']['configuration_complete']);self.assertTrue(rows['square']['configuration_complete'])
  self.assertEqual(rows['square']['evidence'],{})
  self.assertTrue(rows['monzo']['implementation_required']);self.assertTrue(rows['dna']['implementation_required'])
  self.assertNotIn('server-secret',str(rows));self.assertNotIn('token-secret',str(rows));self.assertNotIn('location-secret',str(rows))
