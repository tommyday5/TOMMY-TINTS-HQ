"""Record outcomes of owner-initiated integration reads without storing payloads."""
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class PersistedSyncError(RuntimeError):
    """Provider snapshot/queue storage failed; not an upstream read error."""


def valid_observation_time(value):
    """Accept only timezone-aware, non-future local read/sync evidence dates.

    This checks database metadata shape, NOT the authenticity of a provider
    response. A small window allows ordinary server clock skew.
    """
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if observed.tzinfo is None or observed.utcoffset() is None:
            return None
        if observed.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(minutes=5):
            return None
    except (ValueError, OverflowError):
        return None
    return value


def _db_path():
    root = Path(os.environ.get("HQ_DATA_DIR", "/tmp/tommy-tints-hq"))
    return root / "integration_health.sqlite3"


def record_check(provider, error=None):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=2)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS integration_checks (
                provider TEXT PRIMARY KEY, last_success TEXT, last_error TEXT, last_error_at TEXT,
                needs_attention INTEGER NOT NULL)""")
            db.execute("""INSERT INTO integration_checks VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                last_success = COALESCE(excluded.last_success, integration_checks.last_success),
                last_error = COALESCE(excluded.last_error, integration_checks.last_error),
                last_error_at = COALESCE(excluded.last_error_at, integration_checks.last_error_at),
                needs_attention = excluded.needs_attention""",
                (provider, now if error is None else None, error, now if error else None,
                 int(error is not None)))
    except (OSError, sqlite3.Error):
        log.warning("Integration check history could not be saved")


def integration_status(connections):
    history = {}
    history_available = True
    try:
        path = _db_path().resolve()
        if path.exists():
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                history = {row["provider"]: dict(row) for row in db.execute(
                    "SELECT * FROM integration_checks")}
    except (OSError, sqlite3.Error):
        history_available = False

    entries = []
    for key, name, connected in connections:
        previous = history.get(key, {})
        raw_success = previous.get("last_success")
        checked_success = valid_observation_time(raw_success)
        observation_invalid = raw_success is not None and checked_success is None
        if connected is None:
            state = "Not implemented"
        elif not connected:
            state = "Consent required in this session"
        elif observation_invalid:
            state = "Stored read observation invalid"
        elif previous.get("needs_attention"):
            state = "Last read failed"
        elif checked_success:
            state = "Last read succeeded"
        else:
            state = "Consent present, read unverified"
        entries.append({
            "name": name, "status": state, "last_success": checked_success,
            "observation_invalid": observation_invalid,
            "last_error": previous.get("last_error"), "last_error_at": previous.get("last_error_at"),
            "pending_records": None,
            "needs_attention": bool(previous.get("needs_attention")),
            "intervention_required": (connected is not True or observation_invalid
                                      or bool(previous.get("needs_attention"))),
        })
    # A successful read is not evidence of a committed import/snapshot. Only
    # explicit post-commit markers from the owner-initiated flows qualify.
    sync_keys = ("gmail", "square", "square_payments")
    sync_successes = ({key: history.get(key + "_sync", {}).get("last_success")
                       for key in sync_keys} if history_available else {})
    # A historical post-commit success does not erase a later failed sync.
    # Report failures separately from upstream read failures, without losing
    # the old success timestamp. These observations remain owner-only.
    sync_failures = ({
        key: {
            "needs_attention": bool(history.get(key + "_sync", {}).get("needs_attention")),
            "last_error": history.get(key + "_sync", {}).get("last_error"),
            "last_error_at": history.get(key + "_sync", {}).get("last_error_at"),
        } for key in sync_keys
    } if history_available else {})
    return {"history_available": history_available, "integrations": entries,
            "sync_successes": sync_successes, "sync_failures": sync_failures}
