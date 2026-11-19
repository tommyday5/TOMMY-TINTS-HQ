"""Memory-only, owner-entered transaction classification proposals.

This module cannot verify a bank receipt, change stored classifications or
allocate money to a customer/job. A proposal is NOT financial evidence.
Only canonical, already-mapped Monzo preview rows may be passed here.
"""
from collections import Counter

from hq_reconciliation_contracts import ContractError, _validated_monzo_events


# Deliberately exclude a "verified revenue", "paid" or "settled" label.
CATEGORIES = (
    "customer_receipt_candidate",
    "transfer",
    "director_funding",
    "director_drawings",
    "loan",
    "refund",
    "processor_fee",
    "tax",
    "supplier_expense",
    "wages",
    "liability_payment",
    "other",
    "unresolved",
)


def preview_manual_classifications(bank_preview, proposals=None):
    """Validate explicit user choices and return counts ONLY, not source data.

    Never infer any class from merchant names, transaction amounts, bank
    descriptions, processor events, or spreadsheet categories.
    """
    if not isinstance(bank_preview, dict) or bank_preview.get("provider") != "monzo":
        raise ContractError("Classification review requires canonical Monzo preview")
    # A canonical preview is mutable after normalization. Check the original
    # checksum, source-evidence flags, exact amounts and every event again
    # before accepting labels: valid-looking changed credits are not evidence.
    # This is a local tamper guard, NOT independent proof from Monzo.
    events = _validated_monzo_events(bank_preview)
    if len(events) > 500:
        raise ContractError("Invalid or unbounded canonical bank preview")
    if proposals is None:
        proposals = {}
    if not isinstance(proposals, dict) or len(proposals) > 500:
        raise ContractError("Classifications must be a bounded transaction-ID map")
    ids = []
    amounts = {}
    scope = None
    for event in events:
        if (not isinstance(event, dict) or event.get("provider") != "monzo"
                or not isinstance(event.get("transaction_id"), str)
                or not event.get("transaction_id")):
            raise ContractError("Invalid canonical transaction reference")
        account = event.get("account_ref")
        transaction = event["transaction_id"]
        amount = event.get("amount_pence")
        # Defence in depth: a forged or mixed-account preview must not be
        # mistaken for the output of the single-account canonical adapter.
        if (not isinstance(account, str) or not account
                or (scope is not None and account != scope)
                or event.get("evidence_id") != f"monzo:{account}:{transaction}"
                or event.get("currency") != "GBP"
                or type(amount) is not int):
            raise ContractError("Invalid or mixed-account canonical bank evidence")
        scope = account
        ids.append(transaction)
        amounts[transaction] = amount
    if len(ids) != len(set(ids)):
        raise ContractError("Canonical bank preview contains repeated transaction IDs")
    if set(proposals) - set(ids):
        raise ContractError("Classification refers to a transaction outside this preview")
    if any(not isinstance(category, str) or category not in CATEGORIES
           for category in proposals.values()):
        raise ContractError("Unsupported transaction review category")
    # A debit/zero entry can never be proposed as a customer receipt.
    # Transfers, loans, fees and refunds can have either sign, so those
    # categories are intentionally NOT inferred or restricted here.
    for transaction, category in proposals.items():
        amount = amounts[transaction]
        if (category in ("customer_receipt_candidate", "director_funding")
                and amount <= 0):
            raise ContractError("Receipt/funding proposal requires a positive bank credit")
        if category == "director_drawings" and amount >= 0:
            raise ContractError("Director drawings proposal requires a bank debit")

    counts = Counter(proposals.values())
    pending = len(ids) - len(proposals) + counts["unresolved"]
    return {
        "proposal_counts": {category: counts[category] for category in CATEGORIES
                            if counts[category]},
        "distinct_transactions": len(ids),
        "reviewed_proposals": len(proposals) - counts["unresolved"],
        "pending_review": pending,
        "customer_receipt_candidates": counts["customer_receipt_candidate"],
        "manual_proposals_only": True,
        "source_mapping_verified": False,
        "bank_receipt_verified": False,
        "trading_revenue_verified": False,
        "job_payment_verified": False,
        "bank_balance_verified": False,
        "reconciled": False,
        "records_imported": 0,
        "classification_changes_saved": 0,
    }
