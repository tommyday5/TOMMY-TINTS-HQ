"""Deterministic internal quote estimates; AI must never invent monetary inputs."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

RATES = {"retail": Decimal("85"), "trade": Decimal("70")}
def amount(value, field):
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(field + " must be numeric")
    if not n.is_finite() or n < 0:
        raise ValueError(field + " must be a finite non-negative number")
    return n
def calculate_quote(data, rates=None):
    """Pricing uses confirmed labour rates, supplied costs and explicit overhead.

    Not a binding customer quote: no business cost-library connection yet.
    """
    tier = data.get("tier", "retail")
    if tier not in RATES:
        raise ValueError("Unknown pricing tier")
    hours = amount(data.get("hours", 0), "hours")
    materials = amount(data.get("materials", 0), "materials")
    consumables = amount(data.get("consumables", 0), "consumables")
    overhead = amount(data.get("overhead", 0), "overhead")
    effective_rates = RATES if rates is None else {k: amount(rates[k], k+" rate") for k in RATES}
    labour = hours * effective_rates[tier]
    total = (labour + materials + consumables + overhead).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    result = {"tier": tier, "labour_rate": str(effective_rates[tier]), "labour_hours": str(hours),
        "labour_allowance": str(labour.quantize(Decimal(".01"))), "materials": str(materials),
        "consumables": str(consumables), "overhead": str(overhead),
        "cost_based_target": str(total), "currency": "GBP", "requires_owner_approval": True,
        "note": "Internal estimate only. Confirm scope, cost library and margin before quoting."}
    if data.get("quoted_price") is not None:
        quoted = amount(data["quoted_price"], "quoted_price")
        result["difference_to_target"] = str((quoted-total).quantize(Decimal(".01")))
    return result
