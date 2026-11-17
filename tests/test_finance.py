import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_finance import finance_snapshot, FIELDS, RANGES

class FinanceTests(unittest.TestCase):
    def test_mapping_missing_and_errors_never_become_zero(self):
        s=finance_snapshot({})
        self.assertTrue(s['stale'])
        self.assertFalse(s['bank_verified'])
        self.assertTrue(all(m['pence'] is None for m in s['metrics']))
        self.assertIn("'Cash Engine'!E12",RANGES)
        self.assertIn("'HQ Dashboard'!F66",RANGES)
    def test_effective_results_preserve_formula_currency_and_date(self):
        payload={'sheets':[{'properties':{'title':'Cash Engine'},'data':[
          {'startRow':11,'startColumn':4,'rowData':[{'values':[{'effectiveValue':{'numberValue':-1234.567},'formattedValue':'-£1,234.57','userEnteredValue':{'formulaValue':'=B2'}}]}]},
          {'startRow':3,'startColumn':1,'rowData':[{'values':[{'effectiveValue':{'numberValue':46343}}]}]},
          {'startRow':3,'startColumn':7,'rowData':[{'values':[{'formattedValue':'CURRENT'}]}]}]}]}
        payload['sheets'].append({'properties':{'title':'Settings'},'data':[{'startRow':35,'startColumn':1,'rowData':[{'values':[{'effectiveValue':{'numberValue':3}}]}]}]})
        s=finance_snapshot(payload,datetime(2026,11,17,tzinfo=timezone.utc))
        self.assertEqual(s['metrics'][0]['pence'],-123457)
        self.assertEqual(s['metrics'][0]['formula'],'=B2')
        self.assertEqual(s['bank_data_date'],'2026-11-17')
        self.assertFalse(s['stale'])
        self.assertTrue(finance_snapshot(payload,datetime(2026,11,22,tzinfo=timezone.utc))['stale'])
    def test_owner_required_and_no_store(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,HQ_DATA_DIR=tmp,HQ_SESSION_SECRET='test',GOOGLE_CLIENT_ID='test',GOOGLE_CLIENT_SECRET='test',HQ_ALLOWED_GOOGLE_EMAIL='owner@example.invalid'):
            client=app.test_client()
            for url in ('/hq/finance','/api/hq/finance'):
                r=client.get(url);self.assertEqual(r.status_code,302);self.assertEqual(r.headers['Cache-Control'],'no-store')
            # Customer cookies are not an owner server session.
            with client.session_transaction() as s:s['customer_id']='retail-customer'
            self.assertEqual(client.get('/api/hq/finance').status_code,302)
            with client.session_transaction() as s, auth_db() as db:s['hq_sid']=issue_session(db,'owner')
            with patch('app._google_connection',return_value=('token',9999999999)),patch('hq_sheets_live.read_finance',return_value=finance_snapshot({})) as read:
                r=client.get('/api/hq/finance');self.assertEqual(r.status_code,200);self.assertEqual(r.headers['Cache-Control'],'no-store');read.assert_called_once_with('token')
                page=client.get('/hq/finance');self.assertEqual(page.status_code,200);self.assertNotIn(b'1234.57',page.data)
            with patch('app._google_connection',return_value=('token',9999999999)),patch('hq_sheets_live.read_finance',side_effect=RuntimeError('secret')):
                r=client.get('/api/hq/finance');self.assertEqual(r.status_code,502);self.assertNotIn(b'secret',r.data)
    def test_connector_read_only_and_bounded(self):
        from hq_sheets_live import read_finance
        with patch('hq_sheets_live.requests.get') as get:
            get.return_value.json.return_value={}
            read_finance('authorised')
            args=get.call_args.kwargs
            self.assertEqual(args['timeout'],15)
            self.assertEqual([v for k,v in args['params'] if k=='ranges'],RANGES)
