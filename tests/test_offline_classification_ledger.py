"""Synthetic-only regression tests for the isolated review ledger."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from hq_offline_classification_ledger import OfflineClassificationLedger
from hq_reconciliation_contracts import ContractError, normalise_monzo_rows, normalise_dna_rows


def bank(account="sample-account", *, amount=10250, transaction="fixture-txn"):
    return normalise_monzo_rows([{
        "transaction_id": transaction, "amount_pence": amount,
        "currency": "GBP", "booked_at": "2026-11-18T12:00:00Z",
    }], account_ref=account)


class OfflineClassificationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "isolated-review.sqlite3"
        self.key = b"fixture-secret-for-hmac-audit-ledger" * 2
        self.ledger = OfflineClassificationLedger(self.db_path, hmac_key=self.key)

    def submit(self, evidence=None, **changes):
        args = dict(transaction_id="fixture-txn", category="customer_receipt_candidate",
                    actor_ref="fixture-owner", request_id="fixture-operation-one",
                    expected_revision=0, reason="initial_review")
        args.update(changes)
        return self.ledger.record(evidence or bank(), **args)

    def test_initial_review_is_pending_evidence_not_verified_money(self):
        result = self.submit()
        self.assertEqual(result["revision"], 1)
        self.assertFalse(result["idempotent_replay"])
        for field in ("source_mapping_verified", "bank_receipt_verified",
                      "trading_revenue_verified", "job_payment_verified",
                      "bank_balance_verified", "reconciled"):
            self.assertFalse(result[field])
        history = self.ledger.history(bank(), transaction_id="fixture-txn")
        self.assertEqual([(r["revision"], r["category"]) for r in history["review_events"]],
                         [(1, "customer_receipt_candidate")])
        self.assertFalse(history["job_payment_verified"])

    def test_retry_is_idempotent_after_reopening_database(self):
        self.submit()
        reopened = OfflineClassificationLedger(self.db_path, hmac_key=self.key)
        result = reopened.record(bank(), transaction_id="fixture-txn",
                                 category="customer_receipt_candidate",
                                 actor_ref="fixture-owner", request_id="fixture-operation-one",
                                 expected_revision=0, reason="initial_review")
        self.assertTrue(result["idempotent_replay"])
        self.assertEqual(len(self.ledger.history(bank(), transaction_id="fixture-txn")
                             ["review_events"]), 1)

    def test_correction_appends_and_stale_revision_is_rejected(self):
        self.submit()
        corrected = self.submit(category="director_funding",
                                request_id="fixture-operation-two", expected_revision=1,
                                reason="operator_correction")
        self.assertEqual(corrected["revision"], 2)
        events = self.ledger.history(bank(), transaction_id="fixture-txn")["review_events"]
        self.assertEqual([e["category"] for e in events],
                         ["customer_receipt_candidate", "director_funding"])
        with self.assertRaisesRegex(ContractError, "stale"):
            self.submit(category="transfer", request_id="fixture-operation-three",
                        expected_revision=1, reason="operator_correction")

    def test_request_key_reuse_with_changed_payload_is_blocked(self):
        self.submit()
        for changed in (
            {"category": "transfer"},
            {"actor_ref": "different-owner"},
            {"expected_revision": 1, "reason": "operator_correction"},
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                    ContractError, "Idempotency"):
                self.submit(**changed)

    def test_changed_source_evidence_is_never_silently_reclassified(self):
        self.submit()
        changed_source = bank(amount=10500)
        with self.assertRaisesRegex(ContractError, "Conflicting source"):
            self.submit(changed_source, category="transfer",
                        request_id="fixture-operation-two", expected_revision=1,
                        reason="new_evidence")
        with self.assertRaisesRegex(ContractError, "Source evidence"):
            self.ledger.history(changed_source, transaction_id="fixture-txn")

    def test_different_account_same_provider_id_is_isolated(self):
        self.submit()
        second = bank("different-account")
        another = self.submit(second, request_id="second-account-operation")
        self.assertEqual(another["revision"], 1)
        self.assertEqual(len(self.ledger.history(bank(), transaction_id="fixture-txn")
                             ["review_events"]), 1)
        self.assertEqual(len(self.ledger.history(second, transaction_id="fixture-txn")
                             ["review_events"]), 1)

    def test_debits_dna_unknown_transactions_and_invalid_reasons_fail(self):
        with self.assertRaises(ContractError):
            self.submit(bank(amount=-100))
        with self.assertRaises(ContractError):
            self.submit(bank(), transaction_id="not-in-evidence")
        dna = normalise_dna_rows([{
            "event_id": "event-1", "payment_id": "payment-1",
            "kind": "payment", "gross_pence": 1000, "fee_pence": 20,
            "net_pence": 980, "currency": "GBP",
            "occurred_at": "2026-11-18T12:00:00Z",
        }], merchant_ref="fixture-merchant")
        with self.assertRaises(ContractError):
            self.submit(dna)
        with self.assertRaises(ContractError):
            self.submit(reason="operator_correction")
        with self.assertRaises(ContractError):
            self.submit(expected_revision=True)
        with self.assertRaises(ContractError):
            self.submit(reason="paid")

    def test_sqlite_contains_only_keyed_refs_not_raw_bank_data(self):
        self.submit()
        self.ledger.history(bank(), transaction_id="fixture-txn")
        data = self.db_path.read_bytes()
        for raw in (b"fixture-txn", b"sample-account", b"fixture-owner",
                    b"fixture-operation-one", b"10250"):
            self.assertNotIn(raw, data)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM offline_review_events").fetchone()[0], 1)

    def test_audit_integrity_and_isolated_sqlite_backup_restore(self):
        self.submit()
        self.submit(category="director_funding", request_id="fixture-operation-two",
                    expected_revision=1, reason="operator_correction")
        baseline = self.ledger.verify_integrity()
        self.assertEqual(baseline["events_checked"], 2)
        self.assertEqual(baseline["evidence_chains_checked"], 1)
        self.assertTrue(baseline["audit_structure_valid"])
        self.assertTrue(baseline["offline_fixture_only"])
        self.assertFalse(baseline["bank_receipt_verified"])

        backup = Path(self.temp.name) / "fixture-backup.sqlite3"
        restore = Path(self.temp.name) / "fixture-restored.sqlite3"
        with sqlite3.connect(self.db_path) as original, sqlite3.connect(backup) as copy:
            original.backup(copy)
        with sqlite3.connect(backup) as copy, sqlite3.connect(restore) as restored:
            copy.backup(restored)
        recovered = OfflineClassificationLedger(restore, hmac_key=self.key)
        self.assertEqual(recovered.verify_integrity(), baseline)
        self.assertEqual(recovered.history(bank(), transaction_id="fixture-txn"),
                         self.ledger.history(bank(), transaction_id="fixture-txn"))
        replay = recovered.record(bank(), transaction_id="fixture-txn",
                                  category="director_funding", actor_ref="fixture-owner",
                                  request_id="fixture-operation-two", expected_revision=1,
                                  reason="operator_correction")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(recovered.verify_integrity()["events_checked"], 2)

    def test_payload_tampering_blocks_history_and_further_reviews(self):
        self.submit()
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE offline_review_events SET category='tax' WHERE revision=1")
        with self.assertRaisesRegex(ContractError, "integrity"):
            self.ledger.verify_integrity()
        with self.assertRaisesRegex(ContractError, "integrity"):
            self.ledger.history(bank(), transaction_id="fixture-txn")
        with self.assertRaisesRegex(ContractError, "integrity"):
            self.submit(category="transfer", request_id="fixture-operation-two",
                        expected_revision=1, reason="operator_correction")
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM offline_review_events").fetchone()[0], 1)

    def test_missing_middle_revision_and_wrong_key_fail_closed(self):
        self.submit()
        self.submit(category="director_funding", request_id="fixture-operation-two",
                    expected_revision=1, reason="operator_correction")
        self.submit(category="transfer", request_id="fixture-operation-three",
                    expected_revision=2, reason="new_evidence")
        wrong_key = OfflineClassificationLedger(
            self.db_path, hmac_key=b"another-synthetic-audit-hmac-key-12345678")
        with self.assertRaisesRegex(ContractError, "integrity"):
            wrong_key.verify_integrity()
        with sqlite3.connect(self.db_path) as db:
            db.execute("DELETE FROM offline_review_events WHERE revision=2")
        with self.assertRaisesRegex(ContractError, "integrity"):
            self.ledger.verify_integrity()

    def test_new_events_seal_request_identity_timestamp_and_signature_version(self):
        for field, tampered in (
            ("request_key", "a" * 64),
            ("recorded_at", "2026-11-18T22:30:00+00:00"),
            ("signature_version", 1),
            ("event_key", None),
        ):
            with self.subTest(field=field):
                source = Path(self.temp.name) / ("sealed-" + field + ".sqlite3")
                ledger = OfflineClassificationLedger(source, hmac_key=self.key)
                ledger.record(bank(), transaction_id="fixture-txn",
                              category="customer_receipt_candidate",
                              actor_ref="fixture-owner", request_id="new-" + field,
                              expected_revision=0, reason="initial_review")
                with sqlite3.connect(source) as db:
                    db.execute("UPDATE offline_review_events SET " + field + "=?",
                               (tampered,))
                with self.assertRaises(ContractError):
                    ledger.verify_integrity()
                with self.assertRaises(ContractError):
                    ledger.history(bank(), transaction_id="fixture-txn")

    def test_legacy_fixture_schema_reopens_without_rewriting_old_hmac(self):
        legacy_path = Path(self.temp.name) / "legacy-review.sqlite3"
        scope, evidence, source = self.ledger._source(
            bank(), "fixture-txn", "customer_receipt_candidate")
        actor = self.ledger._digest("actor", "fixture-owner")
        request = self.ledger._digest("request", "legacy-op")
        payload = self.ledger._digest(
            "payload", scope, evidence, source, "customer_receipt_candidate",
            actor, 0, "initial_review")
        with sqlite3.connect(legacy_path) as db:
            db.execute("""CREATE TABLE offline_review_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                request_key TEXT NOT NULL UNIQUE,
                payload_key TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                evidence_key TEXT NOT NULL,
                source_key TEXT NOT NULL,
                revision INTEGER NOT NULL,
                category TEXT NOT NULL,
                reason TEXT NOT NULL,
                actor_key TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(evidence_key, revision))""")
            db.execute("""INSERT INTO offline_review_events
                (request_key, payload_key, scope_key, evidence_key, source_key,
                 revision, category, reason, actor_key, recorded_at)
                VALUES (?, ?, ?, ?, ?, 1, 'customer_receipt_candidate',
                        'initial_review', ?, ?)""",
                (request, payload, scope, evidence, source, actor,
                 "2026-11-18T12:05:00+00:00"))
        reopened = OfflineClassificationLedger(legacy_path, hmac_key=self.key)
        self.assertEqual(reopened.verify_integrity()["events_checked"], 1)
        with sqlite3.connect(legacy_path) as db:
            self.assertEqual(db.execute(
                "SELECT signature_version, event_key FROM offline_review_events"
            ).fetchone(), (1, None))
        replay = reopened.record(
            bank(), transaction_id="fixture-txn",
            category="customer_receipt_candidate", actor_ref="fixture-owner",
            request_id="legacy-op", expected_revision=0, reason="initial_review")
        self.assertTrue(replay["idempotent_replay"])
        correction = reopened.record(
            bank(), transaction_id="fixture-txn", category="transfer",
            actor_ref="fixture-owner", request_id="new-op",
            expected_revision=1, reason="operator_correction")
        self.assertEqual(correction["revision"], 2)
        self.assertEqual(reopened.verify_integrity()["events_checked"], 2)
        with sqlite3.connect(legacy_path) as db:
            self.assertEqual(db.execute(
                "SELECT signature_version FROM offline_review_events ORDER BY revision"
            ).fetchall(), [(1,), (2,)])

    def test_detached_anchor_detects_final_event_deletion_without_in_db_gaps(self):
        self.submit()
        first = self.ledger.export_anchor()
        self.assertEqual(first["events_checked"], 1)
        self.assertEqual(first["format"], "offline-classification-head-v1")
        self.assertEqual(len(first["head_hmac"]), 64)
        self.assertNotIn("fixture-txn", str(first))
        verified = self.ledger.verify_anchor(first)
        self.assertTrue(verified["anchor_matches"])
        self.assertFalse(verified["bank_receipt_verified"])
        self.assertFalse(verified["reconciled"])

        self.submit(category="transfer", request_id="fixture-operation-two",
                    expected_revision=1, reason="operator_correction")
        newer = self.ledger.export_anchor()
        self.assertEqual(newer["events_checked"], 2)
        with self.assertRaisesRegex(ContractError, "head mismatch"):
            self.ledger.verify_anchor(first)
        self.assertTrue(self.ledger.verify_anchor(newer)["anchor_matches"])
        # The in-DB chain still looks intact when its most recent event is
        # deleted. Only the independently retained newer anchor catches it.
        with sqlite3.connect(self.db_path) as db:
            db.execute("DELETE FROM offline_review_events WHERE revision=2")
        self.assertEqual(self.ledger.verify_integrity()["events_checked"], 1)
        with self.assertRaisesRegex(ContractError, "head mismatch"):
            self.ledger.verify_anchor(newer)
        self.assertTrue(self.ledger.verify_anchor(first)["anchor_matches"])

    def test_detached_anchor_detects_full_database_rollback_after_valid_restore(self):
        self.submit()
        backup = Path(self.temp.name) / "older-synthetic-backup.sqlite3"
        with sqlite3.connect(self.db_path) as live, sqlite3.connect(backup) as older:
            live.backup(older)
        self.submit(category="director_funding", request_id="fixture-operation-two",
                    expected_revision=1, reason="operator_correction")
        trusted_head = self.ledger.export_anchor()
        # Simulate an intact, individually HMAC-valid *old* database.
        with sqlite3.connect(backup) as older, sqlite3.connect(self.db_path) as live:
            older.backup(live)
        self.assertTrue(self.ledger.verify_integrity()["audit_structure_valid"])
        with self.assertRaisesRegex(ContractError, "head mismatch"):
            self.ledger.verify_anchor(trusted_head)

    def test_detached_anchor_rejects_untrusted_or_malformed_expected_heads(self):
        self.submit()
        trusted = self.ledger.export_anchor()
        invalid = (
            None, "", {}, {**trusted, "events_checked": True},
            {**trusted, "events_checked": -1},
            {**trusted, "format": "another-format"},
            {**trusted, "head_hmac": "a" * 64},
            {**trusted, "head_hmac": "f" * 63},
            {**trusted, "reconciled": True},
        )
        for case in invalid:
            with self.subTest(case=case), self.assertRaises(ContractError):
                self.ledger.verify_anchor(case)
        reopened = OfflineClassificationLedger(self.db_path, hmac_key=self.key)
        self.assertTrue(reopened.verify_anchor(trusted)["anchor_matches"])

    def test_explicit_key_and_file_are_required(self):
        with self.assertRaises(ContractError):
            OfflineClassificationLedger(self.db_path, hmac_key=b"short")
        with self.assertRaises(ContractError):
            OfflineClassificationLedger(":memory:", hmac_key=self.key)


if __name__ == "__main__":
    unittest.main()
