"""Read-only mapping of the existing Tommy Tints HQ Jobs worksheet.

Accepts rows from Google Sheets values.get; performs no network requests and
never stores customer information. Headers are taken from row 4.
"""
from decimal import Decimal, InvalidOperation
from service_analytics import summarise_services

SHEET_TAB = "Jobs"
HEADER_ROW = 4
SERVICES = tuple("Service " + str(n) for n in range(1, 6))
COMPLETED_STATUSES = {"completed", "paid", "awaiting payment"}
BOOKED_STATUSES = {"accepted", "booked", "in progress"}
IGNORED_STATUSES = {"enquiry", "quoted", "declined"}

def _amount(value):
    if value is None or str(value).strip() == "":
        return Decimal("0")
    cleaned = str(value).strip().replace("£", "").replace(",", "")
    try:
        number = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Invalid numeric cell") from exc
    if not number.is_finite():
        raise ValueError("Nonfinite numeric cell")
    return number

def _field(record, name):
    return record.get(name, "")

def jobs_from_values(rows):
    """Return canonical job lines, preserving single-service attribution.

    Multi-service jobs are intentionally grouped as unallocated rather than
    assigning the whole price to each service or arbitrarily splitting it.
    Cash received is always zero here: Paid status is not a bank transaction.
    """
    if not rows:
        return []
    headers = [str(x).strip() for x in rows[0]]
    required = {"Job ID", "Status", "Quoted Price", "Total Hours",
                "Materials £", "Consumables £", "Actual Hours",
                "Actual Materials £", "Actual Consumables £",
                "Actual Extra Costs £", *SERVICES}
    missing = required.difference(headers)
    if missing:
        raise ValueError("Missing Jobs columns: " + ", ".join(sorted(missing)))
    output = []
    for row in rows[1:]:
        record = dict(zip(headers, row))
        job_id = str(_field(record, "Job ID")).strip().upper()
        if not job_id:
            continue
        raw_status = str(_field(record, "Status")).strip().lower()
        if raw_status in COMPLETED_STATUSES:
            status = "completed"
        elif raw_status in BOOKED_STATUSES:
            status = "booked"
        elif raw_status in IGNORED_STATUSES:
            status = "quoted" if raw_status != "declined" else "cancelled"
        else:
            raise ValueError("Unknown HQ job status for " + job_id + ": " + raw_status)
        services = [str(_field(record, k)).strip() for k in SERVICES if str(_field(record, k)).strip()]
        if not services:
            continue
        service = services[0] if len(services) == 1 else "Multi-service (unallocated)"
        def actual_or_estimate(actual, estimate):
            value = _field(record, actual)
            return _amount(value if str(value).strip() else _field(record, estimate))
        materials = actual_or_estimate("Actual Materials £", "Materials £")
        consumables = actual_or_estimate("Actual Consumables £", "Consumables £")
        extra = _amount(_field(record, "Actual Extra Costs £"))
        hours = actual_or_estimate("Actual Hours", "Total Hours")
        output.append(dict(id=job_id, service=service, status=status,
            price=str(_amount(_field(record, "Quoted Price"))),
            material_cost=str(materials),
            other_direct_cost=str(consumables + extra),
            labour_hours=str(hours), cash_received="0"))
    return output

def summarise_sheet_values(rows):
    return summarise_services(jobs_from_values(rows))
