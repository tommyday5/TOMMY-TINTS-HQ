import os,tempfile,unittest
from unittest.mock import patch
from customer_request_store import connect
from hq_core_records import create_test_job,set_draft_financials,read_work_record,change_work_status
from hq_job_sync import status,enqueue
from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db

class JobSyncTests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
  p=patch.dict(os.environ,HQ_DATA_DIR=t.name,HQ_SESSION_SECRET='test',GOOGLE_CLIENT_ID='test',GOOGLE_CLIENT_SECRET='test',HQ_ALLOWED_GOOGLE_EMAIL='owner@example.invalid');p.start();self.addCleanup(p.stop)
 def work(self):
  ref=create_test_job()
  with connect() as db:db.execute('UPDATE hq_work_records SET source_request_id=? WHERE id=?',('synthetic-request',ref))
  return ref
 def test_atomic_updates_coalesce_and_survive_reopen(self):
  ref=self.work();set_draft_financials(ref,20000,5000,0)
  self.assertEqual(status(ref)['desired_revision'],1)
  change_work_status(ref,'ready_for_review',1)
  self.assertEqual(status(ref)['desired_revision'],2)
  with connect() as db:enqueue(db,ref,1)
  self.assertEqual(status(ref)['desired_revision'],2)
  self.assertFalse(status(ref)['live_sync']);self.assertEqual(status(ref)['state'],'pending')
 def test_outbox_failure_rolls_back_financial_change(self):
  ref=self.work()
  with patch('hq_core_records.enqueue_sheet_sync',side_effect=RuntimeError('storage')):
   with self.assertRaises(RuntimeError):set_draft_financials(ref,20000,0,0)
  self.assertIsNone(read_work_record(ref)['price_pence']);self.assertEqual(read_work_record(ref)['revision'],0)
 def test_test_records_never_queued_and_payments_not_implied(self):
  ref=create_test_job();set_draft_financials(ref,20000,5000,0)
  self.assertEqual(status(ref)['state'],'not_linked')
  with self.assertRaises(ValueError):status('J0077')
 def test_new_revision_resets_only_superseded_retry_attempts(self):
  ref=self.work();set_draft_financials(ref,20000,5000,0)
  with connect() as db:
   db.execute("UPDATE hq_job_sync_outbox SET state='failed',attempts=7,last_error='safe failure' WHERE work_id=?",(ref,))
   enqueue(db,ref,1) # same revision replay must not erase failure evidence
  self.assertEqual(status(ref)['state'],'failed')
  self.assertEqual(status(ref)['attempts'],7)
  change_work_status(ref,'ready_for_review',1)
  state=status(ref)
  self.assertEqual(state['desired_revision'],2)
  self.assertEqual(state['state'],'pending')
  self.assertEqual(state['attempts'],0)
  self.assertIsNone(state['last_error'])

 def test_future_invalid_and_stale_revisions_do_not_corrupt_queue(self):
  ref=self.work();set_draft_financials(ref,20000,5000,0)
  with connect() as db:
   for value in (-1,True,1.0,2,2**63):
    with self.assertRaises(ValueError):enqueue(db,ref,value)
   with self.assertRaises(ValueError):enqueue(db,'not-a-uuid',1)
  self.assertEqual(status(ref)['desired_revision'],1)
  change_work_status(ref,'ready_for_review',1)
  with connect() as db:
   enqueue(db,ref,1) # stale delayed delivery is ignored
  self.assertEqual(status(ref)['desired_revision'],2)

 def test_owner_only_status_no_remote_calls(self):
  ref=self.work();c=app.test_client();url='/api/hq/work-records/'+ref+'/sheet-sync'
  self.assertEqual(c.get(url).status_code,302)
  with c.session_transaction() as s,auth_db() as db:s['hq_sid']=issue_session(db,'owner')
  with patch('requests.get') as get,patch('requests.post') as post:
   r=c.get(url)
  self.assertEqual(r.status_code,200);self.assertEqual(r.headers['Cache-Control'],'no-store')
  get.assert_not_called();post.assert_not_called()
