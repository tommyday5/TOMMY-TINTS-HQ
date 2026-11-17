"""Record outcomes of owner-initiated integration reads without storing payloads."""
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


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
        if connected is None:
            state = "Not implemented"
        elif not connected:
            state = "Consent required in this session"
        elif previous.get("needs_attention"):
            state = "Last read failed"
        elif previous.get("last_success"):
            state = "Last read succeeded"
        else:
            state = "Consent present, read unverified"
        entries.append({
            "name": name, "status": state, "last_success": previous.get("last_success"),
            "last_error": previous.get("last_error"), "last_error_at": previous.get("last_error_at"),
            "pending_records": None,
            "intervention_required": connected is not True or bool(previous.get("needs_attention")),
        })
    return {"history_available": history_available, "integrations": entries}
