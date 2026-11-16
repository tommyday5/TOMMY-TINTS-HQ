import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import date, timedelta
from customer_request_store import create_request,list_requests,update_request_status

class CustomerRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{"HQ_DATA_DIR":self.tmp.name})
        self.env.start()
        self.payload=dict(kind="booking",name="Example Customer",email="customer@example.org",phone="07123456789",service="Window Tinting",make="BMW",model="320d",year="2019",registration="AB12 CDE",date=(date.today()+timedelta(days=2)).isoformat(),time="10:00",notes="Please call",privacy_acknowledged=True)
    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
    def test_store_unconfirmed_request(self):
        ref=create_request(self.payload,"test-ip")
        rows=list_requests()
        self.assertEqual(rows[0]["id"],ref)
        self.assertEqual(rows[0]["status"],"new")
        self.assertEqual(rows[0]["email"],"customer@example.org")
    def test_status_review_without_external_actions(self):
        ref=create_request(self.payload,"test-ip")
        self.assertEqual(update_request_status(ref,"reviewing"),"reviewing")
        self.assertEqual(list_requests()[0]["status"],"reviewing")
        with self.assertRaises(ValueError):update_request_status(ref,"confirmed")
        with self.assertRaises(LookupError):update_request_status("missing","closed")
    def test_terminal_status_cannot_reopen(self):
        ref=create_request(self.payload,"test-ip")
        self.assertEqual(update_request_status(ref,"closed"),"closed")
        self.assertEqual(update_request_status(ref,"closed"),"closed")
        with self.assertRaisesRegex(ValueError,"cannot be reopened"):
            update_request_status(ref,"new")
        self.assertEqual(list_requests()[0]["status"],"closed")
    def test_invalid_identifier_and_status_type(self):
        ref=create_request(self.payload,"test-ip")
        for identifier in (None,123,"x"*65):
            with self.assertRaises(ValueError):update_request_status(identifier,"reviewing")
        for status in (None,123,[],{}):
            with self.assertRaises(ValueError):update_request_status(ref,status)
        self.assertEqual(list_requests()[0]["status"],"new")
    def test_rate_limit(self):
        for _ in range(3):create_request(self.payload,"test-ip")
        with self.assertRaises(ValueError):create_request(self.payload,"test-ip")
    def test_privacy_ack_required(self):
        self.payload["privacy_acknowledged"]=False
        with self.assertRaises(ValueError):create_request(self.payload,"test-ip")
    def test_quote_without_date(self):
        self.payload.update(kind="quote",date="",time="")
        self.assertTrue(create_request(self.payload,"test-ip"))
    def test_honeypot(self):
        self.payload["website"]="spam"
        with self.assertRaises(ValueError):create_request(self.payload,"test-ip")

if __name__=="__main__":unittest.main()
