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

# A manual HQ-Enquiries label is necessary, but not sufficient, to qualify
# messages for a *sales* queue. Keep recognisable legal, debt and enforcement
# correspondence in Gmail for separate handling without storing its content in
# the customer enquiry database. This is a conservative secondary safeguard;
# no subject/snippet keyword filter can classify every message perfectly.
_SENSITIVE_DOMAINS = ("hmrc.gov.uk", "justice.gov.uk", "courts.gov.uk")
_SENSITIVE_LOCAL_PART = re.compile(
    r"(?:legal|debts?|collections?|enforcement|litigation)(?:[._+\-].*)?\Z", re.I
)
_SENSITIVE_TEXT = re.compile(
    r"\b(?:"
    r"debt\s+(?:collection|recovery|collector|owed)|"
    r"(?:county|high)\s+court|"
    r"court\s+(?:claim|order|judg(?:e)?ment|summons)|"
    r"claim\s+(?:form|number)|"
    r"notice\s+of\s+(?:enforcement|intended\s+prosecution)|"
    r"enforcement\s+(?:agent|notice|action)|"
    r"bailiff|writ\s+of\s+control|statutory\s+demand|"
    r"letter\s+before\s+(?:claim|action)|"
    r"(?:penalty|parking)\s+charge\s+notice|"
    r"fixed\s+penalty\s+notice|"
    r"council\s+tax\s+arrears|late\s+filing\s+penalty|"
    r"pcn|ccj|n244|hmrc"
    r")\b", re.I
)


def _excluded_from_sales(sender, subject, snippet):
    """Only flag clear sensitive cues; no external AI or data transfer."""
    address = sender.casefold()
    local, separator, domain = address.partition("@")
    if separator and (
        any(domain == item or domain.endswith("." + item)
            for item in _SENSITIVE_DOMAINS)
        or _SENSITIVE_LOCAL_PART.fullmatch(local)
    ):
        return True
    return bool(_SENSITIVE_TEXT.search(subject + " " + snippet))


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


def _resolve_sales_label_ids(token):
    """Resolve user-label names to provider IDs for a fresh, per-message check.

    Gmail messages carry opaque label IDs, not label names. The list query
    alone is insufficient if labels change between search and message fetch.
    A missing/ambiguous HQ-Enquiries label must fail closed.
    """
    response = _get(token, "/labels", fields="labels(id,name)")
    listing = response.get("labels")
    if not isinstance(listing, list):
        raise RuntimeError("Business Gmail label metadata unavailable")
    names = {}
    for item in listing:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid Gmail label metadata")
        name = item.get("name")
        if name not in ("HQ-Enquiries", "HQ-Legal", "HQ-Debt"):
            continue
        ref = item.get("id")
        if name in names or not isinstance(ref, str) or not ID.fullmatch(ref):
            raise RuntimeError("Business Gmail labels are ambiguous or invalid")
        names[name] = ref
    if "HQ-Enquiries" not in names or len(set(names.values())) != len(names):
        raise RuntimeError("Business Gmail enquiry label is missing or ambiguous")
    return names


def import_page(token, page_token=None):
    if not storage_ready():
        raise PersistedSyncError("Persistent enquiry storage is unavailable")
    verify_mailbox(token)
    label_ids = _resolve_sales_label_ids(token)
    params = {"q": "in:inbox label:HQ-Enquiries -label:HQ-Legal -label:HQ-Debt", "maxResults": 10}
    if page_token:
        params["pageToken"] = page_token
    page = _get(token, "/messages", **params)
    rows = []
    skipped_sensitive = 0
    skipped_label_mismatch = 0
    for message in page.get("messages", []):
        ref = message["id"]
        if not ID.fullmatch(ref):
            raise RuntimeError("Invalid Gmail message reference")
        item = _get(token, "/messages/" + ref, format="full",
                    fields="id,labelIds,snippet,payload(headers)")
        if item.get("id") != ref:
            raise RuntimeError("Mismatched Gmail message reference")
        current_labels = item.get("labelIds")
        if (not isinstance(current_labels, list)
                or any(not isinstance(label, str) or not ID.fullmatch(label)
                       for label in current_labels)):
            raise RuntimeError("Gmail message labels cannot be verified")
        current = set(current_labels)
        if ("INBOX" not in current or label_ids["HQ-Enquiries"] not in current
                or any(label_ids.get(name) in current
                       for name in ("HQ-Legal", "HQ-Debt")
                       if label_ids.get(name))):
            skipped_label_mismatch += 1
            continue
        headers = {header["name"].lower(): header["value"] for header in item["payload"]["headers"]}
        sender = parseaddr(headers.get("from", ""))[1]
        subject, snippet = headers.get("subject", ""), item.get("snippet", "")
        if not all(isinstance(value, str) for value in (sender, subject, snippet)):
            raise RuntimeError("Invalid Gmail message fields")
        if _excluded_from_sales(sender, subject, snippet):
            skipped_sensitive += 1
            continue
        rows.append((ref, sender[:254], subject[:250], snippet[:500]))
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
    return {"added": added, "read": len(rows) + skipped_sensitive + skipped_label_mismatch,
            "skipped_sensitive": skipped_sensitive,
            "skipped_label_mismatch": skipped_label_mismatch,
            "next": page.get("nextPageToken")}


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
