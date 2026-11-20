"""Record outcomes of owner-initiated integration reads without storing payloads."""
import hashlib
import hmac
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


_UPSERT_OBSERVATION = """INSERT INTO integration_checks VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(provider) DO UPDATE SET
    last_success = COALESCE(excluded.last_success, integration_checks.last_success),
    last_error = COALESCE(excluded.last_error, integration_checks.last_error),
    last_error_at = COALESCE(excluded.last_error_at, integration_checks.last_error_at),
    needs_attention = excluded.needs_attention"""


def _write_observation(db, provider, now, error):
    db.execute(_UPSERT_OBSERVATION,
               (provider, now if error is None else None, error,
                now if error else None, int(error is not None)))


def _open_observation_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS integration_checks (
        provider TEXT PRIMARY KEY, last_success TEXT, last_error TEXT, last_error_at TEXT,
        needs_attention INTEGER NOT NULL)""")


def _open_committed_sync_proofs(db):
    """Additive proof that a read and persistent write were observed together.

    The proof row shares the SQLite transaction with both success markers.
    It attests to the application's local observation only, not the provider
    response, source completeness, or any bank/job reconciliation.
    """
    db.execute("""CREATE TABLE IF NOT EXISTS committed_sync_proofs (
        provider TEXT PRIMARY KEY, observed_at TEXT NOT NULL)""")


def _square_location_seal(location):
    """Pseudonymise an exact Square location without storing its raw ID.

    The stable owner session secret is a local integrity key, not proof of
    Square authorisation. Key rotation intentionally invalidates old seals.
    """
    key = os.environ.get("HQ_SESSION_SECRET")
    if (not key or not isinstance(location, str) or not 1 <= len(location) <= 192
            or not location.isascii() or not location.isprintable()):
        return None
    return hmac.new(key.encode("utf-8"), ("square-location:" + location).encode("ascii"),
                    hashlib.sha256).hexdigest()


def _open_scoped_sync_proofs(db):
    db.execute("""CREATE TABLE IF NOT EXISTS scoped_sync_proofs (
        provider TEXT PRIMARY KEY, observed_at TEXT NOT NULL,
        location_seal TEXT NOT NULL)""")


def record_check(provider, error=None):
    """Retain a read outcome. Return False if observation storage failed.

    A successful provider read is not readiness evidence until this local
    observation commits. Callers serving owner-triggered reads must surface
    a failure rather than silently claiming a recorded result.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=2)) as db, db:
            _open_observation_table(db)
            _write_observation(db, provider, now, error)
        return True
    except (OSError, sqlite3.Error):
        log.warning("Integration check history could not be saved")
        return False


def record_committed_sync(provider, *, source_location=None):
    """Atomically record both an upstream read and its committed local snapshot.

    Call ONLY after an owner-initiated Gmail/Square adapter has returned from
    its successful persistence operation. The two observations share one UTC
    timestamp and SQLite commit: a failed second write cannot leave a fresh
    read-success marker with no corresponding sync success. No provider call,
    reconciliation, or remote permission verification happens here.

    Returns False if observation storage failed. In that case neither success
    is advanced by this call; the provider snapshot may still have committed.
    """
    if provider not in ("gmail", "square", "square_payments"):
        raise ValueError("Unsupported committed-sync observation")
    if source_location is not None and provider == "gmail":
        raise ValueError("Gmail does not accept a Square source location")
    seal = _square_location_seal(source_location) if source_location is not None else None
    if source_location is not None and seal is None:
        return False  # Fail closed: do not assert evidence without a stable seal.
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    try:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=2)) as db, db:
            _open_observation_table(db)
            _open_committed_sync_proofs(db)
            _open_scoped_sync_proofs(db)
            _write_observation(db, provider, now, None)
            _write_observation(db, provider + "_sync", now, None)
            db.execute("""INSERT INTO committed_sync_proofs(provider, observed_at)
                VALUES (?, ?) ON CONFLICT(provider) DO UPDATE SET
                observed_at=excluded.observed_at""", (provider, now))
            # A subsequent unscoped legacy observation must never inherit the
            # old location's seal. All changes share the same SQLite commit.
            db.execute("DELETE FROM scoped_sync_proofs WHERE provider=?", (provider,))
            if seal is not None:
                db.execute("""INSERT INTO scoped_sync_proofs
                    (provider, observed_at, location_seal) VALUES (?,?,?)""",
                    (provider, now, seal))
        return True
    except (OSError, sqlite3.Error):
        log.warning("Committed integration observation history could not be saved")
        return False


def integration_status(connections, source_locations=None):
    history = {}
    sync_proofs = {}
    scoped_proofs = {}
    source_locations = source_locations or {}
    history_available = True
    try:
        path = _db_path().resolve()
        if path.exists():
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                history = {row["provider"]: dict(row) for row in db.execute(
                    "SELECT * FROM integration_checks")}
                # Legacy databases remain readable. Older standalone sync
                # markers cannot retroactively prove a paired commit.
                exists = db.execute("""SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='committed_sync_proofs'""").fetchone()
                if exists:
                    sync_proofs = {row["provider"]: row["observed_at"]
                                   for row in db.execute(
                                       "SELECT provider, observed_at FROM committed_sync_proofs")}
                scoped_exists = db.execute("""SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='scoped_sync_proofs'""").fetchone()
                if scoped_exists:
                    scoped_proofs = {row["provider"]: (row["observed_at"], row["location_seal"])
                                     for row in db.execute(
                                         "SELECT provider, observed_at, location_seal FROM scoped_sync_proofs")}
    except (OSError, sqlite3.Error):
        history_available = False

    scope_unverified = {}
    # A legacy success marker has no source/location provenance. Do not
    # present it as a read for a newly configured Square seller location.
    for key in ("square", "square_payments"):
        if key not in source_locations:
            continue
        read_at = history.get(key, {}).get("last_success")
        sync_at = history.get(key + "_sync", {}).get("last_success")
        bound = scoped_proofs.get(key)
        seal = _square_location_seal(source_locations[key])
        verified = bool(seal and bound and bound == (read_at, seal)
                        and sync_at == read_at == sync_proofs.get(key)
                        and valid_observation_time(read_at))
        scope_unverified[key] = bool(read_at or sync_at) and not verified

    entries = []
    for key, name, connected in connections:
        previous = history.get(key, {})
        raw_success = previous.get("last_success")
        checked_success = valid_observation_time(raw_success)
        observation_invalid = raw_success is not None and checked_success is None
        if scope_unverified.get(key):
            checked_success = None
        if connected is None:
            state = "Not implemented"
        elif not connected:
            state = "Consent required in this session"
        elif observation_invalid:
            state = "Stored read observation invalid"
        elif scope_unverified.get(key):
            state = "Source-specific Square read/sync evidence unverified"
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
                                      or bool(scope_unverified.get(key))
                                      or bool(previous.get("needs_attention"))),
        })
    # A successful read is not evidence of a committed import/snapshot. Only
    # explicit post-commit markers from the owner-initiated flows qualify.
    sync_keys = ("gmail", "square", "square_payments")
    # Require a matching local proof written in the SAME transaction as
    # both observations. Older/unpaired success markers are retained for
    # diagnostic history but can never establish sync-tested readiness.
    sync_successes = {}
    sync_unpaired = {}
    sync_raw_successes = {}
    if history_available:
        for key in sync_keys:
            read_at = history.get(key, {}).get("last_success")
            sync_at = history.get(key + "_sync", {}).get("last_success")
            proof_at = sync_proofs.get(key)
            sync_raw_successes[key] = sync_at
            paired = bool(proof_at and read_at == sync_at == proof_at
                          and valid_observation_time(read_at)
                          and not scope_unverified.get(key))
            sync_successes[key] = sync_at if paired else None
            sync_unpaired[key] = bool(sync_at and not paired
                                      and not scope_unverified.get(key))
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
            "sync_successes": sync_successes, "sync_raw_successes": sync_raw_successes,
            "sync_unpaired": sync_unpaired,
            "scope_unverified": scope_unverified,
            "sync_failures": sync_failures}
