import os
import tempfile
import unittest
from unittest.mock import patch
from hq_maps_usage import reserve_route_request
from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db

class MapsUsageTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        p=patch.dict(os.environ,HQ_DATA_DIR=t.name,HQ_SESSION_SECRET='synthetic',GOOGLE_CLIENT_ID='test',GOOGLE_CLIENT_SECRET='test',HQ_ALLOWED_GOOGLE_EMAIL='owner@example.invalid');p.start();self.addCleanup(p.stop)

    def test_shared_durable_hourly_and_daily_ceiling(self):
        with patch('hq_maps_usage.time.time',return_value=100000):
            self.assertTrue(all(reserve_route_request() for _ in range(20)))
            self.assertFalse(reserve_route_request())
        for hour in range(1,5):
            with patch('hq_maps_usage.time.time',return_value=100000+hour*3601):
                self.assertTrue(all(reserve_route_request() for _ in range(20)))
        with patch('hq_maps_usage.time.time',return_value=120000):
            self.assertFalse(reserve_route_request())
        with patch('hq_maps_usage.time.time',return_value=200000):
            self.assertTrue(reserve_route_request())

    def test_matrix_csrf_and_no_secret_response(self):
        c=app.test_client();url='/api/hq/maintenance/driving-matrix'
        self.assertEqual(c.post(url,json={}).status_code,302)
        with c.session_transaction() as s,auth_db() as db:
            s['hq_sid']=issue_session(db,'owner');s['csrf']='synthetic'
        with patch('app.driving_matrix') as provider:
            self.assertEqual(c.post(url,json={}).status_code,403)
            self.assertEqual(c.post(url,json={'addresses':[]},headers={'X-HQ-CSRF':'synthetic'}).status_code,400)
            provider.assert_not_called()
        with patch.dict(os.environ,HQ_GOOGLE_MAPS_API_KEY='server-secret'),patch('app.reserve_route_request',return_value=False),patch('app.driving_matrix') as provider:
            r=c.post(url,json={'addresses':['1 Synthetic Road AB1 2CD','2 Synthetic Road AB1 2CD']},headers={'X-HQ-CSRF':'synthetic'})
            self.assertEqual(r.status_code,429);self.assertNotIn(b'server-secret',r.data)
            self.assertEqual(r.headers['Cache-Control'],'no-store');provider.assert_not_called()
