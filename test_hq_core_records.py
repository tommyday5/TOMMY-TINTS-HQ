import os
import tempfile
import unittest
from datetime import date,timedelta
from unittest.mock import patch
from customer_request_store import create_request,create_review_draft,update_request_status
from hq_core_records import create_internal_work_record,list_work_records,set_draft_financials

class CoreRecordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{"HQ_DATA_DIR":self.tmp.name})
        self.env.start()
        self.payload=dict(kind="booking",name="Test Customer",email="test@example.org",phone="07123456789",
            service="Window Tinting",make="Audi",model="A4",year="2020",registration="AB20 XYZ",
            date=(date.today()+timedelta(days=2)).isoformat(),time="10:00",notes="Rear windows",
            privacy_acknowledged=True)
    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
    def test_conversion_is_idempotent_and_linked(self):
        ref=create_request(self.payload,"ip")
        with self.assertRaises(ValueError): create_internal_work_record(ref)
        create_review_draft(ref,"quote")
        first=create_internal_work_record(ref)
        self.assertEqual(first,create_internal_work_record(ref))
        rows=list_work_records()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["email"],"test@example.org")
        self.assertEqual(rows[0]["registration"],"AB20 XYZ")
        self.assertEqual(rows[0]["status"],"draft")
        self.assertEqual(rows[0]["source_request_id"],ref)
        amounts=set_draft_financials(first,25000,7600)
        self.assertEqual(amounts["balance_pence"],17400)
        with self.assertRaises(ValueError):set_draft_financials(first,1000,2000)
        self.assertEqual(list_work_records()[0]["price_pence"],25000)
    def test_closed_request_not_converted(self):
        ref=create_request(self.payload,"ip")
        create_review_draft(ref,"job")
        update_request_status(ref,"closed")
        with self.assertRaises(ValueError):create_internal_work_record(ref)
    def test_reject_boolean_amounts(self):
        ref=create_request(self.payload,"ip")
        create_review_draft(ref,"job")
        work=create_internal_work_record(ref)
        with self.assertRaises(ValueError):set_draft_financials(work,True,0)

if __name__=="__main__": unittest.main()
