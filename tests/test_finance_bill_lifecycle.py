"""Synthetic owner-only bill lifecycle, migration and audit regression tests."""
import os
import tempfile
import unittest
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db

class FinanceBillLifecycleTests(unittest.TestCase):
 def setUp(self):
  tmp=tempfile.TemporaryDirectory()
  self.addCleanup(tmp.cleanup)
  env=patch.dict(os.environ,HQ_DATA_DIR=tmp.name,HQ_SESSION_SECRET='test',
      GOOGLE_CLIENT_ID='test',GOOGLE_CLIENT_SECRET='test',
      HQ_ALLOWED_GOOGLE_EMAIL='owner@example.invalid')
  env.start()
  self.addCleanup(env.stop)
  self.client=app.test_client()
  self.url='/api/hq/finance/native'

 def login(self):
  with self.client.session_transaction() as s,auth_db() as db:
   s['hq_sid']=issue_session(db,'owner')
   s['csrf']='test'

 def save(self,payload):
  return self.client.post(self.url,json=payload,headers={'X-HQ-CSRF':'test'})

 def bill(self,**updates):
  data=dict(kind='bill',revision=0,id='b'*32,name='Synthetic insurer',
      amount=12000,due='2026-12-01',status='planned',archived=False)
  data.update(updates)
  return data

 def test_create_mark_paid_archive_restore_and_history(self):
  self.login()
  payload=self.bill()
  self.assertEqual(self.save(payload).status_code,200)
  payload.update(revision=1,status='reported_paid',status_date='2026-11-23')
  self.assertEqual(self.save(payload).status_code,200)
  payload.update(revision=2,archived=True)
  self.assertEqual(self.save(payload).status_code,200)
  payload.update(revision=3,archived=False,status='planned',status_date='')
  self.assertEqual(self.save(payload).status_code,200)
  snapshot=self.client.get(self.url)
  self.assertEqual(snapshot.status_code,200)
  self.assertEqual(snapshot.headers['Cache-Control'],'no-store')
  saved=snapshot.json
  self.assertFalse(saved['bank_verified'])
  self.assertIsNone(saved['received_income'])
  self.assertEqual(len(saved['bills']),1)
  self.assertEqual(saved['bills'][0]['revision'],4)
  self.assertEqual(saved['bills'][0]['status'],'planned')
  self.assertFalse(saved['bills'][0]['archived'])
  events=[r for r in saved['bill_history'] if r['bill_id']=='b'*32]
  self.assertEqual([r['revision'] for r in events],[4,3,2,1])
  self.assertEqual(events[2]['snapshot']['status'],'reported_paid')
  self.assertEqual(events[1]['snapshot']['archived'],True)
  self.assertEqual(events[0]['snapshot']['amount'],12000)
  # Same persistent database, independently reopened.
  with connect() as db:
   self.assertEqual(db.execute('SELECT COUNT(*) FROM finance_bill_history').fetchone()[0],4)
  self.assertEqual(self.save(payload).status_code,409)

 def test_invalid_status_date_archive_flag_and_csrf(self):
  self.login()
  payload=self.bill(status='reported_paid')
  self.assertEqual(self.save(payload).status_code,400)
  self.assertEqual(self.save(self.bill(status='not_paid')).status_code,400)
  self.assertEqual(self.save(self.bill(archived='yes')).status_code,400)
  self.assertEqual(self.client.post(self.url,json=self.bill()).status_code,403)
  self.assertEqual(self.save(self.bill()).status_code,200)
  self.assertEqual(self.save(self.bill(revision=1,status='reported_paid',status_date='2026-12-99')).status_code,400)
  self.assertEqual(self.client.get('/hq/finance/command').status_code,200)
  html=self.client.get('/hq/finance/command').get_data(as_text=True)
  self.assertIn('name="status"',html)
  self.assertIn('name="archived"',html)

 def test_old_rows_survive_additive_schema_upgrade(self):
  with connect() as db:
   db.execute('CREATE TABLE finance_bills (id TEXT PRIMARY KEY, name TEXT NOT NULL, amount INTEGER NOT NULL, due TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0)')
   db.execute('INSERT INTO finance_bills VALUES(?,?,?,?,?)',('a'*32,'Old synthetic bill',34567,'2026-11-30',7))
  self.login()
  old=self.client.get(self.url).json['bills'][0]
  self.assertEqual((old['name'],old['amount'],old['revision']),('Old synthetic bill',34567,7))
  self.assertEqual(old['status'],'planned')
  self.assertEqual(old['archived'],0)
  edit=self.bill(id='a'*32,name=old['name'],amount=old['amount'],due=old['due'],revision=7)
  self.assertEqual(self.save(edit).status_code,200)
  self.assertEqual(self.client.get(self.url).json['bills'][0]['revision'],8)

if __name__=='__main__':
 unittest.main()
