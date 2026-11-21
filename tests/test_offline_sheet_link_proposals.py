"""Synthetic offline candidate reservations: never touches production HQ or Sheets."""
import json
import sqlite3
import unittest

from hq_offline_sheet_link_proposals import (
    LinkProposalError, stage_unverified_candidate,
)

OWNER = "a" * 32
WORK = "b" * 32
OP = "c" * 32


def evidence(**changes):
    data = {
        "preflight": "sheet_snapshot_only",
        "candidate_kind": "existing",
        "complete_reference_scan": True,
        "workbook_identity_checked": True,
        "selected_row_formula_safe": True,
        "historical_mapping_verified": False,
        "baseline_verified": False,
        "write_authorised": False,
        "live_sync": False,
        "reconciled": False,
    }
    data.update(changes)
    return data


class OfflineHistoricalCandidateTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)

    def stage(self, **overrides):
        args = dict(owner_scope=OWNER, operation_id=OP, work_id=WORK,
                    work_revision=3, job_ref="J0077", sheet_row=5,
                    inspection=evidence())
        args.update(overrides)
        return stage_unverified_candidate(self.db, **args)

    def counts(self):
        return (self.db.execute(
            "SELECT count(*) FROM offline_hq_sheet_link_proposals").fetchone()[0],
            self.db.execute(
                "SELECT count(*) FROM offline_hq_sheet_link_events").fetchone()[0])

    def test_stage_is_explicitly_unverified_with_one_local_event(self):
        result = self.stage()
        self.assertEqual(result, {
            "candidate_state": "unverified_candidate", "created": True,
            "approved": False, "live_sync": False, "reconciled": False})
        self.assertEqual(self.counts(), (1, 1))
        self.assertNotIn(WORK, json.dumps(result))
        self.assertNotIn("J0077", json.dumps(result))
        self.assertEqual(self.db.execute(
            "SELECT status FROM offline_hq_sheet_link_proposals").fetchone()[0],
            "unverified_candidate")
        self.assertEqual(len(self.db.execute(
            "SELECT payload_digest FROM offline_hq_sheet_link_events").fetchone()[0]), 64)

    def test_exact_retry_is_idempotent_but_changed_operation_is_rejected(self):
        self.stage()
        self.assertFalse(self.stage()["created"])
        self.assertEqual(self.counts(), (1, 1))
        with self.assertRaises(LinkProposalError):
            self.stage(sheet_row=6)
        self.assertEqual(self.counts(), (1, 1))

    def test_one_to_one_reservation_blocks_work_row_and_reference_collisions(self):
        self.stage()
        for change in (
            dict(operation_id="d" * 32, work_id="e" * 32, job_ref="J0077", sheet_row=7),
            dict(operation_id="d" * 32, work_id="e" * 32, job_ref="J0088", sheet_row=5),
            dict(operation_id="d" * 32, work_id=WORK, job_ref="J0088", sheet_row=7),
        ):
            with self.subTest(change=change), self.assertRaises(LinkProposalError):
                self.stage(**change)
            self.assertEqual(self.counts(), (1, 1))

    def test_owner_scope_isolated_with_reused_provider_and_work_ids(self):
        self.stage()
        self.assertTrue(self.stage(owner_scope="f" * 32)["created"])
        self.assertEqual(self.counts(), (2, 2))

    def test_malformed_and_unsafe_readiness_evidence_never_stages(self):
        for invalid in (
            evidence(complete_reference_scan=False),
            evidence(candidate_kind="new"),
            evidence(historical_mapping_verified=True),
            evidence(baseline_verified=True),
            evidence(write_authorised=True),
            evidence(live_sync=True),
            evidence(reconciled=True),
            evidence(selected_row_formula_safe=None),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(LinkProposalError):
                self.stage(inspection=invalid)
        for invalid in (
            {"work_revision": True},
            {"work_revision": -1},
            {"sheet_row": True},
            {"sheet_row": 2001},
            {"job_ref": "=IMPORTXML()"},
            {"owner_scope": "owner@example.com"},
            {"operation_id": "not-a-uuid"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(LinkProposalError):
                self.stage(**invalid)

    def test_audit_failure_rolls_back_candidate_not_just_event(self):
        self.stage()
        self.db.execute("""CREATE TRIGGER deny_link_event
            BEFORE INSERT ON offline_hq_sheet_link_events
            BEGIN SELECT RAISE(ABORT,'synthetic event write failure'); END""")
        with self.assertRaises(LinkProposalError):
            self.stage(operation_id="d" * 32, work_id="e" * 32,
                       job_ref="J0088", sheet_row=6)
        self.assertEqual(self.counts(), (1, 1))


if __name__ == "__main__":
    unittest.main()
