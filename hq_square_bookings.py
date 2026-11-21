"""Read existing Square bookings into an internal, versioned snapshot."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3

import requests

from customer_request_store import storage_ready
from hq_integration_health import PersistedSyncError
from maintenance_calendar import timestamp


def configured():
    return bool(os.environ.get("HQ_SQUARE_READ_TOKEN") and os.environ.get("HQ_SQUARE_LOCATION_ID"))


def _path():
    return Path(os.environ["HQ_DATA_DIR"]).resolve() / "square_bookings.sqlite3"


def refresh_bookings():
    if not configured():
        raise RuntimeError("Square credentials unavailable")
    if not storage_ready():
        raise PersistedSyncError("Square booking snapshot storage unavailable")
    location = os.environ["HQ_SQUARE_LOCATION_ID"]
    now = datetime.now(timezone.utc)
    params = {"location_id": location, "limit": 100,
              "start_at_min": (now - timedelta(days=1)).isoformat(timespec="seconds"),
              "start_at_max": (now + timedelta(days=28)).isoformat(timespec="seconds")}
    headers = {"Authorization": "Bearer " + os.environ["HQ_SQUARE_READ_TOKEN"],
               "Square-Version": "2026-09-16"}
    rows = []
    cursors = set()
    seen_ids = set()
    for _ in range(10):
        response = requests.get("https://connect.squareup.com/v2/bookings",
                                headers=headers, params=params, timeout=(3, 5))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("errors"):
            raise RuntimeError("Square did not return booking data")
        # The requested provider page is limited to 100 bookings. A malformed
        # list or an ambiguous repeated ID is not a complete, reliable source
        # snapshot, even if individual booking versions look plausible.
        bookings = payload.get("bookings", [])
        if not isinstance(bookings, list) or len(bookings) > 100:
            raise RuntimeError("Square booking page cannot be verified")
        for item in bookings:
            if not isinstance(item, dict):
                raise RuntimeError("Square returned an invalid booking")
            ref, version, status = (item.get("id"), item.get("version"),
                                    item.get("status"))
            if (item.get("location_id") != location or not isinstance(ref, str)
                    or not 1 <= len(ref) <= 192 or not ref.isascii()
                    or not ref.isprintable() or any(ch.isspace() for ch in ref)
                    or type(version) is not int or version < 0
                    or not isinstance(status, str) or not 1 <= len(status) <= 64
                    or not status.isascii() or not status.isprintable()):
                raise RuntimeError("Square returned an invalid booking")
            if ref in seen_ids:
                raise RuntimeError("Square returned duplicate booking identifiers")
            seen_ids.add(ref)
            start = timestamp(item.get("start_at")).astimezone(timezone.utc)
            rows.append((location, ref, version, status, start.isoformat()))
        # An omitted cursor is the only unambiguous end-of-pages signal.
        # Empty, non-string and repeated cursors must not be mistaken for
        # a successfully complete read-and-persist operation.
        cursor = payload.get("cursor")
        if cursor is None:
            break
        if (not isinstance(cursor, str) or not 1 <= len(cursor) <= 2000
                or not cursor.isascii() or not cursor.isprintable()
                or any(ch.isspace() for ch in cursor) or cursor in cursors):
            raise RuntimeError("Square returned an invalid pagination cursor")
        cursors.add(cursor)
        params["cursor"] = cursor
    else:
        raise RuntimeError("Square booking window exceeds the page limit")

    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            # Keep the version check and UPSERT in one writer transaction.
            # A provider replay with the SAME version but different booking
            # status/time is contradictory evidence, not a new appointment.
            # Abort the whole refresh before applying any other rows.
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS bookings (
                location TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
                status TEXT NOT NULL, start_at TEXT NOT NULL, PRIMARY KEY(location, id))""")
            for scoped_location, ref, version, status, start_at in rows:
                previous = db.execute(
                    "SELECT version,status,start_at FROM bookings WHERE location=? AND id=?",
                    (scoped_location, ref)).fetchone()
                if (previous is not None and previous[0] == version
                        and (previous[1] != status or previous[2] != start_at)):
                    raise RuntimeError(
                        "Square returned conflicting evidence for an unchanged booking version")
            changed = db.executemany("""INSERT INTO bookings VALUES (?,?,?,?,?)
                ON CONFLICT(location,id) DO UPDATE SET
                version=excluded.version, status=excluded.status, start_at=excluded.start_at
                WHERE excluded.version > bookings.version""", rows).rowcount
        return changed
    except (OSError, sqlite3.Error) as exc:
        raise PersistedSyncError("Square booking snapshot commit failed") from exc


def upcoming_bookings():
    if not storage_ready() or not configured() or not _path().exists():
        return []
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(_path().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        return db.execute("""SELECT id, status, start_at FROM bookings
            WHERE location=? AND start_at>=? ORDER BY start_at LIMIT 100""",
            (os.environ["HQ_SQUARE_LOCATION_ID"], now)).fetchall()
