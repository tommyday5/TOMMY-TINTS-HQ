import os
import tempfile
import unittest
from datetime import date,timedelta
from unittest.mock import patch
from customer_request_store import connect,create_request,create_review_draft,update_request_status
from hq_core_records import create_internal_work_record,list_work_records,set_draft_financials,change_work_status,work_summary,create_test_job,delete_test_job

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
    def test_internal_review_lifecycle_and_summary(self):
        ref=create_request(self.payload,"ip")
        create_review_draft(ref,"quote")
        work=create_internal_work_record(ref)
        with self.assertRaises(ValueError):change_work_status(work,"approved")
        self.assertEqual(change_work_status(work,"ready_for_review"),"ready_for_review")
        with self.assertRaises(ValueError):change_work_status(work,"approved")
        set_draft_financials(work,20000,5000)
        self.assertEqual(change_work_status(work,"approved"),"approved")
        self.assertEqual(change_work_status(work,"approved"),"approved")
        self.assertEqual(create_internal_work_record(ref), work)
        rows = list_work_records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            tuple(rows[0][key] for key in ("id", "kind", "source_request_id", "price_pence",
                                          "deposit_pence", "email", "registration")),
            (work, "job", ref, 20000, 5000, "test@example.org", "AB20 XYZ"),
        )
        with self.assertRaises(LookupError):
            set_draft_financials(work, 21000, 5000)
        self.assertEqual(work_summary()["outstanding_recorded_pence"],15000)
        self.assertFalse(work_summary()["payment_verified"])
        with self.assertRaises(ValueError):change_work_status(work,"draft")
        self.assertEqual(change_work_status(work,"in_progress"),"in_progress")
        self.assertEqual(change_work_status(work,"completed"),"completed")
        with self.assertRaises(ValueError):change_work_status(work,"cancelled")
    def test_legacy_approved_quote_is_promoted_when_work_starts(self):
        ref = create_request(self.payload, "ip")
        create_review_draft(ref, "quote")
        work = create_internal_work_record(ref)
        set_draft_financials(work, 25000, 5000)
        with connect() as db:
            db.execute("UPDATE hq_work_records SET status='approved' WHERE id=?", (work,))
        self.assertEqual(change_work_status(work, "in_progress"), "in_progress")
        row, = list_work_records()
        self.assertEqual((row["id"], row["kind"], row["price_pence"]), (work, "job", 25000))

    def test_synthetic_job_creation_and_cleanup(self):
        work=create_test_job()
        row=next(r for r in list_work_records() if r["id"]==work)
        self.assertTrue(row["source_request_id"].startswith("TEST-"))
        self.assertIn("TEST CUSTOMER",row["name"])
        self.assertEqual(row["status"],"draft")
        set_draft_financials(work,25000,5000)
        change_work_status(work,"ready_for_review")
        change_work_status(work,"approved")
        self.assertTrue(delete_test_job(work))
        self.assertFalse(list_work_records())
    def test_summary_includes_records_beyond_list_page_limit(self):
        # The owner UI intentionally shows only 100 recent rows, but financial
        # totals and status counts must cover every stored record.
        for _ in range(105):
            work = create_test_job()
            set_draft_financials(work, 1000, 200)
        self.assertEqual(len(list_work_records()), 100)
        summary = work_summary()
        self.assertEqual(summary["record_count"], 105)
        self.assertEqual(summary["by_status"]["draft"], 105)
        self.assertEqual(summary["priced_draft_value_pence"], 105000)
        self.assertEqual(summary["outstanding_recorded_pence"], 84000)
        self.assertFalse(summary["payment_verified"])

    def test_real_job_cannot_be_deleted_as_test(self):
        ref=create_request(self.payload,"ip")
        create_review_draft(ref,"job")
        work=create_internal_work_record(ref)
        with self.assertRaises(ValueError):delete_test_job(work)
        self.assertEqual(len(list_work_records()),1)
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
