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
        "customer_summary_ready": False,
        "customer_summary_missing": [],
        "complete_costs": False,
        "saved_sale_matches_record": None,
        "total_recorded_cost": None,
        "materials": None,
        "material_line_count": None,
        "labour_hours": None,
        "actual_labour_rate": None,
        "actual_labour_cost": None,
        "allocated_overhead": None,
        "other_costs": None,
        "missing_cost_fields": [],
        "estimated_contribution": None,
        "margin_percent": None,
        "note": "Internal estimates only. Payment is unverified; tax and omitted costs may alter profit.",
    }
    # Customer previews use the separate, existing /customer-breakdown route.
    # Report readiness without exposing work descriptions, PII or private costs.
    # The preview requires approved HQ pricing and saved, nonempty work stages;
    # it does NOT depend on completeness of the private profitability log.
    missing_summary = []
    if not approved or type(price_pence) is not int or not 0 <= price_pence <= 100000000:
        missing_summary.append("approved_price")
    if not isinstance(payload, dict) or not isinstance(payload.get("stages"), str) or not any(
        stage.strip() for stage in payload["stages"].splitlines()
    ):
        missing_summary.append("saved_work_stages")
    snapshot["customer_summary_missing"] = missing_summary
    snapshot["customer_summary_ready"] = not missing_summary

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

    # A complete cost log is independent of whether the job has an approved
    # selling price. Keep missing components absent, never silently £0.
    missing = []
    if not isinstance(payload.get("lines"), list):
        missing.append("lines")
    missing.extend(field for field in COST_FIELDS[1:] if not _filled(payload.get(field)))
    complete = not missing
    snapshot["missing_cost_fields"] = missing
    snapshot["complete_costs"] = complete
    # The profitability helper supports independent manual calculations and
    # defaults absent materials to []. An imported/legacy HQ log with NO
    # materials list is different from a reviewed, explicitly empty list:
    # never infer £0 materials or silently produce a contribution from it.
    if "lines" in missing:
        snapshot["note"] = (
            "Saved costing is missing a valid materials list. Review the "
            "log before relying on cost totals or contribution."
        )
        return snapshot
    try:
        # Zero is a calculation placeholder ONLY when the authoritative HQ
        # price is absent; such records never expose contribution or margin.
        report = profitability({
            **payload,
            "sale": str(Decimal(price_pence) / 100) if price_pence is not None else "0",
        })
    except (ValueError, TypeError, KeyError, InvalidOperation):
        # A malformed legacy log must not manufacture cost figures or a 500.
        snapshot["complete_costs"] = False
        snapshot["note"] = "Saved costing is invalid; review and correct the log before relying on figures."
        return snapshot

    snapshot["material_line_count"] = len(payload["lines"])
    snapshot["materials"] = report["materials"]
    if "labour" not in missing:
        snapshot["labour_hours"] = str(Decimal(str(payload["labour"])))
    if "actual_labour_rate" not in missing:
        snapshot["actual_labour_rate"] = str(Decimal(str(payload["actual_labour_rate"])))
    if "labour" not in missing and "actual_labour_rate" not in missing:
        snapshot["actual_labour_cost"] = report["labour_cost"]
    if "overhead" not in missing:
        snapshot["allocated_overhead"] = report["overhead"]
    if "other_costs" not in missing:
        snapshot["other_costs"] = report["other_costs"]
    # Do not present a total that quietly treats missing labour / overhead
    # as zero. Individual validated components remain visible for review.
    if complete and report["complete_costs"]:
        snapshot["total_recorded_cost"] = report["total_recorded_cost"]
        if approved and price_pence is not None:
            snapshot["estimated_contribution"] = report["estimated_contribution"]
            snapshot["margin_percent"] = report["margin_percent"]
    return snapshot
