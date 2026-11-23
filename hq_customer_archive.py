"""Reversible owner-only customer directory archival, never deletion or deactivation.

Customer IDs and all linked records remain intact. An archive flag changes HQ
directory visibility only; it never cancels work, changes customer portal rights,
deletes financial evidence or modifies the historical Google workbook.
"""
import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from flask import current_app
from customer_request_store import connect
from hq_core_records import _schema
from hq_work_progress import _actor_fingerprint

HEX_ID = re.compile(r"[0-9a-f]{32}\Z")


class StaleCustomerArchive(ValueError):
    """The archive decision was submitted from an outdated customer profile."""


class CustomerArchiveRetryConflict(ValueError):
    """The same operation ID was reused for different instructions."""


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_archive_state(
        customer_id TEXT PRIMARY KEY REFERENCES hq_customers(id),
        is_archived INTEGER NOT NULL CHECK(is_archived IN (0,1)),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        changed_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_archive_operations(
        operation_id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        request_fingerprint TEXT NOT NULL,
        resulting_revision INTEGER NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_archive_events(
        id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        revision INTEGER NOT NULL,
        previous_archived INTEGER NOT NULL CHECK(previous_archived IN (0,1)),
        next_archived INTEGER NOT NULL CHECK(next_archived IN (0,1)),
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(customer_id,revision))""")


def _valid_id(value):
    return isinstance(value, str) and HEX_ID.fullmatch(value) is not None


def archive_status(customer_id):
    """Read archive status without hiding a customer's existing relationships."""
    if not _valid_id(customer_id):
        raise ValueError("Invalid HQ customer reference")
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        if db.execute("SELECT 1 FROM hq_customers WHERE id=?", (customer_id,)).fetchone() is None:
            raise LookupError("HQ customer not found")
        state = db.execute("""SELECT is_archived,revision
            FROM hq_customer_archive_state WHERE customer_id=?""", (customer_id,)).fetchone()
        return (bool(state["is_archived"]), state["revision"]) if state else (False, 0)


def set_archived(customer_id, desired_archived, expected_revision, operation_id, owner_identity):
    """Atomic archive/restore with revision guard and retry-safe PII-free audit."""
    if not _valid_id(customer_id) or not _valid_id(operation_id):
        raise ValueError("Invalid HQ customer or operation reference")
    if type(desired_archived) is not bool:
        raise ValueError("Invalid archive action")
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**63-1:
        raise ValueError("Invalid archive revision")
    secret = current_app.secret_key
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("Persistent HQ owner session secret required")
    actor = _actor_fingerprint(owner_identity)
    payload = json.dumps({
        "customer_id": customer_id, "archive": desired_archived,
        "revision": expected_revision, "actor": actor,
    }, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("""SELECT customer_id,request_fingerprint,resulting_revision
            FROM hq_customer_archive_operations WHERE operation_id=?""",
            (operation_id,)).fetchone()
        if prior is not None:
            if prior["customer_id"] != customer_id or not hmac.compare_digest(
                    prior["request_fingerprint"], fingerprint):
                raise CustomerArchiveRetryConflict(
                    "This archive form has already been used for another action.")
            return prior["resulting_revision"]
        if db.execute("SELECT 1 FROM hq_customers WHERE id=?", (customer_id,)).fetchone() is None:
            raise LookupError("HQ customer not found")
        row = db.execute("""SELECT is_archived,revision FROM hq_customer_archive_state
            WHERE customer_id=?""", (customer_id,)).fetchone()
        previous, revision = (bool(row["is_archived"]), row["revision"]) if row else (False, 0)
        if revision != expected_revision:
            raise StaleCustomerArchive(
                "This customer archive state changed. Reload before saving.")
        now = datetime.now(timezone.utc).isoformat()
        if previous != desired_archived:
            revision += 1
            db.execute("""INSERT INTO hq_customer_archive_state
                (customer_id,is_archived,revision,changed_at) VALUES (?,?,?,?)
                ON CONFLICT(customer_id) DO UPDATE SET
                    is_archived=excluded.is_archived,
                    revision=excluded.revision,changed_at=excluded.changed_at""",
                (customer_id, int(desired_archived), revision, now))
            db.execute("""INSERT INTO hq_customer_archive_events
                (id,customer_id,revision,previous_archived,next_archived,
                 actor_fingerprint,occurred_at) VALUES (?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, customer_id, revision, int(previous),
                 int(desired_archived), actor, now))
        db.execute("""INSERT INTO hq_customer_archive_operations
            (operation_id,customer_id,request_fingerprint,resulting_revision,
             actor_fingerprint,created_at) VALUES (?,?,?,?,?,?)""",
            (operation_id, customer_id, fingerprint, revision, actor, now))
        return revision
