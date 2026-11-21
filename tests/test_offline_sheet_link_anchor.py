"""Synthetic-only detached candidate ledger HMAC tests; no production access."""
import hashlib
import json
import sqlite3
import unittest

from hq_offline_sheet_link_proposals import (
    LinkProposalError, _candidate_digest, inspect_offline_candidate_ledger,
    stage_unverified_candidate,
)
from hq_offline_sheet_link_anchor import (
    export_detached_candidate_head, verify_detached_candidate_head,
)

OWNER = "a" * 32
OTHER = "f" * 32
KEY = b"fixture-only-offline-sheet-links-head-key-1234"


def inspection():
    return {
        "preflight": "sheet_snapshot_only", "candidate_kind": "existing",
        "complete_reference_scan": True, "workbook_identity_checked": True,
        "selected_row_formula_safe": True, "historical_mapping_verified": False,
        "baseline_verified": False, "write_authorised": False,
        "live_sync": False, "reconciled": False,
    }


class DetachedOfflineSheetHeadTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)

    def stage(self, owner_scope=OWNER):
        return stage_unverified_candidate(
            self.db, owner_scope=owner_scope, operation_id="c" * 32,
            work_id="b" * 32, work_revision=3, job_ref="J0077",
            sheet_row=5, inspection=inspection(),
        )

    def head(self, owner_scope=OWNER, key=KEY):
        return export_detached_candidate_head(
            self.db, owner_scope=owner_scope, key=key,
        )

    def check(self, head, owner_scope=OWNER, key=KEY):
        return verify_detached_candidate_head(
            self.db, owner_scope=owner_scope, key=key, head=head,
        )

    def test_empty_snapshot_is_read_only_and_does_not_invent_verified_states(self):
        head = self.head()
        self.assertEqual((head["candidate_count"], head["event_count"]), (0, 0))
        self.assertFalse(head["source_authenticated"])
        self.assertFalse(head["approved"])
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'offline_hq_%'"
        ).fetchone()[0], 0)
        self.assertTrue(self.check(head)["anchor_matches"])
        self.assertFalse(self.db.in_transaction)

    def test_repeatable_snapshot_and_independent_owner_scope(self):
        self.stage()
        head = self.head()
        self.assertEqual(head, self.head())
        self.assertEqual(head["candidate_count"], 1)
        self.assertNotIn("J0077", json.dumps(head))
        self.assertNotIn("b" * 32, json.dumps(head))
        self.assertTrue(self.check(head)["anchor_matches"])
        self.stage(owner_scope=OTHER)
        self.assertEqual(head, self.head())
        self.assertNotEqual(head["digest"], self.head(owner_scope=OTHER)["digest"])
        with self.assertRaises(LinkProposalError):
            self.check(head, owner_scope=OTHER)

    def test_deleted_last_candidate_and_event_detected_even_if_local_check_passes(self):
        self.stage()
        head = self.head()
        self.db.execute("DELETE FROM offline_hq_sheet_link_events")
        self.db.execute("DELETE FROM offline_hq_sheet_link_proposals")
        self.db.commit()
        self.assertTrue(inspect_offline_candidate_ledger(
            self.db, owner_scope=OWNER,
        )["integrity_consistent"])
        with self.assertRaises(LinkProposalError):
            self.check(head)

    def test_changed_operation_id_detected_by_detached_head(self):
        self.stage()
        head = self.head()
        self.db.execute(
            "UPDATE offline_hq_sheet_link_proposals SET operation_id=?",
            ("d" * 32,),
        )
        self.db.commit()
        self.assertTrue(inspect_offline_candidate_ledger(
            self.db, owner_scope=OWNER,
        )["integrity_consistent"])
        with self.assertRaises(LinkProposalError):
            self.check(head)

    def test_recomputed_local_checksum_does_not_defeat_detached_head(self):
        self.stage()
        head = self.head()
        self.db.execute(
            "UPDATE offline_hq_sheet_link_proposals SET work_revision=4"
        )
        p = self.db.execute(
            """SELECT owner_scope,work_id,work_revision,workbook_id,
                      sheet_id,job_ref,sheet_row FROM offline_hq_sheet_link_proposals"""
        ).fetchone()
        self.db.execute(
            "UPDATE offline_hq_sheet_link_events SET payload_digest=?",
            (_candidate_digest(tuple(p)),),
        )
        self.db.commit()
        self.assertTrue(inspect_offline_candidate_ledger(
            self.db, owner_scope=OWNER,
        )["integrity_consistent"])
        with self.assertRaises(LinkProposalError):
            self.check(head)

    def test_wrong_key_and_invalid_anchor_fail_closed(self):
        self.stage()
        head = self.head()
        with self.assertRaises(LinkProposalError):
            self.check(head, key=b"another-unrelated-synthetic-head-key123")
        for changed in (
            {"digest": "0" * 64}, {"version": 2},
            {"approved": True}, {"candidate_count": True},
            {"candidate_count": 2}, {"unexpected_field": "bad"},
            {"digest": "not a valid digest"},
        ):
            bad = dict(head, **changed)
            with self.subTest(changed=changed), self.assertRaises(LinkProposalError):
                self.check(bad)
        for key in (b"short", "not bytes", None):
            with self.subTest(key=key), self.assertRaises(LinkProposalError):
                self.head(key=key)

    def test_existing_transaction_is_preserved_unchanged(self):
        self.stage()
        head = self.head()
        self.db.execute(
            "UPDATE offline_hq_sheet_link_proposals SET work_revision=9"
        )
        self.assertTrue(self.db.in_transaction)
        with self.assertRaises(LinkProposalError):
            self.head()
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertTrue(self.check(head)["anchor_matches"])


if __name__ == "__main__":
    unittest.main()
