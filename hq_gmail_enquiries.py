"""Read-only import of owner-labelled business enquiries."""
from contextlib import closing
from email.utils import getaddresses
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


class EnquiryLabelError(RuntimeError):
    """Required owner-managed sales labels cannot safely be resolved."""


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
            raise EnquiryLabelError("Business Gmail labels are ambiguous or invalid")
        names[name] = ref
    if "HQ-Enquiries" not in names or len(set(names.values())) != len(names):
        raise EnquiryLabelError("Business Gmail enquiry label is missing or ambiguous")
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
        # A sales enquiry must contain one usable reply-to identity. A
        # missing, duplicated or multi-address From header is ambiguous; do
        # not queue any part of the page before the owner resolves it in Gmail.
        # This validates metadata shape, NOT the sender's authenticity.
        payload = item.get("payload")
        raw_headers = payload.get("headers") if isinstance(payload, dict) else None
        if (not isinstance(raw_headers, list) or len(raw_headers) > 128
                or any(not isinstance(h, dict)
                       or not isinstance(h.get("name"), str)
                       or not isinstance(h.get("value"), str)
                       or len(h["value"]) > 16384 for h in raw_headers)):
            raise RuntimeError("Gmail message headers cannot be verified")
        from_values = [h["value"] for h in raw_headers if h["name"].lower() == "from"]
        if len(from_values) != 1 or not 1 <= len(from_values[0]) <= 2048:
            raise RuntimeError("Gmail enquiry sender cannot be verified")
        addresses = getaddresses(from_values)
        sender = addresses[0][1] if len(addresses) == 1 else ""
        if (not 1 <= len(sender) <= 254 or sender.count("@") != 1
                or not all(sender.split("@"))
                or any(c.isspace() or ord(c) < 33 for c in sender)):
            raise RuntimeError("Gmail enquiry sender cannot be verified")
        headers = {h["name"].lower(): h["value"] for h in raw_headers}
        subject, snippet = headers.get("subject", ""), item.get("snippet", "")
        if not all(isinstance(value, str) for value in (subject, snippet)):
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


def profile_error_diagnostic(exc):
    """Allowlist Google reason codes; never persist/echo arbitrary provider text."""
    response = getattr(exc, 'response', None)
    status = response.status_code if response is not None else None
    reasons = set()
    project = None
    try:
        if response is not None and len(response.content) <= 65536:
            payload = response.json().get('error', {})
            for item in payload.get('errors', []):
                if isinstance(item, dict) and isinstance(item.get('reason'), str):
                    reasons.add(item['reason'])
            for item in payload.get('details', []):
                if not isinstance(item, dict): continue
                if (item.get('@type') == 'type.googleapis.com/google.rpc.ErrorInfo'
                        and item.get('domain') == 'googleapis.com'):
                    reasons.add(item.get('reason', ''))
                    meta = item.get('metadata', {})
                    if (isinstance(meta, dict) and meta.get('service') == 'gmail.googleapis.com'
                            and isinstance(meta.get('consumer'), str)
                            and re.fullmatch(r'projects/[0-9]{1,24}', meta['consumer'])):
                        project = meta['consumer'].split('/')[1]
    except (ValueError, TypeError, AttributeError):
        pass
    code = 'unknown'
    action = 'Gmail API verification failed. Retry Connect Gmail; if it persists, check Gmail API, OAuth client project and Workspace API controls.'
    if status == 401:
        code, action = 'authError', 'Google rejected the access token. Reconnect Gmail with the business mailbox.'
    elif status == 403:
        if reasons & {'SERVICE_DISABLED', 'accessNotConfigured'}:
            code, action = 'api_disabled', 'Google reports Gmail API disabled or not configured for the calling project. Enable gmail.googleapis.com in the project owning the deployed OAuth client, then reconnect Gmail.'
        elif reasons & {'ACCESS_TOKEN_SCOPE_INSUFFICIENT', 'insufficientPermissions'}:
            code, action = 'scope_insufficient', 'Google reports insufficient permission. Reconnect and grant gmail.readonly to the business mailbox; do not add write/send permissions.'
        elif 'domainPolicy' in reasons:
            code, action = 'workspace_policy', 'Google reports a Workspace domain policy block. Ask the Workspace administrator to allow this existing OAuth app access to Gmail read-only.'
        elif reasons & {'dailyLimitExceeded','rateLimitExceeded','userRateLimitExceeded','quotaExceeded','RATE_LIMIT_EXCEEDED'}:
            code, action = 'quota_limit', 'Google reports a quota or rate limit. Wait and check Gmail API quota for the existing client project before retrying.'
        else:
            code, action = 'forbidden_unknown', 'Gmail API access denied (HTTP 403); Google supplied no recognised safe reason. Check Gmail API enablement, Workspace API controls and the OAuth client project; the cause is not yet confirmed.'
    # Numeric project number is configuration metadata, not a credential.
    if project and code == 'api_disabled':
        action += ' Google identifies consumer project ' + project + '. Open https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=' + project
    return 'Gmail profile HTTP ' + str(status or 'unknown') + ' [' + code + ']', action


def import_error_diagnostic(exc):
    """Return fixed actionable import diagnostics without mailbox content."""
    if isinstance(exc, EnquiryLabelError):
        return ("Gmail enquiry label setup required",
                "Gmail connected, but the HQ-Enquiries label is missing or ambiguous. "
                "In the business Gmail account, create a label named exactly HQ-Enquiries "
                "and apply it only to genuine customer enquiries in the Inbox. "
                "Keep legal/debt mail under HQ-Legal or HQ-Debt. HQ has not created "
                "or changed any Gmail labels. Then retry Import latest page.")
    if isinstance(exc, requests.HTTPError):
        diagnostic, action = profile_error_diagnostic(exc)
        return diagnostic.replace("Gmail profile HTTP", "Gmail import HTTP"), action
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return "Gmail import network unavailable", "Gmail could not be reached. Retry the import later; existing enquiries are preserved."
    if isinstance(exc, ValueError):
        return "Gmail import mailbox or response invalid", "Gmail mailbox or response verification failed. Reconnect using the business mailbox."
    return "Read-only Gmail import failed", "Gmail import failed during response validation. Existing enquiries are preserved. Check Integration Health and contact support if it persists."
