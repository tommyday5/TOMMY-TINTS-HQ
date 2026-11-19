import os,tempfile,unittest,json
from unittest.mock import patch
import requests
from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
import hq_gmail_enquiries as gmail

def failure(status,body):
 r=requests.Response();r.status_code=status;r._content=json.dumps(body).encode();return requests.HTTPError(response=r)

class GmailConnectionRepairTests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
  p=patch.dict(os.environ,HQ_DATA_DIR=t.name,HQ_SESSION_SECRET='test',GOOGLE_CLIENT_ID='test',GOOGLE_CLIENT_SECRET='test',HQ_ALLOWED_GOOGLE_EMAIL='owner@example.invalid');p.start();self.addCleanup(p.stop)
  self.c=app.test_client()
  with self.c.session_transaction() as s,auth_db() as db:s.update(hq_sid=issue_session(db,'owner'),google_sub='owner')
  self.token={'access_token':'secret-access','refresh_token':'secret-refresh','scope':gmail.SCOPE,'expires_in':3600}
 def callback(self,verify=None,save=True,token=None):
  with patch('app.google.authorize_access_token',return_value=self.token if token is None else token),patch('app.gmail.verify_mailbox',side_effect=verify),patch('app.save_connection',return_value=save):
   return self.c.get('/auth/gmail/callback')
 def test_success_and_failed_persistence(self):
  r=self.callback();self.assertEqual(r.status_code,302);self.assertIn('/hq/gmail-enquiries',r.location)
  with self.c.session_transaction() as s:s.pop('gmail_token_key',None)
  r=self.callback(save=False);self.assertEqual(r.status_code,503)
  with self.c.session_transaction() as s:self.assertNotIn('gmail_token_key',s)
 def test_wrong_scope_account_expiry(self):
  self.assertEqual(self.callback(token={**self.token,'scope':'openid'}).status_code,403)
  self.assertEqual(self.callback(verify=ValueError()).status_code,403)
  self.assertEqual(self.callback(token={**self.token,'expires_in':-1}).status_code,401)
 def test_profile_reason_classification_and_redaction(self):
  for reason,expected in [('SERVICE_DISABLED','api_disabled'),('ACCESS_TOKEN_SCOPE_INSUFFICIENT','scope_insufficient'),('domainPolicy','workspace_policy'),('userRateLimitExceeded','quota_limit'),('secret-new-reason','forbidden_unknown')]:
   error=failure(403,{'error':{'message':'secret-access','errors':[{'reason':reason}],'details':[{'@type':'type.googleapis.com/google.rpc.ErrorInfo','domain':'googleapis.com','reason':reason,'metadata':{'service':'gmail.googleapis.com','consumer':'projects/12345','credential':'secret-refresh'}}]}})
   diagnostic,action=gmail.profile_error_diagnostic(error)
   self.assertIn(expected,diagnostic);self.assertNotIn('secret',diagnostic+action)
   r=self.callback(verify=error);self.assertEqual(r.status_code,502);self.assertNotIn(b'secret',r.data);self.assertEqual(r.headers['Cache-Control'],'no-store')
  self.assertIn('authError',gmail.profile_error_diagnostic(failure(401,{}))[0])
 def test_malformed_error_is_unknown_not_api_disabled(self):
  e=failure(403,{'error':'secret'});diag,action=gmail.profile_error_diagnostic(e);self.assertIn('forbidden_unknown',diag)
 def test_owner_only(self):
  with self.c.session_transaction() as s:s.clear();s['customer_id']='retail'
  self.assertEqual(self.c.get('/auth/gmail/connect').status_code,302)
  self.assertEqual(self.c.get('/auth/gmail/callback').status_code,302)

 def test_setup_owner_only_and_secret_safe(self):
  with patch.dict(os.environ,GOOGLE_CLIENT_ID='123-publicid.apps.googleusercontent.com',GOOGLE_CLIENT_SECRET='never-show-secret'):
   r=self.c.get('/hq/gmail-setup');self.assertEqual(r.status_code,200);self.assertIn(b'123-publicid.apps.googleusercontent.com',r.data);self.assertNotIn(b'never-show-secret',r.data);self.assertEqual(r.headers['Cache-Control'],'no-store')
  with self.c.session_transaction() as session:session.clear()
  self.assertEqual(self.c.get('/hq/gmail-setup').status_code,302)

 def test_import_failure_actions_are_redacted_and_owner_only(self):
  cases=[(gmail.EnquiryLabelError('secret-mailbox-content'),409,b'HQ-Enquiries'),
         (failure(403,{'error':{'errors':[{'reason':'insufficientPermissions'}],'message':'secret-mailbox-content'}}),502,b'grant gmail.readonly'),
         (requests.Timeout('secret-mailbox-content'),502,b'could not be reached'),
         (RuntimeError('secret-mailbox-content'),502,b'response validation')]
  for error,status,expected in cases:
   with self.c.session_transaction() as s:s['csrf']='synthetic-csrf'
   with patch('app._gmail_token',return_value='synthetic'),patch('app.gmail.import_page',side_effect=error),patch('app.record_check') as record:
    r=self.c.post('/hq/gmail-enquiries',data={'csrf':'synthetic-csrf'})
   self.assertEqual(r.status_code,status);self.assertIn(expected,r.data)
   self.assertNotIn(b'secret-mailbox-content',r.data)
   self.assertNotIn('secret-mailbox-content',str(record.call_args))
   self.assertEqual(r.headers['Cache-Control'],'no-store')
  with self.c.session_transaction() as s:s.clear();s['customer_id']='retail'
  with patch('app.gmail.import_page') as importer:
   self.assertEqual(self.c.post('/hq/gmail-enquiries').status_code,302)
   importer.assert_not_called()

 def test_missing_label_never_reads_messages_or_changes_labels(self):
  with patch.object(gmail,'storage_ready',return_value=True),patch.object(gmail,'_get',side_effect=[{'emailAddress':gmail.MAILBOX},{'labels':[]}]) as api:
   with self.assertRaises(gmail.EnquiryLabelError):gmail.import_page('synthetic')
  self.assertEqual([call.args[1] for call in api.call_args_list],['/profile','/labels'])
