"""Encrypted synthetic-evidence staging rehearsal (NEVER connected to HQ).

This module is deliberately not imported by Flask. It only stores already-
normalised fixture previews; there is no CSV reader, Monzo/DNA API, owner
session, real-data approval or provider authentication. AES-GCM protects
payload bytes at rest and independent HMACs pseudonymise SQLite indexes,
but neither proves real bank transactions or settlement.

DO NOT use for production evidence until owner authentication, verified
provider source mappings, key custody/rotation, external audit anchors,
retention, secure backups and independent restoration are implemented.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hq_reconciliation_contracts import (
    ContractError, _ref, _validated_dna_events, _validated_monzo_events,
)

_FORMAT = "offline-encrypted-evidence-v1"
_COLUMNS = ("request_key", "scope_key", "provider", "payload_key",
            "recorded_at", "nonce", "sealed_payload")
_KEYS = {
    "monzo": frozenset({
        "provider", "events", "duplicate_rows", "canonical_event_digest",
        "source_mapping_verified", "bank_balance_verified",
        "verified_trading_receipts_pence", "reconciled",
    }),
    "dna": frozenset({
        "provider", "events", "duplicate_rows", "canonical_event_digest",
        "source_mapping_verified", "bank_settlement_verified",
        "verified_job_receipts_pence", "reconciled",
    }),
}


def _validated_preview(preview, *, source_ref):
    """Require a complete unchanged canonical, UNVERIFIED source preview."""
    source_ref = _ref(source_ref, "fixture source reference")
    if not isinstance(preview, dict):
        raise ContractError("Canonical unverified fixture evidence required")
    provider = preview.get("provider")
    if not isinstance(provider, str) or provider not in _KEYS or set(preview) != _KEYS[provider]:
        raise ContractError("Unknown or altered canonical preview schema")
    events = (_validated_monzo_events(preview) if provider == "monzo"
              else _validated_dna_events(preview))
    if not events:
        raise ContractError("Empty fixture evidence cannot be staged")
    scope_field = "account_ref" if provider == "monzo" else "merchant_ref"
    if any(event[scope_field] != source_ref for event in events):
        raise ContractError("Fixture evidence and source scope do not match")
    return provider, events


def _payload(preview, *, owner_ref, source_ref):
    return json.dumps({
        "format": _FORMAT, "owner_ref": owner_ref, "source_ref": source_ref,
        "preview": {**preview, "events": list(preview["events"])},
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False).encode("utf-8")


def _unverified():
    return {
        "offline_fixture_only": True, "source_mapping_verified": False,
        "provider_authorised": False, "bank_receipt_verified": False,
        "bank_balance_verified": False, "bank_settlement_verified": False,
        "trading_revenue_verified": False, "job_payment_verified": False,
        "reconciled": False,
    }


class OfflineEncryptedEvidenceStore:
    """Non-production fixture store. No Flask routes or provider access.

    Keys must be independently provisioned and retained outside the database.
    No key recovery, rotation, deletion, off-database auditing or owner identity
    enforcement is provided. Passing an owner_ref here is NOT authentication.
    """

    def __init__(self, path, *, encryption_key, audit_key):
        if (type(encryption_key) is not bytes or len(encryption_key) != 32
                or type(audit_key) is not bytes or len(audit_key) < 32
                or hmac.compare_digest(encryption_key, audit_key)):
            raise ContractError("Distinct 32-byte encryption and audit keys required")
        if (not isinstance(path, (str, Path)) or not str(path)
                or str(path) == ":memory:"):
            raise ContractError("Explicit fixture database file required")
        self._path = str(path)
        self._aead = AESGCM(encryption_key)
        self._audit_key = audit_key
        try:
            with closing(sqlite3.connect(self._path, timeout=5)) as db:
                with db:
                    db.execute("""CREATE TABLE IF NOT EXISTS offline_encrypted_evidence (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        request_key TEXT NOT NULL UNIQUE,
                        scope_key TEXT NOT NULL,
                        provider TEXT NOT NULL CHECK(provider IN ('monzo','dna')),
                        payload_key TEXT NOT NULL,
                        recorded_at TEXT NOT NULL,
                        nonce BLOB NOT NULL,
                        sealed_payload BLOB NOT NULL
                    )""")
        except sqlite3.Error:
            raise ContractError("Fixture evidence database unavailable") from None

    def _h(self, domain, *parts):
        # Domain-separated, keyed identifiers: never index on plain bank refs.
        data = json.dumps([domain, *parts], separators=(",", ":"),
                          ensure_ascii=True).encode("ascii")
        return hmac.new(self._audit_key, data, hashlib.sha256).hexdigest()

    @staticmethod
    def _aad(row):
        return json.dumps([_FORMAT] + [row[name] for name in _COLUMNS[:5]],
                          separators=(",", ":"), ensure_ascii=True).encode("ascii")

    def _keys(self, owner_ref, source_ref, provider, request_id):
        _ref(owner_ref, "fixture owner reference")
        _ref(source_ref, "fixture source reference")
        _ref(request_id, "fixture idempotency reference")
        if provider not in _KEYS:
            raise ContractError("Unsupported fixture provider")
        scope = self._h("source-v1", owner_ref, provider, source_ref)
        request = self._h("request-v1", owner_ref, provider, source_ref, request_id)
        return scope, request


    def _checkpoint_rows(self, rows):
        """Keyed head of the *ordered* encrypted rows, with no plaintext IDs.

        A locally generated head is NOT trusted until separately retained
        outside this SQLite file, under independent owner-controlled custody.
        """
        digest = hmac.new(self._audit_key, b"offline-evidence-checkpoint-v1",
                          hashlib.sha256).digest()
        try:
            for row in rows:
                item = [
                    row["seq"], row["request_key"], row["scope_key"],
                    row["provider"], row["payload_key"], row["recorded_at"],
                    row["nonce"].hex(), row["sealed_payload"].hex(),
                ]
                encoded = json.dumps(item, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")
                digest = hmac.new(self._audit_key, digest + encoded,
                                  hashlib.sha256).digest()
        except (TypeError, ValueError, AttributeError, UnicodeError, KeyError):
            raise ContractError("Fixture checkpoint rows are malformed") from None
        return {"format": "offline-evidence-checkpoint-v1",
                "entries": len(rows), "head": digest.hex()}

    def _verify_checkpoint_rows(self, rows, trusted_checkpoint):
        """Accept an independently held *prefix* only, never infer provenance."""
        if (not isinstance(trusted_checkpoint, dict)
                or set(trusted_checkpoint) != {"format", "entries", "head"}
                or trusted_checkpoint["format"] != "offline-evidence-checkpoint-v1"
                or type(trusted_checkpoint["entries"]) is not int
                or trusted_checkpoint["entries"] < 0
                or not isinstance(trusted_checkpoint["head"], str)
                or len(trusted_checkpoint["head"]) != 64
                or any(c not in "0123456789abcdef"
                       for c in trusted_checkpoint["head"])):
            raise ContractError("Trusted fixture checkpoint is malformed")
        count = trusted_checkpoint["entries"]
        if count > len(rows):
            raise ContractError("Fixture evidence database rollback detected")
        prefix = self._checkpoint_rows(rows[:count])
        if not hmac.compare_digest(prefix["head"], trusted_checkpoint["head"]):
            raise ContractError("Fixture evidence checkpoint mismatch")

    def checkpoint(self, *, trusted_checkpoint=None):
        """Return a detached candidate anchor; optionally verify an older one.

        This does not save the head externally or certify real bank evidence.
        The caller must retain/verify a prior head independently to notice
        rollback, deletion or historical edits.
        """
        try:
            with closing(sqlite3.connect(
                    Path(self._path).resolve().as_uri() + "?mode=ro",
                    uri=True, timeout=5)) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT * FROM offline_encrypted_evidence ORDER BY seq"
                ).fetchall()
                if trusted_checkpoint is not None:
                    self._verify_checkpoint_rows(rows, trusted_checkpoint)
                return self._checkpoint_rows(rows)
        except sqlite3.Error:
            raise ContractError("Fixture checkpoint database read failed") from None

    def _decode(self, row, *, owner_ref, source_ref, provider, scope, request):
        if row["request_key"] != request:
            raise ContractError("Fixture evidence owner/source mismatch")
        return self._decode_scoped(
            row, owner_ref=owner_ref, source_ref=source_ref,
            provider=provider, scope=scope)

    def _decode_scoped(self, row, *, owner_ref, source_ref, provider, scope):
        """Validate one historical encrypted snapshot without its original run ID.

        The opaque request key is authenticated by AES-GCM associated data;
        checking a prior source snapshot must never require plaintext run IDs.
        """
        if row["scope_key"] != scope or row["provider"] != provider:
            raise ContractError("Fixture evidence owner/source mismatch")
        try:
            data = self._aead.decrypt(row["nonce"], row["sealed_payload"],
                                      self._aad(row))
            obj = json.loads(data.decode("utf-8"))
            if (not isinstance(obj, dict)
                    or set(obj) != {"format", "owner_ref", "source_ref", "preview"}
                    or obj["format"] != _FORMAT or obj["owner_ref"] != owner_ref
                    or obj["source_ref"] != source_ref
                    or not isinstance(obj["preview"], dict)
                    or not isinstance(obj["preview"].get("events"), list)):
                raise ContractError("Fixture encrypted envelope is invalid")
            preview = {**obj["preview"],
                       "events": tuple(obj["preview"]["events"])}
            verified_provider, events = _validated_preview(
                preview, source_ref=source_ref)
            if verified_provider != provider:
                raise ContractError("Fixture provider mismatch")
            if not hmac.compare_digest(
                    row["payload_key"],
                    self._h("payload-v1", scope, data.hex())):
                raise ContractError("Fixture evidence HMAC mismatch")
            return preview, len(events)
        except (InvalidTag, TypeError, ValueError, UnicodeError,
                KeyError, OverflowError, AttributeError):
            raise ContractError("Fixture ciphertext integrity check failed") from None

    def stage(self, preview, *, owner_ref, source_ref, request_id, trusted_checkpoint=None):
        """Atomically insert/replay one synthetic snapshot; never book or pay."""
        provider, events = _validated_preview(preview, source_ref=source_ref)
        scope, request = self._keys(owner_ref, source_ref, provider, request_id)
        data = _payload(preview, owner_ref=owner_ref, source_ref=source_ref)
        payload_key = self._h("payload-v1", scope, data.hex())
        try:
            with closing(sqlite3.connect(self._path, timeout=5)) as db:
                db.row_factory = sqlite3.Row
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    if trusted_checkpoint is not None:
                        rows = db.execute(
                            "SELECT * FROM offline_encrypted_evidence ORDER BY seq"
                        ).fetchall()
                        self._verify_checkpoint_rows(rows, trusted_checkpoint)
                    # Different import/run IDs may contain the same provider
                    # event. Those repeats are harmless only if the canonical
                    # event is IDENTICAL. Never let a fresh request bypass the
                    # original idempotency key by silently changing its amount,
                    # timestamp, batch, fee or transaction classification.
                    # Check the entire owner/provider/source scope, including
                    # older fixture databases without a new index/migration.
                    prior_events = {}
                    for historical in db.execute(
                            "SELECT * FROM offline_encrypted_evidence "
                            "WHERE scope_key=? ORDER BY seq", (scope,)):
                        prior, _ = self._decode_scoped(
                            historical, owner_ref=owner_ref,
                            source_ref=source_ref, provider=provider, scope=scope)
                        for old_event in prior["events"]:
                            evidence_id = old_event["evidence_id"]
                            if (evidence_id in prior_events
                                    and prior_events[evidence_id] != old_event):
                                raise ContractError(
                                    "Conflicting provider event in staged fixture history")
                            prior_events[evidence_id] = old_event
                    reused = 0
                    for event in events:
                        evidence_id = event["evidence_id"]
                        if evidence_id in prior_events:
                            if prior_events[evidence_id] != event:
                                raise ContractError(
                                    "Conflicting provider event across fixture runs")
                            reused += 1
                    row = db.execute(
                        "SELECT * FROM offline_encrypted_evidence WHERE request_key=?",
                        (request,)).fetchone()
                    if row:
                        previous, _ = self._decode(
                            row, owner_ref=owner_ref, source_ref=source_ref,
                            provider=provider, scope=scope, request=request)
                        if previous != preview or row["payload_key"] != payload_key:
                            raise ContractError(
                                "Fixture idempotency key reused for different evidence")
                        replay = True
                    else:
                        now = datetime.now(timezone.utc).isoformat()
                        nonce = os.urandom(12)
                        aad = self._aad({
                            "request_key": request, "scope_key": scope,
                            "provider": provider, "payload_key": payload_key,
                            "recorded_at": now,
                        })
                        ciphertext = self._aead.encrypt(nonce, data, aad)
                        db.execute("""INSERT INTO offline_encrypted_evidence
                            (request_key, scope_key, provider, payload_key,
                             recorded_at, nonce, sealed_payload)
                            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                   (request, scope, provider, payload_key, now,
                                    nonce, ciphertext))
                        replay = False
            return {
                **_unverified(), "provider": provider,
                "events_staged": len(events), "idempotent_replay": replay,
                "events_already_seen_in_scope": reused,
            }
        except sqlite3.Error:
            raise ContractError("Fixture evidence database write failed") from None

    def load(self, *, owner_ref, source_ref, provider, request_id,
             trusted_checkpoint=None):
        """Read a scoped fixture, optionally validating independent audit custody.

        The caller must authenticate the owner AND independently retain the
        checkpoint; a locally regenerated head cannot detect a restored DB.
        When supplied, verify the full trusted history in the same SQLite
        read transaction as the target row. This is not live bank evidence.
        """
        scope, request = self._keys(owner_ref, source_ref, provider, request_id)
        try:
            with closing(sqlite3.connect(
                    "file:" + str(Path(self._path).resolve()) + "?mode=ro",
                    uri=True, timeout=5)) as db:
                db.row_factory = sqlite3.Row
                # Freeze both the checkpoint history and the target row in a
                # single read snapshot; concurrent staging cannot change the
                # interpretation between the two SELECT operations.
                db.execute("BEGIN")
                if trusted_checkpoint is not None:
                    rows = db.execute(
                        "SELECT * FROM offline_encrypted_evidence ORDER BY seq"
                    ).fetchall()
                    self._verify_checkpoint_rows(rows, trusted_checkpoint)
                row = db.execute(
                    "SELECT * FROM offline_encrypted_evidence WHERE request_key=?",
                    (request,)).fetchone()
                if row is None:
                    raise ContractError("Fixture snapshot not found for scope")
                preview, _ = self._decode(
                    row, owner_ref=owner_ref, source_ref=source_ref,
                    provider=provider, scope=scope, request=request)
                return preview
        except sqlite3.Error:
            raise ContractError("Fixture evidence database read failed") from None
