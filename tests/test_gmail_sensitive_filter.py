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

    def test_encoded_sensitive_subject_is_filtered_before_sales_storage(self):
        encoded = message(
            "encoded-legal", "neutral@example.invalid",
            "=?utf-8?Q?County_Court_Judgment?=",
            "Please see the attached documents.")
        safe = message(
            "encoded-customer", "customer@example.invalid",
            "=?utf-8?Q?Hyundai_bonnet_quote?=",
            "How much for a respray?")
        result = self.import_messages([encoded, safe])
        self.assertEqual(result["skipped_sensitive"], 1)
        self.assertEqual(result["added"], 1)
        queued = gmail.queue()[1]
        self.assertEqual([row["id"] for row in queued], ["encoded-customer"])
        self.assertEqual(queued[0]["subject"], "Hyundai bonnet quote")
        self.assertNotIn("County Court", str([dict(row) for row in queued]))

    def test_invalid_encoded_subject_aborts_whole_page_atomically(self):
        safe = message("safe", "customer@example.invalid", "Tint quote", "Quote please")
        malformed = (
            "=?utf-8?B?%%%?=",  # invalid base64 silently decodes to empty
            "=?x-impossible?Q?Court_claim?=",  # unsupported charset
            "=?utf-8?B?//8=?=",  # undecodable UTF-8 bytes
            "=?utf-8?B?no-end-marker",  # unresolved encoded-word marker
        )
        for subject in malformed:
            with self.subTest(subject=subject):
                bad = message("bad", "neutral@example.invalid", subject, "Hello")
                with self.assertRaisesRegex(RuntimeError, "subject cannot be decoded"):
                    self.import_messages([safe, bad])
                self.assertFalse(gmail._path().exists())

    def test_sensitive_sender_display_name_is_not_queued_from_neutral_address(self):
        legal_plain = message(
            "display-legal", '"County Court Judgment Team" <neutral@example.invalid>',
            "Documents attached", "Please see the attachment.")
        legal_encoded = message(
            "display-encoded",
            "=?utf-8?Q?Debt_Recovery_Department?= <neutral@example.invalid>",
            "General correspondence", "Please review.")
        customer = message(
            "display-customer", '"Hyundai Parts Customer" <buyer@example.invalid>',
            "Bonnet quote", "Please send a price.")
        result = self.import_messages([legal_plain, legal_encoded, customer])
        self.assertEqual(result["skipped_sensitive"], 2)
        self.assertEqual(result["added"], 1)
        self.assertEqual([row["id"] for row in gmail.queue()[1]], ["display-customer"])
        self.assertNotIn("Court", str([dict(row) for row in gmail.queue()[1]]))

    def test_sensitive_reply_to_display_name_is_not_queued(self):
        legal = message(
            "reply-display", "customer@example.invalid",
            "General question", "Please see the attachment.")
        legal["payload"]["headers"].append({
            "name": "Reply-To",
            "value": "=?utf-8?Q?County_Court_Judgment?= <reply@example.invalid>",
        })
        ordinary = message(
            "ordinary", "customer@example.invalid",
            "Window tint", "I would like a quote.")
        result = self.import_messages([legal, ordinary])
        self.assertEqual(result["skipped_sensitive"], 1)
        self.assertEqual(result["added"], 1)
        self.assertEqual([row["id"] for row in gmail.queue()[1]], ["ordinary"])

    def test_undecodable_sender_display_name_aborts_page_before_any_write(self):
        customer = message(
            "customer", "customer@example.invalid", "Tint quote", "Quote please")
        malformed = message(
            "bad", "=?utf-8?B?%%%?= <neutral@example.invalid>",
            "General enquiry", "Please reply.")
        with self.assertRaisesRegex(RuntimeError, "subject cannot be decoded"):
            self.import_messages([customer, malformed])
        self.assertFalse(gmail._path().exists())

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

    def test_sensitive_reply_to_does_not_enter_sales_queue(self):
        legal = message("legal", "accounts@example.invalid", "Quick question",
                        "Please see the attachment.")
        legal["payload"]["headers"].append({
            "name": "Reply-To", "value": "Collections Team <collections@agency.invalid>"
        })
        safe = message("safe", "customer@example.invalid", "Hyundai paint quote",
                       "Could I have a price?")
        safe["payload"]["headers"].append({
            "name": "Reply-To", "value": "Customer <reply@example.invalid>"
        })
        result = self.import_messages([legal, safe])
        self.assertEqual(result["skipped_sensitive"], 1)
        self.assertEqual(result["added"], 1)
        self.assertEqual([row["id"] for row in gmail.queue()[1]], ["safe"])

    def test_duplicate_subject_or_ambiguous_reply_to_aborts_whole_page(self):
        good = message("good", "customer@example.invalid", "Tint quote", "Quote please")
        duplicate_subject = message("bad", "sender@example.invalid", "Booking", "Hello")
        duplicate_subject["payload"]["headers"].append({
            "name": "subject", "value": "County Court Judgment"
        })
        with self.assertRaisesRegex(RuntimeError, "subject headers are ambiguous"):
            self.import_messages([good, duplicate_subject])
        self.assertFalse(gmail._path().exists())
        duplicate_reply_to = message("bad", "sender@example.invalid", "Booking", "Hello")
        duplicate_reply_to["payload"]["headers"].extend([
            {"name": "Reply-To", "value": "customer@example.invalid"},
            {"name": "reply-to", "value": "legal@example.invalid"},
        ])
        invalid_reply_to = message("bad", "sender@example.invalid", "Booking", "Hello")
        invalid_reply_to["payload"]["headers"].append({
            "name": "Reply-To", "value": "customer@example.invalid, legal@example.invalid"
        })
        for bad in (duplicate_reply_to, invalid_reply_to):
            with self.subTest(headers=bad["payload"]["headers"]):
                with self.assertRaisesRegex(RuntimeError, "reply address cannot be verified"):
                    self.import_messages([good, bad])
                self.assertFalse(gmail._path().exists())


if __name__ == "__main__":
    unittest.main()
