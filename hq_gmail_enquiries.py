"""Read-only import of owner-labelled business enquiries."""
from contextlib import closing
from email.utils import parseaddr
import os
from pathlib import Path
import re
import sqlite3

import requests
from customer_request_store import storage_ready
from hq_integration_health import PersistedSyncError

MAILBOX = "info@tommytintsltd.co.uk"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
ID = re.compile(r"[a-zA-Z0-9_-]{1,64}")


def _path():
    return Path(os.environ["HQ_DATA_DIR"]).resolve() / "gmail_enquiries.sqlite3"


def _get(token, endpoint, **params):
    response = requests.get(BASE + endpoint, params=params,
                            headers={"Authorization": "Bearer " + token}, timeout=(3, 5))
    response.raise_for_status()
    return response.json()


def verify_mailbox(token):
    if _get(token, "/profile").get("emailAddress", "").casefold() != MAILBOX:
        raise ValueError("Select the Tommy Tints business mailbox")


def import_page(token, page_token=None):
    if not storage_ready():
        raise PersistedSyncError("Persistent enquiry storage is unavailable")
    verify_mailbox(token)
    params = {"q": "in:inbox label:HQ-Enquiries", "maxResults": 10}
    if page_token:
        params["pageToken"] = page_token
    page = _get(token, "/messages", **params)
    rows = []
    for message in page.get("messages", []):
        ref = message["id"]
        if not ID.fullmatch(ref):
            raise RuntimeError("Invalid Gmail message reference")
        item = _get(token, "/messages/" + ref, format="full", fields="id,snippet,payload(headers)")
        if item.get("id") != ref:
            raise RuntimeError("Mismatched Gmail message reference")
        headers = {header["name"].lower(): header["value"] for header in item["payload"]["headers"]}
        rows.append((ref, parseaddr(headers.get("from", ""))[1][:254],
                     headers.get("subject", "")[:250], item.get("snippet", "")[:500]))
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS enquiries (
                id TEXT PRIMARY KEY, sender TEXT NOT NULL, subject TEXT NOT NULL,
                snippet TEXT NOT NULL, reviewed INTEGER NOT NULL DEFAULT 0)""")
            added = db.executemany("""INSERT INTO enquiries(id,sender,subject,snippet)
                VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING""", rows).rowcount
    except (OSError, sqlite3.Error) as exc:
        raise PersistedSyncError("Gmail enquiry storage commit failed") from exc
    return {"added": added, "read": len(rows), "next": page.get("nextPageToken")}


def queue(limit=50):
    if not storage_ready():
        return None, []
    path = _path()
    if not path.exists():
        return 0, []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        count = db.execute("SELECT COUNT(*) FROM enquiries WHERE reviewed=0").fetchone()[0]
        rows = db.execute("""SELECT id,sender,subject,snippet FROM enquiries
            WHERE reviewed=0 ORDER BY rowid DESC LIMIT ?""", (limit,)).fetchall()
    return count, rows


def review(ref):
    if not storage_ready() or not ID.fullmatch(ref) or not _path().exists():
        return False
    with closing(sqlite3.connect(_path(), timeout=5)) as db, db:
        return db.execute("UPDATE enquiries SET reviewed=1 WHERE id=?", (ref,)).rowcount == 1
