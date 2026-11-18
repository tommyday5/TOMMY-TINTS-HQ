"""SQLite persistence for owner-only job material logs. Requires mounted HQ_DATA_DIR.

Revisions guard against two open HQ tabs silently overwriting one another.
Existing logs migrate in place; content is never duplicated in audit entries.
"""
import hashlib
import json
import os
import sqlite3
from pathlib import Path


class StaleMaterialLogError(ValueError):
    """The log was changed after the owner loaded it."""


def database_path():
    root = os.environ.get("HQ_DATA_DIR")
    if not root:
        raise RuntimeError("Persistent storage not configured")
    directory = Path(root)
    if not directory.is_dir():
        raise RuntimeError("Persistent storage directory missing")
    return str(directory / "hq_jobs.sqlite3")


def connect():
    db = sqlite3.connect(database_path(), timeout=10)
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("""CREATE TABLE IF NOT EXISTS job_material_logs(
        owner TEXT NOT NULL, job_key TEXT NOT NULL, payload TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        revision INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(owner,job_key))""")
    # Additive migration for logs saved before revisions were introduced.
    if not any(row[1] == "revision" for row in db.execute("PRAGMA table_info(job_material_logs)")):
        try:
            db.execute("ALTER TABLE job_material_logs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                db.close()
                raise
    db.execute("""CREATE TABLE IF NOT EXISTS job_material_log_audit(
        owner TEXT NOT NULL, job_key TEXT NOT NULL, revision INTEGER NOT NULL,
        previous_sha256 TEXT, current_sha256 TEXT NOT NULL,
        changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(owner,job_key,revision))""")
    return db


def read_log(owner, job_key):
    with connect() as db:
        row = db.execute(
            "SELECT payload,updated_at,revision FROM job_material_logs WHERE owner=? AND job_key=?",
            (owner, job_key),
        ).fetchone()
    return (
        {"data": json.loads(row[0]), "updated_at": row[1], "revision": row[2]}
        if row else {"data": None, "updated_at": None, "revision": 0}
    )


def write_log(owner, job_key, data, expected_revision=None):
    """Save one revision atomically; exact retries are idempotent.

    The optional revision argument preserves existing trusted Python callers.
    HTTP callers MUST provide it to avoid lost updates.
    """
    if not isinstance(data, dict):
        raise ValueError("Job material log must be an object")
    if expected_revision is not None and (
        type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1
    ):
        raise ValueError("Invalid material log revision")
    encoded = json.dumps(data, separators=(",", ":"), sort_keys=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > 100000:
        raise ValueError("Job material log too large")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT payload,revision FROM job_material_logs WHERE owner=? AND job_key=?",
            (owner, job_key),
        ).fetchone()
        existing_revision = row[1] if row else 0
        # An identical retried save is safe even if its revision is now old.
        if row and json.loads(row[0]) == data:
            pass
        else:
            if expected_revision is not None and expected_revision != existing_revision:
                raise StaleMaterialLogError(
                    "This job costing log changed in another tab. Review the latest saved version before saving."
                )
            new_revision = existing_revision + 1
            if row:
                db.execute("""UPDATE job_material_logs SET payload=?,updated_at=CURRENT_TIMESTAMP,
                    revision=? WHERE owner=? AND job_key=?""",
                    (encoded, new_revision, owner, job_key))
            else:
                db.execute("""INSERT INTO job_material_logs(owner,job_key,payload,revision)
                    VALUES(?,?,?,?)""", (owner, job_key, encoded, new_revision))
            db.execute("""INSERT INTO job_material_log_audit
                (owner,job_key,revision,previous_sha256,current_sha256) VALUES(?,?,?,?,?)""",
                (owner, job_key, new_revision,
                 hashlib.sha256(row[0].encode("utf-8")).hexdigest() if row else None,
                 hashlib.sha256(encoded.encode("utf-8")).hexdigest()))
    return read_log(owner, job_key)
