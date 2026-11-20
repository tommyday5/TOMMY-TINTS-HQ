"""Synthetic Gmail intake isolation: never store obvious legal/debt mail as sales leads."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hq_gmail_enquiries as gmail


def message(ref, sender, subject, snippet, labels=None):
    return {
        "id": ref,
        "labelIds": ["INBOX", "Label_Enquiries"] if labels is None else labels,
        "snippet": snippet,
        "payload": {"headers": [
            {"name": "From", "value": sender},
            {"name": "Subject", "value": subject},
        ]},
    }


class SensitiveGmailEnquiryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"HQ_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def import_messages(self, messages):
        ids = [{"id": item["id"]} for item in messages]
        responses = [
            {"emailAddress": gmail.MAILBOX},
            {"labels": [
                {"name": "HQ-Enquiries", "id": "Label_Enquiries"},
                {"name": "HQ-Legal", "id": "Label_Legal"},
                {"name": "HQ-Debt", "id": "Label_Debt"},
            ]},
            {"messages": ids}, *messages,
        ]
        with patch.object(gmail, "_get", side_effect=responses) as api:
            result = gmail.import_page("synthetic-token")
        self.assertEqual(
            api.call_args_list[2].kwargs["q"],
            "in:inbox label:HQ-Enquiries -label:HQ-Legal -label:HQ-Debt",
        )
        self.assertIn("labelIds", api.call_args_list[3].kwargs["fields"] if messages else "labelIds")
        return result

    def test_label_changed_after_search_is_excluded_even_with_innocent_subject(self):
        result = self.import_messages([
            message("valid", "customer@example.invalid", "Window tint enquiry",
                    "Can you book me in?"),
            message("legal", "person@example.invalid", "General message",
                    "Please see attached.", ["INBOX", "Label_Enquiries", "Label_Legal"]),
            message("debt", "person@example.invalid", "Hello",
                    "Just a reminder.", ["INBOX", "Label_Enquiries", "Label_Debt"]),
            message("unlabelled", "person@example.invalid", "Quote",
                    "Tint quote", ["INBOX"]),
            message("archived", "person@example.invalid", "Quote",
                    "Tint quote", ["Label_Enquiries"]),
        ])
        self.assertEqual(result["read"], 5)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped_sensitive"], 0)
        self.assertEqual(result["skipped_label_mismatch"], 4)
        self.assertEqual([row["id"] for row in gmail.queue()[1]], ["valid"])

    def test_missing_message_labels_fails_closed_without_partial_queue_write(self):
        rows = [message("good", "customer@example.invalid", "Tint quote", "Hello"),
                message("missing", "customer@example.invalid", "Quote", "Hello")]
        del rows[1]["labelIds"]
        with self.assertRaisesRegex(RuntimeError, "labels cannot be verified"):
            self.import_messages(rows)
        self.assertFalse(gmail._path().exists())

    def test_ambiguous_or_missing_sender_aborts_entire_page_without_storing_mail(self):
        good = message("good", "customer@example.invalid", "Tint quote", "Please quote")
        no_from = message("bad", "customer@example.invalid", "Quote", "Hello")
        no_from["payload"]["headers"] = [
            h for h in no_from["payload"]["headers"] if h["name"] != "From"]
        duplicate = message("bad", "customer@example.invalid", "Quote", "Hello")
        duplicate["payload"]["headers"].append(
            {"name": "from", "value": "legal@example.invalid"})
        scenarios = [
            no_from,
            duplicate,
            message("bad", "", "Quote", "Hello"),
            message("bad", "not-an-email", "Quote", "Hello"),
            message("bad", "customer@example.invalid, legal@example.invalid", "Quote", "Hello"),
        ]
        for bad in scenarios:
            with self.subTest(headers=bad["payload"]["headers"]):
                with self.assertRaisesRegex(RuntimeError, "sender cannot be verified"):
                    self.import_messages([good, bad])
                self.assertFalse(gmail._path().exists())

    def test_bounded_header_metadata_fails_closed(self):
        good = message("good", "customer@example.invalid", "Tint quote", "Hello")
        bad = message("bad", "customer@example.invalid", "Quote", "Hello")
        bad["payload"]["headers"].append({"name": "X-Untrusted", "value": "x" * 16385})
        with self.assertRaisesRegex(RuntimeError, "headers cannot be verified"):
            self.import_messages([good, bad])
        self.assertFalse(gmail._path().exists())

    def test_missing_enquiry_label_fails_closed_before_message_search(self):
        responses = [
            {"emailAddress": gmail.MAILBOX},
            {"labels": [{"name": "HQ-Legal", "id": "Label_Legal"}]},
        ]
        with patch.object(gmail, "_get", side_effect=responses) as api:
            with self.assertRaisesRegex(RuntimeError, "enquiry label"):
                gmail.import_page("synthetic-token")
        self.assertEqual(api.call_count, 2)
        self.assertFalse(gmail._path().exists())

    def test_mixed_labelled_batch_keeps_legal_and_debt_out_of_sales_queue(self):
        result = self.import_messages([
            message("a", "customer@example.invalid", "Rear window tints",
                    "Could you quote a set of privacy tints for my car?"),
            message("b", "notice@example.invalid", "Notice of enforcement",
                    "Account reference and payment instructions."),
            message("c", "collections@example.invalid", "Business account",
                    "Your correspondence has been received."),
            message("d", "news@sub.hmrc.gov.uk", "Important account message",
                    "Please read the attachment."),
        ])
        self.assertEqual(result["read"], 4)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped_sensitive"], 3)
        count, queued = gmail.queue()
        self.assertEqual(count, 1)
        self.assertEqual([row["id"] for row in queued], ["a"])
        self.assertNotIn("enforcement", str([dict(row) for row in queued]).lower())

    def test_no_sensitive_content_is_written_even_when_label_was_applied(self):
        result = self.import_messages([
            message("legal1", "alerts@example.invalid", "County Court Judgment",
                    "An outstanding claim."),
            message("legal2", "reminders@example.invalid", "Parking Charge Notice",
                    "Notice of debt recovery."),
        ])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped_sensitive"], 2)
        count, queued = gmail.queue()
        self.assertEqual((count, queued), (0, []))
        # Retrying a wrongly labelled mail does not promote it to a lead.
        self.assertEqual(self.import_messages([
            message("legal1", "alerts@example.invalid", "County Court Judgment",
                    "An outstanding claim."),
        ])["added"], 0)

    def test_ordinary_price_and_booking_enquiries_are_not_suppressed(self):
        result = self.import_messages([
            message("safe", "person@example.invalid", "Hyundai bonnet repair quote",
                    "Hello, how much would it cost to paint the bonnet?"),
            message("safe2", "client@example.invalid", "Window tint booking",
                    "Could I book a trade tinting appointment?"),
        ])
        self.assertEqual(result["skipped_sensitive"], 0)
        self.assertEqual(result["added"], 2)
        self.assertEqual(gmail.queue()[0], 2)

    def test_sensitive_sender_domains_and_terms_are_case_insensitive(self):
        for sender, subject, snippet in (
            ("notice@Justice.Gov.Uk", "Update", ""),
            ("legal@example.invalid", "Update", ""),
            ("debt+cases@example.invalid", "Update", ""),
            ("person@example.invalid", "Court claim form", ""),
            ("person@example.invalid", "Update", "N244 court application"),
            ("person@example.invalid", "Update", "Fixed Penalty Notice"),
        ):
            with self.subTest(sender=sender, subject=subject):
                self.assertTrue(gmail._excluded_from_sales(sender, subject, snippet))
        self.assertFalse(gmail._excluded_from_sales(
            "trade@example.invalid", "Window tint quote",
            "Can you tint four vehicle windows?"
        ))


if __name__ == "__main__":
    unittest.main()
