"""Fixture-only, explicit Monzo/DNA settlement amount comparison.

This is not a provider adapter, authorised bank read, settlement confirmation,
revenue classification or job allocation. Canonical source mapping and actual
bank ownership remain unverified. The output never includes identifiers or
monetary totals, and no data is written.
"""
from hq_reconciliation_contracts import (
    ContractError, _ref, _validated_dna_events, _validated_monzo_events,
    preview_processor_batches,
)

_MAX_LINKS = 500


def _canonical_bank(preview):
    events = _validated_monzo_events(preview)
    return {event["transaction_id"]: (event["amount_pence"], event["occurred_at"])
            for event in events}


def _canonical_processor(preview):
    events = _validated_dna_events(preview)
    batches = preview_processor_batches(preview)["batches"]
    unbatched = sum(event["batch_id"] is None for event in events)
    # A processor batch cannot already have reached a bank before events
    # included in that batch occurred. This is a conservative chronology
    # screen, not a payout-date inference or provider settlement proof.
    last_event = {}
    for event in events:
        batch_id = event["batch_id"]
        if batch_id is not None:
            last_event[batch_id] = max(
                last_event.get(batch_id, event["occurred_at"]), event["occurred_at"])
    return {key: (batch["net_pence"], last_event[key])
            for key, batch in batches.items()}, unbatched


def preview_explicit_settlement_pairs(bank_preview, processor_preview, proposals):
    """Compare owner-proposed one-to-one payout links using exact signed pence.

    proposals: {DNA batch ID: Monzo transaction ID}. No guessing from amounts,
    dates, descriptions, names or order. Posting before the latest event in a
    proposed batch blocks an amount-equal candidate even if pence match. One
    bank transaction cannot represent
    two batches here; splitting and aggregation require a separate reviewed
    allocation design. Even exact arithmetic is NOT a verified bank settlement.
    """
    if not isinstance(proposals, dict) or len(proposals) > _MAX_LINKS:
        raise ContractError("A bounded explicit batch-to-bank reference map is required")
    bank = _canonical_bank(bank_preview)
    batches, unbatched = _canonical_processor(processor_preview)
    used_bank = set()
    amount_equal = credit_equal = debit_equal = mismatch = 0
    chronology_conflicts = equal_with_chronology_conflict = 0
    for batch_id, transaction_id in proposals.items():
        _ref(batch_id, "processor batch ID")
        _ref(transaction_id, "bank transaction ID")
        if batch_id not in batches or transaction_id not in bank:
            raise ContractError("Proposed settlement references are outside the previews")
        if transaction_id in used_bank:
            raise ContractError("A bank transaction cannot be assigned twice")
        used_bank.add(transaction_id)
        processor_pence, latest_event_at = batches[batch_id]
        bank_pence, bank_posted_at = bank[transaction_id]
        if processor_pence == 0 or bank_pence == 0:
            raise ContractError("Zero-net batches or zero bank movements cannot be paired")
        # Every timestamp was normalised to UTC by the canonical contracts.
        # Equal amounts with a bank posting preceding any included event
        # require investigation, not an 'amount-equal candidate' decision.
        chronology_conflict = bank_posted_at < latest_event_at
        if chronology_conflict:
            chronology_conflicts += 1
        if processor_pence == bank_pence:
            if chronology_conflict:
                equal_with_chronology_conflict += 1
            else:
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
        "chronology_conflicts": chronology_conflicts,
        "amount_equal_but_chronology_conflicts": equal_with_chronology_conflict,
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
