"""Owner-only, PII-minimised quotation-to-job provenance and change history.

Read-only. Existing internal records retain the same ID when a reviewed
quotation is approved and promoted into a job. Unknown legacy origins are
reported as unknown rather than guessed.
"""
import json

from customer_request_store import connect
from hq_core_records import _schema, read_work_record, WORK_TRANSITIONS

_HISTORY_LIMIT = 100
_FINANCIAL_FIELDS = ("price_pence", "deposit_pence")
_STATUS_FIELDS = ("status", "kind")


def _safe_values(raw, action):
    """Allowlist fields so old or future audit payloads never expose customer PII."""
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(values, dict):
        return {}
    cleaned = {}
    if action == "financials":
        for name in _FINANCIAL_FIELDS:
            value = values.get(name)
            if name in values and (value is None or (type(value) is int and 0 <= value <= 100000000)):
                cleaned[name] = value
    elif action == "status":
        value = values.get("status")
        if value in WORK_TRANSITIONS and isinstance(value, str):
            cleaned["status"] = value
        kind = values.get("kind")
        if kind in ("quote", "job"):
            cleaned["kind"] = kind
    return cleaned


def work_record_history(record_id):
    """A bounded, read-only history of an existing HQ work ID.

    Only the caller-facing Flask route can enforce owner authentication.
    No customer contact details, notes or arbitrary serialized audit data
    are returned. Pricing entries are owner-entered, not verified receipts.
    """
    record = read_work_record(record_id)  # validates ID and missing records
    with connect() as db:
        _schema(db)
        rows = db.execute("""
            SELECT revision,action,before_value,after_value,created_at
            FROM hq_work_audit WHERE work_id=? AND action IN ('status','financials')
            ORDER BY revision DESC LIMIT ?
        """, (record_id, _HISTORY_LIMIT + 1)).fetchall()
    has_earlier = len(rows) > _HISTORY_LIMIT
    events = []
    for row in reversed(rows[:_HISTORY_LIMIT]):
        events.append({
            "revision": row["revision"],
            "action": row["action"],
            "created_at": row["created_at"],
            "before": _safe_values(row["before_value"], row["action"]),
            "after": _safe_values(row["after_value"], row["action"]),
        })
    approved_at = next(
        (event["created_at"] for event in reversed(events)
         if event["action"] == "status" and event["after"].get("status") == "approved"),
        None,
    )
    origin = record["original_draft_kind"]
    if origin not in ("quote", "job"):
        origin = None
    return {
        "id": record["id"],
        "source_request_id": record["source_request_id"],
        "original_draft_kind": origin,
        "current_kind": record["kind"],
        "current_status": record["status"],
        "created_at": record["created_at"],
        "approved_from_quote": origin == "quote" and record["kind"] == "job",
        "approved_at": approved_at,
        "events": events,
        "has_earlier_events": has_earlier,
        "payment_verified": False,
    }
