"""Fixture-only tests for private encrypted evidence staging rehearsal.

No real bank data, provider calls, Flask routes or network credentials.
"""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from hq_offline_encrypted_evidence import OfflineEncryptedEvidenceStore
from hq_reconciliation_contracts import (
    ContractError, normalise_dna_rows, normalise_monzo_rows,
    preview_bank_rollforward, preview_processor_batches,
)


def monzo(*, amount=10023, account="fixture-account"):
    return normalise_monzo_rows([{
        "transaction_id": "fixture-txn-secret",
        "amount_pence": amount,
        "currency": "GBP",
        "booked_at": "2026-11-19T12:00:00.250+00:00",
    }], account_ref=account)


def dna(*, net=9950, merchant="fixture-merchant"):
    return normalise_dna_rows([{
        "event_id": "fixture-dna-event-secret",
        "payment_id": "fixture-card-payment",
        "kind": "payment",
        "gross_pence": net + 50,
        "fee_pence": 50,
        "net_pence": net,
        "currency": "GBP",
        "occurred_at": "2026-11-19T11:30:00.750+00:00",
        "batch_id": "fixture-dna-batch",
    }], merchant_ref=merchant)


class OfflineEncryptedEvidenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "fixture-staged.sqlite3"
        self.aes_key = bytes.fromhex("ab" * 32)
        self.audit_key = bytes.fromhex("cd" * 32)
        self.store = OfflineEncryptedEvidenceStore(
            self.path, encryption_key=self.aes_key, audit_key=self.audit_key)

    def test_round_trip_monzo_is_still_unverified_and_does_not_disclose_plaintext(self):
        preview = monzo()
        saved = self.store.stage(
            preview, owner_ref="fixture-owner", source_ref="fixture-account",
            request_id="fixture-run-one")
        self.assertEqual(saved["events_staged"], 1)
        self.assertFalse(saved["idempotent_replay"])
        for key in ("source_mapping_verified", "bank_receipt_verified",
                    "bank_balance_verified", "bank_settlement_verified",
                    "trading_revenue_verified", "job_payment_verified",
                    "provider_authorised", "reconciled"):
            self.assertFalse(saved[key])
        self.assertEqual(self.store.load(
            owner_ref="fixture-owner", source_ref="fixture-account",
            provider="monzo", request_id="fixture-run-one"), preview)
        self.assertFalse(preview_bank_rollforward(preview)["bank_balance_verified"])
        raw = self.path.read_bytes()
        for secret in (b"fixture-owner", b"fixture-account",
                       b"fixture-txn-secret", b"10023"):
            self.assertNotIn(secret, raw)

    def test_dna_round_trip_and_provider_isolation(self):
        preview = dna()
        self.store.stage(
            preview, owner_ref="fixture-owner", source_ref="fixture-merchant",
            request_id="fixture-dna-run")
        loaded = self.store.load(
            owner_ref="fixture-owner", source_ref="fixture-merchant",
            provider="dna", request_id="fixture-dna-run")
        self.assertEqual(loaded, preview)
        self.assertFalse(preview_processor_batches(loaded)["bank_settlement_verified"])
        for bad in (
            dict(owner_ref="other-owner", source_ref="fixture-merchant",
                 provider="dna", request_id="fixture-dna-run"),
            dict(owner_ref="fixture-owner", source_ref="other-merchant",
                 provider="dna", request_id="fixture-dna-run"),
            dict(owner_ref="fixture-owner", source_ref="fixture-merchant",
                 provider="monzo", request_id="fixture-dna-run"),
        ):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                self.store.load(**bad)
        self.assertNotIn(b"fixture-dna-event-secret", self.path.read_bytes())

    def test_same_request_is_idempotent_and_conflicting_replay_is_rejected(self):
        preview = monzo()
        self.store.stage(
            preview, owner_ref="fixture-owner", source_ref="fixture-account",
            request_id="fixture-run-one")
        replay = self.store.stage(
            preview, owner_ref="fixture-owner", source_ref="fixture-account",
            request_id="fixture-run-one")
        self.assertTrue(replay["idempotent_replay"])
        with self.assertRaisesRegex(ContractError, "idempotency"):
            self.store.stage(
                monzo(amount=20000), owner_ref="fixture-owner",
                source_ref="fixture-account", request_id="fixture-run-one")
        with sqlite3.connect(self.path) as db:
            count = db.execute("SELECT count(*) FROM offline_encrypted_evidence").fetchone()[0]
        self.assertEqual(count, 1)

    def test_same_request_string_different_owner_has_separate_encrypted_scope(self):
        for owner in ("owner-one", "owner-two"):
            self.store.stage(
                monzo(), owner_ref=owner, source_ref="fixture-account",
                request_id="same-request")
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT request_key, scope_key FROM offline_encrypted_evidence").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertNotEqual(rows[0][1], rows[1][1])

    def test_tampering_ciphertext_or_authenticated_metadata_fails_closed(self):
        for column in ("sealed_payload", "nonce", "recorded_at", "payload_key"):
            with self.subTest(column=column):
                path = self.path.parent / ("tamper-" + column + ".sqlite3")
                store = OfflineEncryptedEvidenceStore(
                    path, encryption_key=self.aes_key, audit_key=self.audit_key)
                store.stage(
                    dna(), owner_ref="fixture-owner", source_ref="fixture-merchant",
                    request_id="fixture-run")
                with sqlite3.connect(path) as db:
                    if column == "sealed_payload":
                        db.execute(
                            "UPDATE offline_encrypted_evidence SET sealed_payload=zeroblob(length(sealed_payload))")
                    elif column == "nonce":
                        db.execute(
                            "UPDATE offline_encrypted_evidence SET nonce=zeroblob(12)")
                    elif column == "recorded_at":
                        db.execute(
                            "UPDATE offline_encrypted_evidence SET recorded_at='2000-01-01T00:00:00+00:00'")
                    else:
                        db.execute(
                            "UPDATE offline_encrypted_evidence SET payload_key=?",
                            ("f" * 64,))
                with self.assertRaises(ContractError):
                    store.load(
                        owner_ref="fixture-owner", source_ref="fixture-merchant",
                        provider="dna", request_id="fixture-run")
                with self.assertRaises(ContractError):
                    store.stage(
                        dna(), owner_ref="fixture-owner",
                        source_ref="fixture-merchant", request_id="fixture-run")

    def test_wrong_encryption_or_audit_key_cannot_read_fixture(self):
        self.store.stage(
            monzo(), owner_ref="fixture-owner", source_ref="fixture-account",
            request_id="fixture-run")
        for aes, audit in (
            (bytes.fromhex("ef" * 32), self.audit_key),
            (self.aes_key, bytes.fromhex("ef" * 32)),
        ):
            store = OfflineEncryptedEvidenceStore(
                self.path, encryption_key=aes, audit_key=audit)
            with self.assertRaises(ContractError):
                store.load(
                    owner_ref="fixture-owner", source_ref="fixture-account",
                    provider="monzo", request_id="fixture-run")

    def test_rejects_unverified_flags_mutations_scope_mismatch_and_empty_rows(self):
        preview = monzo()
        invalid = (
            {**preview, "reconciled": True},
            {**preview, "bank_balance_verified": True},
            {**preview, "source_mapping_verified": True},
            {**preview, "events": preview["events"] + preview["events"]},
            {**preview, "events": ()},
        )
        for case in invalid:
            with self.subTest(case=str(case)[:50]), self.assertRaises(ContractError):
                self.store.stage(
                    case, owner_ref="fixture-owner", source_ref="fixture-account",
                    request_id="fixture-run")
        with self.assertRaises(ContractError):
            self.store.stage(
                preview, owner_ref="fixture-owner", source_ref="wrong-source",
                request_id="fixture-run")

    def test_explicit_file_and_independent_keys_required(self):
        invalid = (
            dict(encryption_key=b"too-short", audit_key=self.audit_key),
            dict(encryption_key=self.aes_key, audit_key=b"too-short"),
            dict(encryption_key=self.aes_key, audit_key=self.aes_key),
        )
        for case in invalid:
            with self.subTest(case=str(case)[:40]), self.assertRaises(ContractError):
                OfflineEncryptedEvidenceStore(
                    self.path.parent / "invalid.sqlite3", **case)
        with self.assertRaises(ContractError):
            OfflineEncryptedEvidenceStore(
                ":memory:", encryption_key=self.aes_key, audit_key=self.audit_key)

    def test_sqlite_backup_restores_ciphertext_in_isolation_not_provider_proof(self):
        self.store.stage(
            dna(), owner_ref="fixture-owner", source_ref="fixture-merchant",
            request_id="fixture-run")
        backup = self.path.parent / "fixture-backup.sqlite3"
        with sqlite3.connect(self.path) as original, sqlite3.connect(backup) as copied:
            original.backup(copied)
        reopened = OfflineEncryptedEvidenceStore(
            backup, encryption_key=self.aes_key, audit_key=self.audit_key)
        self.assertEqual(reopened.load(
            owner_ref="fixture-owner", source_ref="fixture-merchant",
            provider="dna", request_id="fixture-run"), dna())
        self.assertNotIn(b"fixture-merchant", backup.read_bytes())
        # This exercise DOES NOT detect an intact older DB; external trusted
        # audit anchors and independently verified production restores remain mandatory.


    def test_detached_checkpoint_allows_append_only_growth_and_guarded_replay(self):
        self.store.stage(
            monzo(), owner_ref="fixture-owner", source_ref="fixture-account",
            request_id="first")
        trusted = self.store.checkpoint()
        self.assertEqual(trusted["entries"], 1)
        self.assertEqual(len(trusted["head"]), 64)
        self.assertNotIn(b"fixture-account", str(trusted).encode())
        self.store.stage(
            dna(), owner_ref="fixture-owner", source_ref="fixture-merchant",
            request_id="second", trusted_checkpoint=trusted)
        self.assertEqual(self.store.checkpoint(
            trusted_checkpoint=trusted)["entries"], 2)
        replay = self.store.stage(
            monzo(), owner_ref="fixture-owner", source_ref="fixture-account",
            request_id="first", trusted_checkpoint=trusted)
        self.assertTrue(replay["idempotent_replay"])

    def test_detached_checkpoint_blocks_older_database_and_new_writes(self):
        backup = self.path.parent / "older.sqlite3"
        self.store.stage(
            monzo(), owner_ref="fixture-owner", source_ref="fixture-account",
            request_id="first")
        with sqlite3.connect(self.path) as source, sqlite3.connect(backup) as target:
            source.backup(target)
        self.store.stage(
            dna(), owner_ref="fixture-owner", source_ref="fixture-merchant",
            request_id="second")
        trusted = self.store.checkpoint()
        restored = OfflineEncryptedEvidenceStore(
            backup, encryption_key=self.aes_key, audit_key=self.audit_key)
        with self.assertRaisesRegex(ContractError, "rollback"):
            restored.checkpoint(trusted_checkpoint=trusted)
        with self.assertRaisesRegex(ContractError, "rollback"):
            restored.stage(
                monzo(), owner_ref="fixture-owner", source_ref="fixture-account",
                request_id="first", trusted_checkpoint=trusted)
        with sqlite3.connect(backup) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM offline_encrypted_evidence"
                           ).fetchone()[0], 1)

    def test_detached_checkpoint_rejects_old_row_edits_and_deletion(self):
        for case_number, modification in enumerate((
            "UPDATE offline_encrypted_evidence SET recorded_at='2020-01-01T00:00:00Z' WHERE seq=1",
            "DELETE FROM offline_encrypted_evidence WHERE seq=1",
        ), start=1):
            path = self.path.parent / f"case-{case_number}.sqlite3"
            store = OfflineEncryptedEvidenceStore(
                path, encryption_key=self.aes_key, audit_key=self.audit_key)
            store.stage(
                monzo(), owner_ref="fixture-owner", source_ref="fixture-account",
                request_id="first")
            store.stage(
                dna(), owner_ref="fixture-owner", source_ref="fixture-merchant",
                request_id="second")
            trusted = store.checkpoint()
            with sqlite3.connect(path) as db:
                db.execute(modification)
            with self.assertRaisesRegex(ContractError, "checkpoint|rollback"):
                store.checkpoint(trusted_checkpoint=trusted)
            with self.assertRaises(ContractError):
                store.stage(
                    dna(), owner_ref="fixture-owner", source_ref="fixture-merchant",
                    request_id="second", trusted_checkpoint=trusted)

    def test_detached_checkpoint_malformed_or_wrong_key_fails_closed(self):
        self.store.stage(
            monzo(), owner_ref="fixture-owner", source_ref="fixture-account",
            request_id="first")
        trusted = self.store.checkpoint()
        cases = [
            {**trusted, "entries": True},
            {**trusted, "entries": -1},
            {**trusted, "head": "0" * 64},
            {**trusted, "head": trusted["head"].upper()},
            {**trusted, "provider": "monzo"},
        ]
        for anchor in cases:
            with self.subTest(anchor=anchor), self.assertRaises(ContractError):
                self.store.checkpoint(trusted_checkpoint=anchor)
        other = OfflineEncryptedEvidenceStore(
            self.path, encryption_key=self.aes_key,
            audit_key=bytes.fromhex("ef" * 32))
        with self.assertRaisesRegex(ContractError, "checkpoint"):
            other.checkpoint(trusted_checkpoint=trusted)


if __name__ == "__main__":
    unittest.main()
