"""Synthetic-only source-specific Square Integration Health tests."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hq_connection_readiness import build_readiness
from hq_integration_health import integration_status, record_check, record_committed_sync


class SquareHealthScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"HQ_DATA_DIR": tmp.name,
                                     "HQ_SESSION_SECRET": "fixture-owner-secret"})
        env.start()
        self.addCleanup(env.stop)

    def status(self, location):
        return integration_status([
            ("square", "Square", True),
            ("square_payments", "Square payments", True),
        ], source_locations={"square": location, "square_payments": location})

    def readiness(self, location):
        return {row["key"]: row for row in build_readiness(
            self.status(location), {
                "square": {"configured": True, "consent": None},
                "square_payments": {"configured": True, "consent": None},
            })}

    def test_scoped_success_survives_same_location_but_not_a_changed_location(self):
        for key in ("square", "square_payments"):
            self.assertTrue(record_committed_sync(key, source_location="LOC-ONE"))
        valid = self.readiness("LOC-ONE")
        for key in ("square", "square_payments"):
            self.assertTrue(valid[key]["read_tested"])
            self.assertTrue(valid[key]["sync_tested"])
            self.assertFalse(valid[key]["source_scope_unverified"])
            self.assertFalse(valid[key]["reconciled"])
        different = self.readiness("LOC-TWO")
        for key in ("square", "square_payments"):
            self.assertFalse(different[key]["read_tested"])
            self.assertFalse(different[key]["sync_tested"])
            self.assertIsNone(different[key]["last_success"])
            self.assertTrue(different[key]["source_scope_unverified"])
            self.assertIn("currently configured location", different[key]["blocker"])
            self.assertFalse(different[key]["reconciled"])
        self.assertIsNone(self.status("LOC-TWO")["integrations"][0]["last_success"])
        # Restoring the exact source recovers historical evidence, not current
        # Square permission or independently authenticated settlement.
        self.assertTrue(self.readiness("LOC-ONE")["square"]["sync_tested"])

    def test_legacy_unscoped_records_are_not_source_proof(self):
        record_check("square")
        record_check("square_sync")
        record_check("square_payments")
        self.assertTrue(record_committed_sync("square_payments"))
        old = self.readiness("LOC-ONE")
        for key in ("square", "square_payments"):
            self.assertTrue(old[key]["source_scope_unverified"])
            self.assertFalse(old[key]["read_tested"])
            self.assertFalse(old[key]["sync_tested"])
        # A real, newly observed committed read for the exact location is
        # required to replace the unscoped evidence.
        self.assertTrue(record_committed_sync("square_payments", source_location="LOC-ONE"))
        self.assertTrue(self.readiness("LOC-ONE")["square_payments"]["sync_tested"])
        self.assertFalse(self.readiness("LOC-ONE")["square"]["read_tested"])

    def test_scope_proof_is_pseudonymous_tamper_detectable_and_transactional(self):
        self.assertTrue(record_committed_sync("square", source_location="LOC-ONE"))
        path = self.root / "integration_health.sqlite3"
        self.assertNotIn(b"LOC-ONE", path.read_bytes())
        with sqlite3.connect(path) as db:
            db.execute("UPDATE scoped_sync_proofs SET location_seal='changed' WHERE provider='square'")
        self.assertFalse(self.readiness("LOC-ONE")["square"]["read_tested"])
        self.assertTrue(self.readiness("LOC-ONE")["square"]["source_scope_unverified"])
        self.assertTrue(record_committed_sync("square", source_location="LOC-ONE"))
        self.assertTrue(self.readiness("LOC-ONE")["square"]["sync_tested"])
        # A subsequent unscoped marker must never inherit the old scope seal.
        self.assertTrue(record_committed_sync("square"))
        self.assertFalse(self.readiness("LOC-ONE")["square"]["sync_tested"])

    def test_missing_key_or_invalid_location_cannot_write_scoped_success(self):
        with patch.dict(os.environ, {"HQ_SESSION_SECRET": ""}):
            self.assertFalse(record_committed_sync("square", source_location="LOC-ONE"))
        self.assertFalse(record_committed_sync("square_payments", source_location="\u0000bad"))
        self.assertFalse(record_committed_sync("square_payments", source_location=""))
        self.assertFalse(self.readiness("LOC-ONE")["square"]["read_tested"])
        self.assertFalse(self.readiness("LOC-ONE")["square_payments"]["sync_tested"])


if __name__ == "__main__":
    unittest.main()
