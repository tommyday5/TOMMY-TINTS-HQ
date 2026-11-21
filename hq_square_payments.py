"""Owner-only, read-only Square payment evidence; never reconciles or books cash.

Square payment data is not an HQ invoice, bank settlement or permission to
change customer jobs. Fetches are explicitly owner-initiated (CSRF protected).
Only minimal provider status/amount evidence is retained on the /data volume.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import secrets
import sqlite3

import requests
from flask import Blueprint, abort, jsonify, render_template_string, request, session

from customer_request_store import storage_ready
from hq_auth import required
from hq_integration_health import PersistedSyncError, record_check, record_committed_sync

payment_evidence = Blueprint("payment_evidence", __name__)
_API = "https://connect.squareup.com/v2/payments"
_API_VERSION = "2026-09-16"
_STATUSES = frozenset({"APPROVED", "PENDING", "COMPLETED", "CANCELED", "FAILED"})


def configured():
    return bool(os.environ.get("HQ_SQUARE_READ_TOKEN") and os.environ.get("HQ_SQUARE_LOCATION_ID"))


def _path():
    return Path(os.environ["HQ_DATA_DIR"]).resolve() / "square_payment_evidence.sqlite3"


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Square returned an invalid payment timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Square returned an invalid payment timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("Square returned an unzoned payment timestamp")
    return parsed.astimezone(timezone.utc).isoformat()


def _minor(value, currency):
    if (not isinstance(value, dict) or type(value.get("amount")) is not int
            or not 0 <= value["amount"] <= 10**12 or value.get("currency") != currency):
        raise ValueError("Square returned an invalid payment amount")
    return value["amount"]


def _payment(item, location):
    if not isinstance(item, dict):
        raise ValueError("Square returned an invalid payment")
    ref = item.get("id")
    status = item.get("status")
    currency = item.get("total_money", {}).get("currency") if isinstance(item.get("total_money"), dict) else None
    if (not isinstance(ref, str) or not 1 <= len(ref) <= 192
            or any(ord(ch) < 33 or ord(ch) > 126 for ch in ref)
            or item.get("location_id") != location
            or status not in _STATUSES
            or not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha()):
        raise ValueError("Square returned an invalid payment")
    currency = currency.upper()
    total = _minor(item.get("total_money"), currency)
    refunded = item.get("refunded_money")
    refunded_minor = _minor(refunded, currency) if refunded is not None else None
    if refunded_minor is not None and refunded_minor > total:
        raise ValueError("Square returned inconsistent refund details")
    created_at = _timestamp(item.get("created_at"))
    updated_at = _timestamp(item.get("updated_at"))
    # A provider payment cannot be updated before it was created. Reject
    # impossible future evidence as well (allow five minutes of clock skew).
    # These checks do not authenticate the payment or establish bank cash.
    if (datetime.fromisoformat(updated_at) < datetime.fromisoformat(created_at)
            or datetime.fromisoformat(updated_at)
            > datetime.now(timezone.utc) + timedelta(minutes=5)):
        raise ValueError("Square returned inconsistent payment timestamps")
    return (ref, status, total, refunded_minor, currency,
            created_at, updated_at)


def refresh_payments():
    """Fetch complete bounded 30-day window before replacing local snapshot.

    Fail closed on provider errors, invalid money, duplicate IDs, or truncated
    pagination. No half-refreshed snapshots and no automatic job matching.
    """
    if not configured():
        raise RuntimeError("Square payment read access unavailable")
    if not storage_ready():
        raise PersistedSyncError("Square payment snapshot storage unavailable")
    location = os.environ["HQ_SQUARE_LOCATION_ID"]
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=30)).isoformat(timespec="seconds")
    end = now.isoformat(timespec="seconds")
    params = {"location_id": location, "begin_time": start, "end_time": end, "limit": 100}
    headers = {"Authorization": "Bearer " + os.environ["HQ_SQUARE_READ_TOKEN"],
               "Square-Version": _API_VERSION}
    rows, seen_ids, cursors = [], set(), set()
    for _ in range(10):
        response = requests.get(_API, params=params, headers=headers, timeout=(3, 7))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("errors"):
            raise ValueError("Square returned invalid payment results")
        # Square's requested limit is 100 records per page. A larger or
        # malformed page cannot be treated as complete provider evidence.
        payments = payload.get("payments", [])
        if not isinstance(payments, list) or len(payments) > 100:
            raise ValueError("Square payment page cannot be verified")
        for item in payments:
            row = _payment(item, location)
            if row[0] in seen_ids:
                raise ValueError("Square returned duplicate payment identifiers")
            seen_ids.add(row[0])
            rows.append(row)
        # Only an absent cursor signals completion. An empty, whitespace,
        # malformed or replayed cursor could silently truncate this snapshot.
        cursor = payload.get("cursor")
        if cursor is None:
            break
        if (not isinstance(cursor, str) or not 1 <= len(cursor) <= 2000
                or not cursor.isascii() or not cursor.isprintable()
                or any(ch.isspace() for ch in cursor) or cursor in cursors):
            raise ValueError("Square returned an invalid pagination cursor")
        cursors.add(cursor)
        params["cursor"] = cursor
    else:
        raise ValueError("Square payment window exceeds the safe page limit")

    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, total_minor INTEGER NOT NULL,
                refunded_minor INTEGER, currency TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS snapshot (
                id INTEGER PRIMARY KEY CHECK(id=1), location TEXT NOT NULL,
                refreshed_at TEXT NOT NULL, window_start TEXT NOT NULL,
                window_end TEXT NOT NULL, item_count INTEGER NOT NULL)""")
            db.execute("DELETE FROM payments")
            db.executemany("INSERT INTO payments VALUES(?,?,?,?,?,?,?)", rows)
            db.execute("""INSERT INTO snapshot VALUES(1,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET location=excluded.location,
                refreshed_at=excluded.refreshed_at, window_start=excluded.window_start,
                window_end=excluded.window_end, item_count=excluded.item_count""",
                (location, datetime.now(timezone.utc).isoformat(), start, end, len(rows)))
        return len(rows)
    except (OSError, sqlite3.Error) as exc:
        raise PersistedSyncError("Square payment snapshot commit failed") from exc




def snapshot_metadata_consistent(db, meta):
    """Validate bounded local snapshot coverage, not payment authenticity.

    A count mismatch, impossible reporting window or future-dated refresh
    cannot be evidence for a later exact-reference HQ job comparison.
    Call on the same SQLite read transaction as the matching payment rows.
    """
    count = meta["item_count"]
    if type(count) is not int or not 0 <= count <= 1000:
        return False
    if db.execute("SELECT COUNT(*) FROM payments").fetchone()[0] != count:
        return False
    try:
        start = datetime.fromisoformat(_timestamp(meta["window_start"]))
        end = datetime.fromisoformat(_timestamp(meta["window_end"]))
        refreshed = datetime.fromisoformat(_timestamp(meta["refreshed_at"]))
    except (TypeError, ValueError, OverflowError):
        return False
    return (start < end <= refreshed
            and end - start <= timedelta(days=31)
            and refreshed <= datetime.now(timezone.utc) + timedelta(minutes=5))


def read_snapshot():
    """Read only a stored snapshot; missing access never implies zero payments."""
    result = {"configured": configured(), "snapshot_available": False,
              "stale": True, "last_refreshed_at": None, "window_start": None,
              "window_end": None, "payment_count": None, "payments": [],
              "reconciliation_status": "not_linked",
              "verified_job_receipts_pence": None, "verified_job_outstanding_pence": None,
              "bank_settlement_verified": False, "automatic_job_matching": False,
              "scope_unverified": False, "integrity_issue": False}
    if not storage_ready() or not _path().exists():
        return result
    with closing(sqlite3.connect(_path().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")  # All local metadata/count/rows use one read snapshot.
        meta = db.execute("SELECT * FROM snapshot WHERE id=1").fetchone()
        if not meta:
            return result
        # A previously cached seller/location snapshot is not evidence for
        # the currently configured Square account. Never return its counts,
        # identifiers or amounts after credentials or location are changed.
        # Keep the database intact so an owner can restore the prior setup.
        stored_location = meta["location"]
        active_location = os.environ.get("HQ_SQUARE_LOCATION_ID", "")
        if (not configured() or not isinstance(stored_location, str)
                or not stored_location.isascii() or not active_location.isascii()
                or not secrets.compare_digest(stored_location, active_location)):
            result["scope_unverified"] = True
            return result
        if not snapshot_metadata_consistent(db, meta):
            # A missing/tampered payment row or impossible metadata is NOT
            # evidence of zero receipts. Withhold all figures until refreshed.
            result["integrity_issue"] = True
            return result
        rows = db.execute("""SELECT id,status,total_minor,refunded_minor,currency,
            created_at,updated_at FROM payments ORDER BY created_at DESC,id DESC LIMIT 50""").fetchall()
    refreshed = _timestamp(meta["refreshed_at"])
    age = datetime.now(timezone.utc) - datetime.fromisoformat(refreshed)
    result.update(snapshot_available=True, stale=age < timedelta(0)
                  or age > timedelta(hours=24), last_refreshed_at=refreshed,
                  window_start=meta["window_start"], window_end=meta["window_end"],
                  payment_count=meta["item_count"], payments=[dict(r) for r in rows])
    return result


@payment_evidence.get("/api/hq/square-payment-evidence")
@required
def api_snapshot():
    try:
        return jsonify(read_snapshot())
    except (OSError, sqlite3.Error, ValueError):
        return jsonify(error="Payment evidence storage is unavailable"), 503


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Square payment evidence | Tommy Tints HQ</title>
<style>
body{font:16px system-ui;background:#0b0f16;color:#eef2f8;margin:0}
main{max-width:980px;margin:24px auto;padding:18px}
a{color:#ff8088}.panel{background:#151e2b;border:1px solid #303d50;border-radius:12px;padding:18px;margin:16px 0}
button{background:#c82737;color:white;border:0;border-radius:7px;padding:12px;cursor:pointer}
table{width:100%;border-collapse:collapse;word-break:break-word}
td,th{padding:10px;text-align:left;border-bottom:1px solid #3a4554}
small{color:#afb9c7}@media(max-width:650px){table{font-size:12px}td,th{padding:5px}}
</style></head><body><main><a href="/">Back to HQ</a>
<h1>Square payment evidence</h1>
<p>Read-only provider evidence. No customer or job is matched automatically.
Amounts are not confirmed bank deposits, settled cash, invoices or HQ revenue.
Refunds and processing fees can affect net proceeds.</p>
{% if error %}<div class="panel" role="alert">{{ error }}</div>{% endif %}
<section class="panel">
<p><strong>Provider connection:</strong> {{ "Configured" if data.configured else "Not configured" }}.
<strong>Snapshot:</strong> {{ "Stale / read access unavailable" if data.stale else "Recently refreshed" }}.</p>
{% if data.snapshot_available %}
<p>Last refresh: {{ data.last_refreshed_at }} UTC. Source window: {{ data.window_start }} to {{ data.window_end }}.
{{ data.payment_count }} provider payment records (showing at most 50).</p>
{% elif data.scope_unverified %}
<p>Stored historical Square evidence is withheld because the current configured
location or read access cannot verify its scope. Reconnect/refresh the intended
location before reviewing payments. This is not evidence of zero receipts.</p>
{% elif data.integrity_issue %}
<p>The local Square snapshot metadata or row count cannot be verified.
Payment identifiers and amounts are withheld. Refresh the read-only provider
snapshot; do not treat this as evidence of zero receipts.</p>
{% else %}<p>No provider payment snapshot has been retrieved; this is not evidence of zero receipts.</p>{% endif %}
{% if data.configured %}
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<button type="submit">Refresh read-only Square payment evidence</button></form>
{% else %}<p>Owner must configure a Square token with PAYMENTS_READ and the Square location ID before a refresh is possible.</p>{% endif %}
</section>
{% if data.payments %}<div class="panel"><table><thead><tr>
<th>Payment ID</th><th>Status</th><th>Provider amount</th><th>Reported refund</th><th>Created (UTC)</th>
</tr></thead><tbody>{% for p in data.payments %}
<tr><td>{{ p.id }}</td><td>{{ p.status }}</td>
<td>{{ p.total_minor }} minor units ({{ p.currency }})</td>
<td>{{ p.refunded_minor if p.refunded_minor is not none else "Not supplied" }}</td>
<td>{{ p.created_at }}</td></tr>{% endfor %}
</tbody></table></div>{% endif %}
<p><a href="/hq/square-payment-review">Compare one exact Square payment reference with an approved HQ job (read-only)</a></p>
<p><small>Snapshot contains minimal payment identifiers, timestamps, statuses and amounts only.
No card, buyer identity, access token or payment method details are stored.
Reconciliation with HQ jobs is deliberately disabled until exact matching and approval controls are implemented.</small></p>
</main></body></html>"""


@payment_evidence.route("/hq/square-payment-evidence", methods=["GET", "POST"])
@required
def payment_page():
    error = None
    error_status = 502
    if request.method == "POST":
        if (request.content_length is not None and request.content_length > 2000):
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
            abort(403)
        if not configured():
            return "Square payment read access is not configured", 503
        try:
            refresh_payments()
        except PersistedSyncError:
            record_check("square_payments_sync", "Square payment snapshot commit failed")
            error = "Unable to store the Square payment snapshot. Check persistent storage."
        except Exception:
            # No response bodies, customer data, URLs containing tokens or secrets logged.
            record_check("square_payments", "Square payment read failed")
            error = "Unable to refresh payment evidence. Check read-only Square access and PAYMENTS_READ permission."
        else:
            # Snapshot persistence and readiness observation are separate
            # commits. A missing observation must not look like a clean sync.
            if not record_committed_sync(
                    "square_payments", source_location=os.environ.get("HQ_SQUARE_LOCATION_ID")):
                error = ("Square payment snapshot was stored, but integration "
                         "read/sync evidence could not be recorded. Check "
                         "Integration Health storage; sync remains unverified.")
                error_status = 503
    try:
        data = read_snapshot()
    except (OSError, sqlite3.Error, ValueError):
        return "Payment evidence storage is unavailable", 503
    return render_template_string(_PAGE, data=data, error=error,
                                  csrf=session.get("csrf")), (error_status if error else 200)
