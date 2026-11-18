"""Fixture-only, explicit Monzo/DNA settlement amount comparison.

This is not a provider adapter, authorised bank read, settlement confirmation,
revenue classification or job allocation. Canonical source mapping and actual
bank ownership remain unverified. The output never includes identifiers or
monetary totals, and no data is written.
"""
from hq_reconciliation_contracts import (
    ContractError, _ref, normalise_dna_rows, normalise_monzo_rows,
    preview_processor_batches,
)

_MAX_LINKS = 500
_MAX_EVENTS = 5000


def _canonical_bank(preview):
    if (not isinstance(preview, dict) or preview.get("provider") != "monzo"
            or preview.get("source_mapping_verified") is not False):
        raise ContractError("An unverified canonical Monzo preview is required")
    events = preview.get("events")
    if not isinstance(events, tuple) or len(events) > _MAX_EVENTS:
        raise ContractError("Canonical bank evidence must be a bounded tuple")
    if not events:
        return {}
    try:
        scope = events[0]["account_ref"]
        canonical = normalise_monzo_rows([{
            "transaction_id": event["transaction_id"],
            "amount_pence": event["amount_pence"],
            "currency": event["currency"],
            "booked_at": event["occurred_at"],
        } for event in events], account_ref=scope)
    except (KeyError, TypeError, AttributeError, IndexError):
        raise ContractError("Malformed canonical bank evidence") from None
    if canonical["events"] != events:
        raise ContractError("Bank evidence was modified or mixed across accounts")
    return {event["transaction_id"]: event["amount_pence"] for event in events}


def _canonical_processor(preview):
    if (not isinstance(preview, dict) or preview.get("provider") != "dna"
            or preview.get("source_mapping_verified") is not False):
        raise ContractError("An unverified canonical DNA preview is required")
    events = preview.get("events")
    if not isinstance(events, tuple) or len(events) > _MAX_EVENTS:
        raise ContractError("Canonical processor evidence must be a bounded tuple")
    if not events:
        return {}, 0
    try:
        scope = events[0]["merchant_ref"]
        canonical = normalise_dna_rows([{
            "event_id": event["event_id"],
            "payment_id": event["payment_id"],
            "kind": event["kind"],
            "gross_pence": event["gross_pence"],
            "fee_pence": event["fee_pence"],
            "net_pence": event["net_pence"],
            "currency": event["currency"],
            "occurred_at": event["occurred_at"],
            "batch_id": event["batch_id"],
        } for event in events], merchant_ref=scope)
    except (KeyError, TypeError, AttributeError, IndexError):
        raise ContractError("Malformed canonical processor evidence") from None
    if canonical["events"] != events:
        raise ContractError("Processor evidence was modified or mixed across merchants")
    batches = preview_processor_batches(preview)["batches"]
    unbatched = sum(event["batch_id"] is None for event in events)
    return {key: batch["net_pence"] for key, batch in batches.items()}, unbatched


def preview_explicit_settlement_pairs(bank_preview, processor_preview, proposals):
    """Compare owner-proposed one-to-one payout links using exact signed pence.

    proposals: {DNA batch ID: Monzo transaction ID}. No guessing from amounts,
    dates, descriptions, names or order. One bank transaction cannot represent
    two batches here; splitting and aggregation require a separate reviewed
    allocation design. Even exact arithmetic is NOT a verified bank settlement.
    """
    if not isinstance(proposals, dict) or len(proposals) > _MAX_LINKS:
        raise ContractError("A bounded explicit batch-to-bank reference map is required")
    bank = _canonical_bank(bank_preview)
    batches, unbatched = _canonical_processor(processor_preview)
    used_bank = set()
    amount_equal = credit_equal = debit_equal = mismatch = 0
    for batch_id, transaction_id in proposals.items():
        _ref(batch_id, "processor batch ID")
        _ref(transaction_id, "bank transaction ID")
        if batch_id not in batches or transaction_id not in bank:
            raise ContractError("Proposed settlement references are outside the previews")
        if transaction_id in used_bank:
            raise ContractError("A bank transaction cannot be assigned twice")
        used_bank.add(transaction_id)
        processor_pence, bank_pence = batches[batch_id], bank[transaction_id]
        if processor_pence == 0 or bank_pence == 0:
            raise ContractError("Zero-net batches or zero bank movements cannot be paired")
        if processor_pence == bank_pence:
            amount_equal += 1
            if bank_pence > 0:
                credit_equal += 1
            else:
                debit_equal += 1
        else:
            mismatch += 1
    return {
        "bank_entries_in_preview": len(bank),
        "processor_batches_in_preview": len(batches),
        "unbatched_processor_events": unbatched,
        "proposed_one_to_one_links": len(proposals),
        "amount_equal_candidates": amount_equal,
        "credit_amount_equal_candidates": credit_equal,
        "debit_amount_equal_candidates": debit_equal,
        "amount_mismatches": mismatch,
        "unpaired_bank_entries": len(bank) - len(used_bank),
        "unpaired_processor_batches": len(batches) - len(proposals),
        "manual_proposals_only": True,
        "source_mapping_verified": False,
        "provider_authorised": False,
        "bank_receipt_verified": False,
        "bank_balance_verified": False,
        "bank_settlement_verified": False,
        "trading_revenue_verified": False,
        "job_payment_verified": False,
        "reconciled": False,
        "changes_saved": 0,
    }
