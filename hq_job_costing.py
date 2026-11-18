"""Owner-only, read-only costing snapshot for an internal HQ work record.

The approved sale figure always comes from HQ work records, never from the
editable costing log. No customer PII or unverified payment information is
returned. This module never updates prices, jobs, invoices or bookings.
"""
from decimal import Decimal, InvalidOperation

from hq_core_records import read_work_record
from job_material_store import read_log
from job_profitability import profitability


APPROVED_STATUSES = frozenset(("approved", "in_progress", "completed"))
COST_FIELDS = ("lines", "labour", "actual_labour_rate", "overhead", "other_costs")


def _filled(value):
    return value is not None and str(value).strip() != ""


def costing_snapshot(owner, record_id):
    """Return a bounded internal profitability view; fail closed on missing data."""
    if not isinstance(owner, str) or not owner:
        raise ValueError("Owner identity missing")
    record = read_work_record(record_id)  # validates identifier and existence
    log = read_log(owner, record_id)
    payload = log["data"]
    approved = record["status"] in APPROVED_STATUSES
    price_pence = record["price_pence"]
    snapshot = {
        "id": record_id,
        "status": record["status"],
        "price_pence": price_pence,
        "price_source": "internal_hq_work_record",
        "price_approved": approved,
        "payment_verified": False,
        "costing_saved": payload is not None,
        "costing_revision": log["revision"],
        "complete_costs": False,
        "saved_sale_matches_record": None,
        "total_recorded_cost": None,
        "materials": None,
        "actual_labour_cost": None,
        "estimated_contribution": None,
        "margin_percent": None,
        "note": "Internal estimates only. Payment is unverified; tax and omitted costs may alter profit.",
    }
    if payload is None:
        return snapshot
    if not isinstance(payload, dict):
        raise ValueError("Saved costing data is invalid")

    saved_sale = payload.get("sale")
    if _filled(saved_sale) and price_pence is not None:
        try:
            saved = Decimal(str(saved_sale))
            if saved.is_finite():
                snapshot["saved_sale_matches_record"] = saved * 100 == Decimal(price_pence)
        except (InvalidOperation, ValueError, TypeError):
            pass

    # A missing price must not become an invented £0 sale.
    # An incomplete cost log must never display an apparently reliable margin.
    complete = (
        isinstance(payload.get("lines"), list)
        and all(_filled(payload.get(field)) for field in COST_FIELDS if field != "lines")
    )
    snapshot["complete_costs"] = complete
    if price_pence is None:
        return snapshot
    try:
        report = profitability({
            **payload, "sale": str(Decimal(price_pence) / 100)
        })
    except (ValueError, TypeError, KeyError):
        # A legacy malformed log must not produce an invented cost or a 500.
        snapshot["complete_costs"] = False
        snapshot["note"] = "Saved costing is invalid; review and correct the log before relying on figures."
        return snapshot
    snapshot["materials"] = report["materials"]
    snapshot["actual_labour_cost"] = report["labour_cost"]
    snapshot["total_recorded_cost"] = report["total_recorded_cost"]
    if complete and approved and report["complete_costs"]:
        snapshot["estimated_contribution"] = report["estimated_contribution"]
        snapshot["margin_percent"] = report["margin_percent"]
    return snapshot
