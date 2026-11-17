"""Regression checks: internal HQ job cards must search all pages, owner only."""
import os
import tempfile
import unittest
from unittest.mock import patch

from app import app
from customer_request_store import create_request, create_review_draft
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import create_internal_work_record, create_test_job


class JobCardPaginationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ,{
            "HQ_DATA_DIR":tmp.name, "HQ_SESSION_SECRET":"job-card-tests",
            "GOOGLE_CLIENT_ID":"test", "GOOGLE_CLIENT_SECRET":"test",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client=app.test_client()

    def sign_in(self):
        with self.client.session_transaction() as session, auth_db() as db:
            session.update(hq_sid=issue_session(db,"test-owner"),csrf="test")

    def test_job_card_ui_uses_owner_only_filtered_paginated_search(self):
        self.assertEqual(self.client.get("/api/hq/work-records/search").status_code,302)
        self.sign_in()
        html=self.client.get("/").get_data(as_text=True)
        for field in ('id="jobCardSearch"','id="jobCardFilter"',
                      'id="jobCardPrev"','id="jobCardNext"','id="hqJobCards"'):
            self.assertIn(field,html)
        self.assertIn('kind:"job"',html)
        self.assertIn('jobCardLimit=25',html)
        self.assertIn('"/api/hq/work-records/search?"+filters',html)
        self.assertNotIn('const {records}=await assistantTaskApi("/api/hq/work-records")',html)
        self.assertIn('expected_revision:job.revision',html)

    def test_job_cards_can_page_past_100_and_exclude_quotes(self):
        for _ in range(27):
            create_test_job()
        quote_request=create_request({
            "kind":"quote","name":"Quote Only","email":"quote@example.invalid",
            "phone":"07123456789","service":"Wheel Refurbishment",
            "make":"Ford","model":"Fiesta","year":"2019",
            "registration":"AB19 XYZ","date":"","time":"",
            "notes":"","privacy_acknowledged":True,
        },"quote-fingerprint")
        create_review_draft(quote_request,"quote")
        create_internal_work_record(quote_request)
        self.sign_in()
        first=self.client.get("/api/hq/work-records/search",query_string={
            "kind":"job","limit":"25","offset":"0"})
        second=self.client.get("/api/hq/work-records/search",query_string={
            "kind":"job","limit":"25","offset":"25"})
        self.assertEqual(first.status_code,200)
        self.assertEqual(second.status_code,200)
        self.assertEqual((first.json["total"],len(first.json["records"]),first.json["has_more"]),(27,25,True))
        self.assertEqual((second.json["total"],len(second.json["records"]),second.json["has_more"]),(27,2,False))
        self.assertEqual({r["id"] for r in first.json["records"]}.intersection(
            {r["id"] for r in second.json["records"]}),set())
        self.assertTrue(all(row["kind"]=="job" for row in first.json["records"]+second.json["records"]))
        searched=self.client.get("/api/hq/work-records/search",query_string={
            "kind":"job","q":"TEST-ONLY","status":"draft"})
        self.assertEqual(searched.json["total"],27)

if __name__=="__main__":
    unittest.main()
