"""Owner-triggered, bounded private SQLite recovery snapshots. Never restores live data."""
from contextlib import closing
from pathlib import Path
import fcntl
import os
import re
import sqlite3
import stat
import tempfile
import time

MAX_BYTES = 16 * 1024 * 1024
MAX_SECONDS = 8
MAX_BACKUPS = 3
_ID = re.compile(r"[0-9a-f]{32}")


def _paths():
    root = Path(os.environ.get("HQ_DATA_DIR", ""))
    source = root / "customer_requests.sqlite3"
    if not root.is_absolute() or not source.is_file() or source.is_symlink():
        raise LookupError("Canonical database unavailable")
    return source, root / "private_recovery_snapshots"


def _directory(directory):
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise ValueError("Recovery directory is not private")


def _existing(directory):
    if not directory.exists():
        return []
    _directory(directory)
    items = []
    for path in directory.glob("*.sqlite3"):
        if not _ID.fullmatch(path.stem):
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ValueError("Recovery file is not private")
        items.append(dict(id=path.stem, bytes=info.st_size, created_at=info.st_mtime))
    return sorted(items, key=lambda x: x["created_at"], reverse=True)


def list_snapshots():
    _, directory = _paths()
    return _existing(directory)


def _copy(source, target, deadline):
    size = source.execute("PRAGMA page_size").fetchone()[0]
    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise ValueError("Recovery check time limit exceeded")
        if total * size > MAX_BYTES:
            raise ValueError("Database exceeds recovery size limit")
    progress(0, 0, source.execute("PRAGMA page_count").fetchone()[0])
    source.backup(target, pages=64, progress=progress, sleep=0.01)


def _check(db, deadline):
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
        raise ValueError("Recovery integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Recovery foreign key check failed")
    schema = db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    counts = []
    for name, sql in schema:
        quoted = '"' + name.replace('"','""') + '"'
        counts.append((name, sql, db.execute("SELECT COUNT(*) FROM "+quoted).fetchone()[0]))
    return counts


def _restore_check(path, deadline):
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=1)) as saved, \
         closing(sqlite3.connect(":memory:")) as restored:
        saved.execute("PRAGMA query_only=ON")
        _copy(saved, restored, deadline)
        if _check(saved, deadline) != _check(restored, deadline):
            raise ValueError("Restored schema or row counts differ")


def create_snapshot(operation_id):
    if not isinstance(operation_id,str) or not _ID.fullmatch(operation_id):
        raise ValueError("Invalid recovery operation")
    source, directory = _paths()
    _directory(directory)
    deadline = time.monotonic() + MAX_SECONDS
    final = directory / (operation_id + ".sqlite3")
    # Serialize across Gunicorn workers; never overwrite or delete completed backups.
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        existing = _existing(directory)
        if any(x["id"] == operation_id for x in existing):
            _restore_check(final, deadline)
            return operation_id
        if len(existing) >= MAX_BACKUPS:
            raise ValueError("Recovery snapshot capacity reached")
        # Bound orphaned temporary files after crashes without deleting evidence.
        if any(directory.glob(".pending-*")):
            raise ValueError("Incomplete recovery file requires administrator review")
        temp_fd, temp_name = tempfile.mkstemp(prefix=".pending-",dir=directory)
        os.close(temp_fd)
        pending = Path(temp_name)
        try:
            with closing(sqlite3.connect(source.as_uri()+"?mode=ro",uri=True,timeout=1)) as live, \
                 closing(sqlite3.connect(str(pending),timeout=1)) as dest:
                live.execute("PRAGMA query_only=ON")
                _copy(live,dest,deadline)
                dest.execute("PRAGMA journal_mode=DELETE")
            _restore_check(pending, deadline)
            if time.monotonic() >= deadline:
                raise ValueError("Recovery time limit exceeded")
            with pending.open("rb") as handle:
                os.fsync(handle.fileno())
            os.link(pending,final)  # atomic no-clobber publication
            pending.unlink()
            dir_fd = os.open(directory,os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            pending.unlink(missing_ok=True)
        return operation_id
    finally:
        os.close(fd)
