import os,tempfile,unittest
from unittest.mock import patch
from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db

class NativeFinanceWorkspaceTests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
  p=patch.dict(os.environ,HQ_DATA_DIR=t.name,HQ_SESSION_SECRET='test',GOOGLE_CLIENT_ID='test',GOOGLE_CLIENT_SECRET='test',HQ_ALLOWED_GOOGLE_EMAIL='owner@example.invalid');p.start();self.addCleanup(p.stop)
  self.c=app.test_client();self.url='/api/hq/finance/native'
 def login(self):
  with self.c.session_transaction() as s,auth_db() as db:s['hq_sid']=issue_session(db,'owner');s['csrf']='test'
 def save(self,d):return self.c.post(self.url,json=d,headers={'X-HQ-CSRF':'test'})
 def test_owner_csrf_no_store_and_no_provider_calls(self):
  self.assertEqual(self.c.get(self.url).status_code,302)
  self.login();self.assertEqual(self.c.post(self.url,json={}).status_code,403)
  with patch('requests.get') as get,patch('requests.post') as post:
   r=self.c.get(self.url);self.assertEqual(r.status_code,200);self.assertEqual(r.headers['Cache-Control'],'no-store')
   self.assertEqual(self.c.get('/hq/finance/command').status_code,200)
  get.assert_not_called();post.assert_not_called()
 def test_edit_conflict_and_manual_values_never_verify_receipts(self):
  self.login();d=dict(kind='plan',revision=0,balance=250000,balance_date='2026-11-22',target=800000)
  self.assertEqual(self.save(d).status_code,200);self.assertEqual(self.save(d).status_code,409)
  tx=dict(kind='transaction',id='a'*32,revision=0,day='2026-11-22',description='Synthetic transfer',amount=10000,category='internal_transfer',reviewed=True)
  self.assertEqual(self.save(tx).status_code,200);self.assertEqual(self.save(tx).status_code,409)
  r=self.c.get(self.url).json;self.assertEqual(r['transaction_count'],1);self.assertFalse(r['bank_verified']);self.assertIsNone(r['received_income'])
  tx.update(revision=1,category='loan');self.assertEqual(self.save(tx).status_code,200)
 def test_invalid_and_bill_revision(self):
  self.login();b=dict(kind='bill',revision=0,id='b'*32,name='Synthetic bill',amount=10000,due='2026-12-01')
  self.assertEqual(self.save(b).status_code,200);b.update(revision=1,amount=15000);self.assertEqual(self.save(b).status_code,200)
  b['amount']=-1;self.assertEqual(self.save(b).status_code,409)
  b['revision']=2;self.assertEqual(self.save(b).status_code,400)
