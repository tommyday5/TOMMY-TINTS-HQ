import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from app import app
import hq_gmail_enquiries as gmail
from maintenance_calendar_google import live_conflicts


class GmailTests(unittest.TestCase):
    def setUp(self):
        db = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(db.cleanup)
        env = patch.dict(os.environ, HQ_DATA_DIR=db.name, HQ_SESSION_SECRET="test",
                         GOOGLE_CLIENT_ID="test", GOOGLE_CLIENT_SECRET="test",
                         HQ_ALLOWED_GOOGLE_EMAIL="owner@example.invalid")
        env.start()
        self.addCleanup(env.stop)
        self.responses = [{"emailAddress": gmail.MAILBOX}, {"messages": [{"id": "abc"}]},
                          {"id": "abc", "snippet": "<script>ignored</script>", "payload": {"headers": []}}]

    def test_idempotent_retry_and_persistent_review(self):
        with patch.object(gmail, "_get", side_effect=self.responses * 3) as api:
            self.assertEqual(gmail.import_page("token")["added"], 1)
            self.assertEqual(api.call_args_list[1].kwargs["q"], "in:inbox label:HQ-Enquiries")
            self.assertEqual(gmail.import_page("token", "cursor")["added"], 0)
            self.assertEqual(api.call_args_list[4].kwargs["pageToken"], "cursor")
            self.assertEqual(gmail.queue()[0], 1)
            self.assertTrue(gmail.review("abc"))
            self.assertEqual(gmail.import_page("token")["added"], 0)
            self.assertEqual(gmail.queue()[0], 0)

    def test_wrong_mailbox_or_failed_read_never_writes_a_partial_batch(self):
        for responses in ([{"emailAddress": "wrong@example.invalid"}],
                          self.responses[:2] + [RuntimeError("provider failed")]):
            with patch.object(gmail, "_get", side_effect=responses):
                with self.assertRaises((ValueError, RuntimeError)):
                    gmail.import_page("token")
        self.assertFalse(gmail._path().exists())

    def test_owner_csrf_preview_and_verified_dashboard(self):
        url = "/hq/gmail-enquiries"
        with app.test_client() as client:
            self.assertEqual(client.get(url).status_code, 302)
            with client.session_transaction() as session:
                session.update(hq_auth=True, csrf="csrf", gmail_token_key="key")
            self.assertEqual(client.post(url).status_code, 403)
            self.assertEqual(client.post(url, data={"csrf": "csrf"}).status_code, 401)
            with patch("app._gmail_tokens", {"key": ("token", time.time() + 60)}):
                with patch.object(gmail, "_get", side_effect=self.responses):
                    result = client.post(url, data={"csrf": "csrf"})
                self.assertIn(b"&lt;script&gt;", result.data)
                status = next(item for item in client.get("/api/hq/integrations").json["integrations"]
                              if item["name"] == "Gmail")
                self.assertEqual((status["status"], status["pending_records"]), ("Last read succeeded", 1))

    def test_unconfigured_storage_prevents_mail_reads(self):
        with patch.dict(os.environ, HQ_DATA_DIR="/tmp/test"), patch.object(gmail, "_get") as api:
            with self.assertRaises(RuntimeError):
                gmail.import_page("token")
            api.assert_not_called()


class CalendarProviderTests(unittest.TestCase):
    def check(self, payload):
        with patch("maintenance_calendar_google.requests.get", return_value=Mock(
            json=Mock(return_value=payload),
        )):
            return live_conflicts("token", "2026-07-10T23:10:00Z", "2026-07-10T23:30:00Z")

    def test_all_day_event_blocks_at_local_midnight(self):
        result = self.check({"timeZone": "Europe/London", "items": [
            {"start": {"date": "2026-07-11"}, "end": {"date": "2026-07-12"}},
        ]})
        self.assertFalse(result["available"])
        self.assertFalse(result["booking_created"])

    def test_transparent_events_are_ignored(self):
        self.assertTrue(self.check({"items": [{"transparency": "transparent"}]})["available"])

    def test_incomplete_or_unparseable_provider_response_fails_closed(self):
        for payload in ({}, {"items": [], "nextPageToken": "next"},
                        {"items": [{"start": {"date": "2026-07-11"}, "end": {"date": "2026-07-12"}}]}):
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                self.check(payload)
