"""Owner-only, read-only HQ-versus-historical-J *candidate* comparison.

A sheet reference and matching-looking values are NOT proof of the same job.
B:E workbook semantics have not been owner-verified, and no link, baseline,
approval, reconciliation or provider write is created by this helper.
Never return customer names, registration numbers, price amounts or row values.
"""
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from hq_sheet_link_preflight import FIRST_HEADER_ROW, _entered, _text


def _normalise(value):
    if not isinstance(value, str):
        return None
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _candidate_text(sheet_value, hq_value):
    if not sheet_value or not hq_value:
        return "unavailable"
    return ("candidate_agreement" if _normalise(sheet_value) == _normalise(hq_value)
            else "requires_manual_review")


def _candidate_price(sheet_value, hq_pence):
    if sheet_value is None or hq_pence is None:
        return "unavailable"
    if (type(hq_pence) is not int or hq_pence < 0 or
            type(sheet_value) not in (int, float)):
        return "requires_manual_review"
    try:
        pence = Decimal(str(sheet_value)) * 100
    except (InvalidOperation, ValueError, OverflowError):
        return "requires_manual_review"
    if not pence.is_finite() or pence != pence.to_integral_value():
        return "requires_manual_review"
    return ("candidate_agreement" if pence == hq_pence
            else "requires_manual_review")


def review_historical_work(snapshot, *, work, expected_revision, target_row):
    """Compare an explicitly chosen HQ work revision with a preflighted J row.

    Caller MUST first run preflight_job_link(..., mode='existing') on the same
    complete raw snapshot and recheck HQ revision after its provider read.
    No PII, bank/receipt/settlement data or proposed link is returned.
    """
    if (not isinstance(work, dict) or type(work.get("id")) is not str or
            not re.fullmatch(r"[0-9a-f]{32}", work["id"])):
        raise ValueError("Invalid reviewed HQ work record")
    if (type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1 or
            type(work.get("revision")) is not int or
            work["revision"] != expected_revision):
        raise ValueError("HQ work revision changed; refresh the record and retry")
    if type(target_row) is not int or target_row < 5:
        raise ValueError("Invalid reviewed Jobs row")
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("sheets"), list) or
            len(snapshot["sheets"]) != 1):
        raise ValueError("Missing inspected Jobs source evidence")
    try:
        rows = snapshot["sheets"][0]["data"][0]["rowData"]
        target = rows[target_row - FIRST_HEADER_ROW]
        # Candidate checks are advisory: C and Q have not been verified as
        # complete cross-system identity fields. Exact agreement is not a link.
        customer = _text(_entered(target, 2), "Customer")
        price_entry = _entered(target, 16)
    except (KeyError, IndexError, TypeError):
        raise ValueError("Missing inspected candidate source evidence") from None

    if price_entry is not None and "numberValue" not in price_entry:
        price_result = "requires_manual_review"
    else:
        price_result = _candidate_price(
            price_entry["numberValue"] if price_entry else None,
            work.get("price_pence"))

    customer_result = _candidate_text(customer, work.get("name"))
    is_disagreement = "requires_manual_review" in (customer_result, price_result)
    return {
        "hq_revision_checked": True,
        "hq_candidate_review": "conflict_or_difference" if is_disagreement
            else "manual_identity_review_required",
        "customer_candidate_comparison": customer_result,
        "quoted_price_candidate_comparison": price_result,
        "mapping_semantics_verified": False,
        "historical_mapping_verified": False,
        "baseline_verified": False,
        "write_authorised": False,
        "live_sync": False,
        "reconciled": False,
        "next_safe_action": (
            "Compare the original HQ job card and Jobs row yourself; verify "
            "the historical identity, B:E column meanings and a current write "
            "baseline before separately approving a link or any Sheets edit."
        ),
    }
