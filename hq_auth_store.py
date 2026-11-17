"""Durable owner sessions, passkey credentials and single-use challenges."""
from contextlib import contextmanager, closing
import os
from pathlib import Path
import sqlite3
import time


@contextmanager
def auth_db():
    root = Path(os.environ.get("HQ_DATA_DIR", "/tmp/tommy-tints-hq"))
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "owner_security.sqlite3", timeout=5)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, email TEXT NOT NULL, subject TEXT NOT NULL,
            issued INTEGER NOT NULL, expires INTEGER NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS passkeys (
            id BLOB PRIMARY KEY, email TEXT NOT NULL, subject TEXT NOT NULL,
            public_key BLOB NOT NULL, counter INTEGER NOT NULL, created INTEGER NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS used_challenges (
            id BLOB PRIMARY KEY, expires INTEGER NOT NULL)""")
        yield db


def consume(db, challenge, expires):
    db.execute("DELETE FROM used_challenges WHERE expires<=?", (int(time.time()),))
    db.execute("INSERT INTO used_challenges VALUES (?,?)", (challenge, expires))
