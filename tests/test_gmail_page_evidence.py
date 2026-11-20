"""Fail-closed synthetic Gmail pagination contract, not a live mailbox read."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hq_gmail_enquiries as gmail


class GmailPageEvidenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"HQ_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.db_path = Path(tmp.name) / "gmail_enquiries.sqlite3"

    def run_page(self, page, *, token=None):
        with (patch.object(gmail, "verify_mailbox"),
              patch.object(gmail, "_resolve_sales_label_ids",
                           return_value={"HQ-Enquiries": "Label_Enquiries"}),
              patch.object(gmail, "_get", return_value=page) as read):
            value = gmail.import_page("synthetic", page_token=token)
            return value, read

    def test_invalid_list_shapes_and_duplicate_ids_do_not_write(self):
        malformed = (
            None, [], "not a Gmail response",
            {"messages": None}, {"messages": {}}, {"messages": "abc"},
            {"messages": [None]}, {"messages": [{}]},
            {"messages": [{"id": 3}]},
            {"messages": [{"id": "bad!"}]},
            {"messages": [{"id": "msg1"}, {"id": "msg1"}]},
            {"messages": [{"id": str(n)} for n in range(11)]},
            {"messages": [], "nextPageToken": []},
            {"messages": [], "nextPageToken": ""},
            {"messages": [], "nextPageToken": "next\nline"},
        )
        for page in malformed:
            with self.subTest(page=page), self.assertRaises(RuntimeError):
                self.run_page(page)
            self.assertFalse(self.db_path.exists())

    def test_invalid_inbound_page_token_never_reaches_provider(self):
        for token in ("", "bad\nvalue", "x" * 4097, [], 23):
            with self.subTest(token=token):
                with (patch.object(gmail, "verify_mailbox"),
                      patch.object(gmail, "_resolve_sales_label_ids",
                                   return_value={"HQ-Enquiries": "Label_Enquiries"}),
                      patch.object(gmail, "_get") as read,
                      self.assertRaisesRegex(RuntimeError, "pagination token")):
                    gmail.import_page("synthetic", page_token=token)
                read.assert_not_called()
                self.assertFalse(self.db_path.exists())

    def test_genuine_empty_page_is_not_a_validation_failure(self):
        result, read = self.run_page({"resultSizeEstimate": 0})
        self.assertEqual(result, {
            "added": 0, "read": 0, "skipped_sensitive": 0,
            "skipped_label_mismatch": 0, "next": None,
        })
        read.assert_called_once()
        self.assertTrue(self.db_path.exists())

    def test_valid_opaque_token_and_empty_next_page(self):
        result, read = self.run_page(
            {"messages": [], "nextPageToken": "eyJhIjoidGVzdC8rPSJ9"},
            token="previous+token/==",
        )
        self.assertEqual(result["next"], "eyJhIjoidGVzdC8rPSJ9")
        self.assertEqual(read.call_args.kwargs["pageToken"], "previous+token/==")


if __name__ == "__main__":
    unittest.main()
