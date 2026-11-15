"""Negotiation guardrails: distinguish sustainable contribution from short-term cash."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

def n(v,name):
    try: d=Decimal(str(v))
    except (ValueError,TypeError,InvalidOperation): raise ValueError(name+" must be numeric")
    if not d.is_finite() or d<0 or d>Decimal("10000000"): raise ValueError(name+" must be nonnegative and finite")
    return d

def money(v): return str(v.quantize(Decimal(".01"),rounding=ROUND_HALF_UP))

def evaluate(data):
    if not isinstance(data,dict): raise ValueError("Invalid input")
    required=("proposed_price","materials","labour_cost","overhead","other_costs","minimum_margin_percent")
    if any(str(data.get(k,"")).strip()=="" for k in required):
        raise ValueError("Enter all costs and a minimum sustainable margin before negotiation")
    p,m,l,o,x,t=(n(data[k],k) for k in required)
    if t>=100: raise ValueError("Minimum margin must be below 100%")
    if p==0: margin=None
    else: margin=(p-m-l-o-x)/p*100
    full=m+l+o+x
    floor=full/(Decimal(1)-t/100)
    cash_floor=m+l+x
    sustainable=p>=floor
    cash_positive=p>=cash_floor
    return {"proposed_price":money(p),"full_cost":money(full),"sustainable_floor":money(floor),
            "incremental_cash_floor":money(cash_floor),"estimated_contribution":money(p-full),
            "margin_percent":money(margin) if margin is not None else None,
            "sustainable":sustainable,"cash_positive":cash_positive,
            "status":"SUSTAINABLE" if sustainable else ("CASH_ONLY_NOT_SUSTAINABLE" if cash_positive else "BELOW_INCREMENTAL_COST"),
            "note":"Cash-only pricing does not cover allocated overhead and must not be treated as a sustainable minimum. All figures are estimates."}
