"""Fixture-only Square payout arithmetic; NEVER a live settlement or job receipt.

This module requires deliberately mapped canonical rows. It does not assume a
Square export/API schema, call Square/Monzo, store financial data, allocate jobs,
authenticate bank statements or advance integration readiness.
"""
from collections import defaultdict
from datetime import datetime

from hq_reconciliation_contracts import (
    ContractError, _dedupe, _event_digest, _pence, _ref, _timestamp,
    _validated_monzo_events,
)

_KINDS = frozenset({
    "payment", "refund", "chargeback", "chargeback_reversal",
    "fee", "adjustment",
})
_MAX_EVENTS = 1000
_MAX_PAYOUTS = 200
_MAX_PROPOSALS = 200


def normalise_square_payout_rows(rows, *, location_ref):
    """Validate explicitly mapped, GBP, signed-pence payout components.

    Required fields: event_id, payout_id, kind, gross_pence, fee_pence,
    net_pence, currency and occurred_at. payment_id is required for payment,
    refund and chargeback-related kinds; optional for fee/adjustment.
    No payout status, bank account or bank posting is inferred from this data.
    """
    location = _ref(location_ref, "Square location reference")
    if not isinstance(rows, (list, tuple)) or len(rows) > _MAX_EVENTS:
        raise ContractError("Square preview requires a bounded row list")
    prepared = []
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("Square preview rows must be dictionaries")
        if row.get("currency") != "GBP":
            raise ContractError("Square preview supports explicitly GBP-mapped rows only")
        event = _ref(row.get("event_id"), "Square payout event ID")
        payout = _ref(row.get("payout_id"), "Square payout ID")
        kind = row.get("kind")
        if type(kind) is not str or kind not in _KINDS:
            raise ContractError("Unknown canonical Square payout event kind")
        gross = _pence(row.get("gross_pence"), "Square gross")
        fee = _pence(row.get("fee_pence"), "Square fee", signed=False)
        net = _pence(row.get("net_pence"), "Square net")
        if gross - fee != net:
            raise ContractError("Square event net must equal gross less fee")
        if ((kind in ("payment", "chargeback_reversal") and gross <= 0)
                or (kind in ("refund", "chargeback") and gross >= 0)
                or (kind == "fee" and (gross != 0 or fee <= 0))
                or (kind == "adjustment" and (gross == 0 or fee != 0))):
            raise ContractError("Square event amount sign conflicts with kind")
        payment = row.get("payment_id")
        if payment is not None:
            payment = _ref(payment, "Square payment ID")
        if kind in ("payment", "refund", "chargeback", "chargeback_reversal") and payment is None:
            raise ContractError("Square payment-related event requires an exact payment ID")
        prepared.append({
            "evidence_id": "square:" + location + ":" + event,
            "provider": "square", "location_ref": location,
            "event_id": event, "payout_id": payout, "payment_id": payment,
            "kind": kind, "gross_pence": gross, "fee_pence": fee,
            "net_pence": net, "occurred_at": _timestamp(row.get("occurred_at")),
            "currency": "GBP", "bank_settlement_verified": False,
            "job_payment_verified": False,
        })
    events, repeats = _dedupe(prepared)
    if len({event["payout_id"] for event in events}) > _MAX_PAYOUTS:
        raise ContractError("Square preview exceeds the payout count limit")
    return {
        "provider": "square", "events": events, "duplicate_rows": repeats,
        "canonical_event_digest": _event_digest(events),
        "source_mapping_verified": False, "provider_authorised": False,
        "bank_settlement_verified": False, "job_payment_verified": False,
        "reconciled": False,
    }


def _validated_square(preview):
    """Re-validate mutable canonical evidence before any arithmetic."""
    if (not isinstance(preview, dict) or preview.get("provider") != "square"
            or preview.get("source_mapping_verified") is not False
            or preview.get("provider_authorised") is not False
            or preview.get("bank_settlement_verified") is not False
            or preview.get("job_payment_verified") is not False
            or preview.get("reconciled") is not False):
        raise ContractError("Unverified canonical Square preview is required")
    events = preview.get("events")
    if not isinstance(events, tuple) or len(events) > _MAX_EVENTS:
        raise ContractError("Canonical Square events must be a bounded tuple")
    if preview.get("canonical_event_digest") != _event_digest(events):
        raise ContractError("Square preview changed after normalisation")
    if not events:
        return events
    try:
        rebuilt = normalise_square_payout_rows([{
            "event_id": e["event_id"], "payout_id": e["payout_id"],
            "payment_id": e["payment_id"], "kind": e["kind"],
            "gross_pence": e["gross_pence"], "fee_pence": e["fee_pence"],
            "net_pence": e["net_pence"], "occurred_at": e["occurred_at"],
            "currency": e["currency"],
        } for e in events], location_ref=events[0]["location_ref"])
    except (KeyError, TypeError, AttributeError, IndexError):
        raise ContractError("Malformed canonical Square payout events") from None
    if rebuilt["events"] != events:
        raise ContractError("Square events changed or mixed across locations")
    return events


def preview_explicit_square_payout_pairs(bank_preview, square_preview, proposals):
    """Compare exact owner-proposed payout→bank IDs without inferring transfers.

    Only one-to-one matches are supported: aggregation, split payouts,
    reversals and incomplete provider coverage need a later reviewed design.
    Returns COUNTS only; equal sums are not proof of bank settlement.
    """
    if type(proposals) is not dict or len(proposals) > _MAX_PROPOSALS:
        raise ContractError("Bounded exact payout-to-bank proposals required")
    bank = _validated_monzo_events(bank_preview)
    events = _validated_square(square_preview)
    bank_index = {e["transaction_id"]: e for e in bank}
    grouped = defaultdict(lambda: {"net": 0, "last_event": None})
    for event in events:
        payout = grouped[event["payout_id"]]
        payout["net"] += event["net_pence"]
        at = datetime.fromisoformat(event["occurred_at"])
        if payout["last_event"] is None or at > payout["last_event"]:
            payout["last_event"] = at
    used_bank = set()
    equal = unequal = chronology_conflicts = negative_equal = 0
    for payout_id, transaction_id in proposals.items():
        _ref(payout_id, "Square payout ID")
        _ref(transaction_id, "Monzo transaction ID")
        if payout_id not in grouped or transaction_id not in bank_index:
            raise ContractError("Payout proposal references absent source rows")
        if transaction_id in used_bank:
            raise ContractError("Cannot propose one bank entry for multiple payouts")
        used_bank.add(transaction_id)
        bank_row = bank_index[transaction_id]
        payout_net = grouped[payout_id]["net"]
        bank_amount = bank_row["amount_pence"]
        if payout_net == 0 or bank_amount == 0:
            raise ContractError("Zero payout or bank movement cannot be paired")
        if datetime.fromisoformat(bank_row["occurred_at"]) < grouped[payout_id]["last_event"]:
            chronology_conflicts += 1
        elif payout_net == bank_amount:
            equal += 1
            if payout_net < 0:
                negative_equal += 1
        else:
            unequal += 1
    return {
        "square_payouts_in_preview": len(grouped),
        "bank_entries_in_preview": len(bank_index),
        "proposed_one_to_one_links": len(proposals),
        "amount_equal_candidates": equal,
        "negative_amount_equal_candidates": negative_equal,
        "amount_mismatches": unequal,
        "chronology_conflicts": chronology_conflicts,
        "unpaired_payouts": len(grouped) - len(proposals),
        "unpaired_bank_entries": len(bank_index) - len(used_bank),
        "unbatched_payout_events": 0,
        "manual_proposals_only": True,
        "source_mapping_verified": False,
        "provider_authorised": False,
        "bank_receipt_verified": False,
        "bank_settlement_verified": False,
        "job_payment_verified": False,
        "reconciled": False,
        "changes_saved": 0,
    }
