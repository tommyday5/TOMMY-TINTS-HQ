"""Offline, read-only reconciliation input contracts for future Monzo and DNA adapters.

This module ONLY validates already-mapped, synthetic/canonical provider rows.
It does not parse a real provider export, connect to a provider, store data,
categorise customer revenue, allocate a payment or verify a bank settlement.
No adapter should use the resulting preview as live financial evidence.
"""
from collections import defaultdict
from datetime import datetime, timezone
import re

_REF = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_MAX_ROWS = 5000
_PROCESSOR_KINDS = frozenset({
    "payment", "refund", "chargeback", "chargeback_reversal", "fee",
})


class ContractError(ValueError):
    """Provider mapping or integrity evidence is insufficient for ingestion."""


def _ref(value, field):
    if not isinstance(value, str) or not _REF.fullmatch(value):
        raise ContractError(f"Invalid {field}; exact stable provider reference required")
    return value


def _pence(value, field, *, signed=True):
    # Floats, decimals, numeric strings and bools can silently change money.
    if type(value) is not int or abs(value) > 10**12 or (not signed and value < 0):
        raise ContractError(f"Invalid {field}; signed integer minor units required")
    return value


def _timestamp(value):
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        raise ContractError("Invalid provider timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        raise ContractError("Timestamp must include an explicit timezone") from None


def _rows(rows):
    if not isinstance(rows, (tuple, list)) or len(rows) > _MAX_ROWS:
        raise ContractError("Import preview must be a bounded list of mapped rows")
    if any(not isinstance(row, dict) for row in rows):
        raise ContractError("Every mapped row must be a field dictionary")
    return rows


def _dedupe(items):
    seen = {}
    repeated = 0
    for item in items:
        key = item["evidence_id"]
        if key in seen:
            if seen[key] != item:
                raise ContractError("Conflicting rows share a provider event reference")
            repeated += 1
        else:
            seen[key] = item
    return tuple(sorted(seen.values(), key=lambda item: (item["occurred_at"], item["evidence_id"]))), repeated


def _gbp(row):
    if row.get("currency") != "GBP":
        raise ContractError("Only explicitly GBP-mapped evidence is supported")


def normalise_monzo_rows(rows, *, account_ref):
    """Preview already-mapped Monzo bank rows; does NOT read Monzo or a CSV.

    Required canonical keys: transaction_id, amount_pence, currency, booked_at.
    The caller must prove mappings from the actual export before production use.
    A provider category or description is intentionally NOT used to label revenue.
    """
    account = _ref(account_ref, "account reference")
    prepared = []
    for row in _rows(rows):
        _gbp(row)
        transaction = _ref(row.get("transaction_id"), "transaction ID")
        amount = _pence(row.get("amount_pence"), "bank amount")
        prepared.append({
            "evidence_id": f"monzo:{account}:{transaction}",
            "provider": "monzo",
            "account_ref": account,
            "transaction_id": transaction,
            "amount_pence": amount,
            "occurred_at": _timestamp(row.get("booked_at")),
            "currency": "GBP",
            "classification": None,
            "bank_receipt_verified": False,
            "job_payment_verified": False,
        })
    events, duplicates = _dedupe(prepared)
    return {
        "provider": "monzo", "events": events, "duplicate_rows": duplicates,
        "source_mapping_verified": False, "bank_balance_verified": False,
        "verified_trading_receipts_pence": None, "reconciled": False,
    }


def normalise_dna_rows(rows, *, merchant_ref):
    """Preview canonical DNA processor events; no actual DNA export format assumed.

    Required canonical keys: event_id, payment_id, kind, gross_pence,
    fee_pence, net_pence, currency, occurred_at. Fee is always non-negative;
    gross and net are signed. Events MUST NOT be summed again as bank receipts.
    Optional batch_id groups provider-side events, not bank settlements.
    """
    merchant = _ref(merchant_ref, "merchant reference")
    prepared = []
    for row in _rows(rows):
        _gbp(row)
        event = _ref(row.get("event_id"), "processor event ID")
        payment = _ref(row.get("payment_id"), "payment ID")
        kind = row.get("kind")
        if not isinstance(kind, str) or kind not in _PROCESSOR_KINDS:
            raise ContractError("Unrecognised processor event type")
        gross = _pence(row.get("gross_pence"), "gross")
        fee = _pence(row.get("fee_pence"), "fee", signed=False)
        net = _pence(row.get("net_pence"), "net")
        if gross - fee != net:
            raise ContractError("Processor net does not equal signed gross less fees")
        if (kind in ("payment", "chargeback_reversal") and gross <= 0
                or kind in ("refund", "chargeback") and gross >= 0
                or kind == "fee" and (gross != 0 or fee <= 0)):
            raise ContractError("Processor event sign is inconsistent with its kind")
        batch = row.get("batch_id")
        if batch is not None:
            batch = _ref(batch, "processor batch ID")
        prepared.append({
            "evidence_id": f"dna:{merchant}:{event}",
            "provider": "dna",
            "merchant_ref": merchant,
            "event_id": event,
            "payment_id": payment,
            "kind": kind,
            "gross_pence": gross,
            "fee_pence": fee,
            "net_pence": net,
            "batch_id": batch,
            "occurred_at": _timestamp(row.get("occurred_at")),
            "currency": "GBP",
            "bank_settlement_verified": False,
            "job_payment_verified": False,
        })
    events, duplicates = _dedupe(prepared)
    return {
        "provider": "dna", "events": events, "duplicate_rows": duplicates,
        "source_mapping_verified": False, "bank_settlement_verified": False,
        "verified_job_receipts_pence": None, "reconciled": False,
    }


def preview_bank_rollforward(bank_preview, *, opening_pence=None, closing_pence=None):
    """Arithmetic-only check. Matching totals do not authenticate bank balances."""
    if not isinstance(bank_preview, dict) or bank_preview.get("provider") != "monzo":
        raise ContractError("A canonical Monzo preview is required")
    events = bank_preview.get("events")
    if not isinstance(events, tuple) or any(
        not isinstance(item, dict) or item.get("provider") != "monzo"
        or type(item.get("amount_pence")) is not int for item in events
    ):
        raise ContractError("Invalid canonical bank events")
    net = sum(item["amount_pence"] for item in events)
    if opening_pence is None or closing_pence is None:
        return {
            "mapped_net_movement_pence": net, "expected_closing_pence": None,
            "difference_pence": None, "arithmetically_consistent": None,
            "bank_balance_verified": False, "reconciled": False,
        }
    opening = _pence(opening_pence, "opening balance")
    closing = _pence(closing_pence, "closing balance")
    expected = opening + net
    return {
        "mapped_net_movement_pence": net, "expected_closing_pence": expected,
        "difference_pence": closing - expected,
        "arithmetically_consistent": closing == expected,
        "bank_balance_verified": False, "reconciled": False,
    }


def preview_processor_batches(processor_preview):
    """Provider-side net grouping, NEVER evidence of a Monzo bank receipt."""
    if not isinstance(processor_preview, dict) or processor_preview.get("provider") != "dna":
        raise ContractError("A canonical DNA preview is required")
    batches = defaultdict(lambda: {"event_count": 0, "net_pence": 0})
    for event in processor_preview.get("events", ()):
        if not isinstance(event, dict) or event.get("provider") != "dna":
            raise ContractError("Invalid canonical processor events")
        if event.get("batch_id") is not None:
            batch_id = _ref(event["batch_id"], "processor batch ID")
            batches[batch_id]["event_count"] += 1
            batches[batch_id]["net_pence"] += _pence(event.get("net_pence"), "net")
    return {
        "batches": {key: batches[key] for key in sorted(batches)},
        "bank_settlement_verified": False, "job_receipts_verified": False,
        "reconciled": False,
    }
