"""CSRF regression coverage for authenticated enquiry analysis."""
import unittest
from unittest.mock import patch
from app import app

class EnquiryAnalysisCsrfTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client=app.test_client()
        self.auth=patch("hq_auth.configured",return_value=True)
        self.auth.start()
        with self.client.session_transaction() as sess:
            sess["hq_auth"]=True
            sess["csrf"]="test-csrf-token"

    def tearDown(self):
        self.auth.stop()

    def test_missing_token_rejected(self):
        response=self.client.post("/api/hq/enquiry/analyse",json={"description":"Tint my car windows"})
        self.assertEqual(response.status_code,403)

    def test_invalid_token_rejected(self):
        response=self.client.post("/api/hq/enquiry/analyse",json={"description":"Tint my car windows"},headers={"X-HQ-CSRF":"wrong"})
        self.assertEqual(response.status_code,403)

    def test_valid_token_allows_read_only_analysis(self):
        response=self.client.post("/api/hq/enquiry/analyse",json={"description":"Tint my car windows"},headers={"X-HQ-CSRF":"test-csrf-token"})
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.get_json()["requires_owner_approval"])

if __name__=="__main__": unittest.main()
