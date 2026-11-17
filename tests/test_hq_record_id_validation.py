"""Regression tests for canonical internal work IDs, discovered by unittest."""
import unittest
from unittest.mock import patch

from hq_core_records import delete_test_job, set_draft_financials


class RecordIdValidationTests(unittest.TestCase):
    BAD_IDS = ("z" * 32, "A" * 32, "0" * 31, "0" * 33,
               "../" + "0" * 29, None, 123, True)

    def test_financial_edit_rejects_invalid_record_reference_without_db_access(self):
        with patch("hq_core_records.connect") as connect:
            for bad_id in self.BAD_IDS:
                with self.subTest(record_id=bad_id):
                    with self.assertRaisesRegex(ValueError, "Invalid record"):
                        set_draft_financials(bad_id, 10000, 0)
            connect.assert_not_called()

    def test_cleanup_rejects_invalid_record_reference_without_db_access(self):
        with patch("hq_core_records.connect") as connect:
            for bad_id in self.BAD_IDS:
                with self.subTest(record_id=bad_id):
                    with self.assertRaisesRegex(ValueError, "Invalid record"):
                        delete_test_job(bad_id)
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
