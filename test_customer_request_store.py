import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from datetime import date, timedelta
from customer_request_store import connect,create_request,list_requests,update_request_status,storage_ready,create_review_draft,list_review_drafts

class CustomerRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{"HQ_DATA_DIR":self.tmp.name})
        self.env.start()
        self.payload=dict(kind="booking",name="Example Customer",email="customer@example.org",phone="07123456789",service="Window Tinting",make="BMW",model="320d",year="2019",registration="AB12 CDE",date=(date.today()+timedelta(days=2)).isoformat(),time="10:00",notes="Please call",privacy_acknowledged=True)
    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
    def test_storage_readiness_requires_explicit_path(self):
        with patch.dict(os.environ,{"HQ_DATA_DIR":"/mnt/data/hq"}):
            self.assertTrue(storage_ready())
        with patch.dict(os.environ,{"HQ_DATA_DIR":"/tmp/tommy-tints-hq"}):
            self.assertFalse(storage_ready())
        with patch.dict(os.environ,{"HQ_DATA_DIR":""}):
            self.assertFalse(storage_ready())
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
    def test_review_draft_idempotency(self):
        ref=create_request(self.payload,"test-ip")
        first=create_review_draft(ref,"quote")
        second=create_review_draft(ref,"quote")
        self.assertEqual(first["id"],second["id"])
        self.assertEqual(list_review_drafts()[0]["email"],"customer@example.org")
        with self.assertRaises(ValueError):create_review_draft(ref,"job")
    def test_review_draft_rejects_malformed_inputs(self):
        ref=create_request(self.payload,"test-ip")
        for identifier in (None,123,[],{}, "x"*65, ""):
            with self.subTest(identifier=repr(identifier)):
                with self.assertRaises(ValueError):create_review_draft(identifier,"quote")
        for draft_type in (None,123,[],{}, "invoice"):
            with self.subTest(draft_type=repr(draft_type)):
                with self.assertRaises(ValueError):create_review_draft(ref,draft_type)
        self.assertEqual(list_review_drafts(),[])

    def test_closed_request_cannot_be_converted(self):
        ref=create_request(self.payload,"test-ip")
        update_request_status(ref,"closed")
        with self.assertRaises(ValueError):create_review_draft(ref,"job")
    def test_rate_limit(self):
        for _ in range(3):create_request(self.payload,"test-ip")
        with self.assertRaises(ValueError):create_request(self.payload,"test-ip")
    def test_simultaneous_public_requests_obey_the_hourly_limit(self):
        # The real endpoint can be served by separate threads/processes;
        # a SELECT followed by an INSERT must be one locked transaction.
        with connect():
            pass
        gate = Barrier(8)
        def submit(_):
            gate.wait(timeout=10)
            try:
                create_request(self.payload, "shared-ip")
                return "accepted"
            except ValueError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(8)))
        self.assertEqual(results.count("accepted"), 3)
        self.assertEqual(sum("Too many requests" in result for result in results), 5)
        self.assertEqual(len(list_requests()), 3)
        # A separate fingerprint remains independently allowed.
        self.assertTrue(create_request(self.payload, "other-ip"))

    def test_simultaneous_draft_retries_return_one_record(self):
        ref = create_request(self.payload, "test-ip")
        gate = Barrier(8)
        def submit(_):
            gate.wait(timeout=10)
            return create_review_draft(ref, "quote")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(8)))
        self.assertEqual(len({item["id"] for item in results}), 1)
        self.assertEqual(len(list_review_drafts()), 1)

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
