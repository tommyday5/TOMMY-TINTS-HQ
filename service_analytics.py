"""Pure service-performance calculations. No banking credentials or customer data.

Revenue is recognised only for completed jobs, and cash received is reported
separately to avoid counting deposits or unpaid invoices as bank income.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

def money(value):
    try:
        amount = Decimal(str(value if value is not None else "0"))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid monetary amount")
    if not amount.is_finite():
        raise ValueError("Amount must be finite")
    return amount

def summarise_services(jobs):
    """Return sorted service metrics from canonical, deduplicated job records.

    Required: unique id, service, status, price, material_cost, labour_hours.
    Optional: other_direct_cost, cash_received. Values are GBP.
    A multi-service booking must be split into separate job/service lines upstream.
    """
    seen = set()
    groups = defaultdict(lambda: dict(booked=0, completed=0, revenue=Decimal(0),
        direct_cost=Decimal(0), cash_received=Decimal(0), hours=Decimal(0)))
    for job in jobs:
        job_id = str(job.get("id", "")).strip()
        service = str(job.get("service", "")).strip()
        status = str(job.get("status", "")).lower().strip()
        if not job_id or not service:
            raise ValueError("Every job needs an id and service")
        if job_id in seen:
            raise ValueError("Duplicate job id: " + job_id)
        seen.add(job_id)
        if status not in ("quoted", "booked", "in_progress", "completed", "cancelled"):
            raise ValueError("Unknown job status: " + status)
        price = money(job.get("price"))
        materials = money(job.get("material_cost"))
        other = money(job.get("other_direct_cost"))
        hours = money(job.get("labour_hours"))
        cash = money(job.get("cash_received"))
        if any(v < 0 for v in (price, materials, other, hours, cash)):
            raise ValueError("Negative values require explicit credit/refund records")
        row = groups[service]
        if status == "booked":
            row["booked"] += 1
        if status == "completed":
            row["completed"] += 1
            row["revenue"] += price
            row["direct_cost"] += materials + other
            row["hours"] += hours
        row["cash_received"] += cash
    result = []
    for service, row in groups.items():
        profit = row["revenue"] - row["direct_cost"]
        result.append({
            "service": service, "booked_jobs": row["booked"],
            "completed_jobs": row["completed"],
            "earned_revenue": str(row["revenue"].quantize(Decimal(".01"))),
            "direct_cost": str(row["direct_cost"].quantize(Decimal(".01"))),
            "gross_contribution": str(profit.quantize(Decimal(".01"))),
            "cash_received": str(row["cash_received"].quantize(Decimal(".01"))),
            "labour_hours": str(row["hours"]),
            "contribution_per_hour": str((profit / row["hours"]).quantize(Decimal(".01"))) if row["hours"] else None,
        })
    return sorted(result, key=lambda x: Decimal(x["gross_contribution"]), reverse=True)
