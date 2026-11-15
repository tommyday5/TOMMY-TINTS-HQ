"""Job-level contribution calculations, separate from charge-out rates."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

def dec(value, name):
    try:
        d=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError):
        raise ValueError(name+" must be a valid number")
    if not d.is_finite() or d<0 or d>Decimal("10000000"):
        raise ValueError(name+" must be nonnegative and finite")
    return d

def cash(value):
    return str(value.quantize(Decimal("0.01"),rounding=ROUND_HALF_UP))

def profitability(data):
    if not isinstance(data,dict): raise ValueError("Job data required")
    lines=data.get("lines",[])
    if not isinstance(lines,list) or len(lines)>100: raise ValueError("Invalid materials")
    materials=Decimal("0")
    for line in lines:
        if not isinstance(line,dict): raise ValueError("Invalid material line")
        materials+=dec(line.get("cost"),"material cost")
    sale=data.get("sale")
    if sale is None or str(sale).strip()=="": raise ValueError("Selling price required")
    revenue=dec(sale,"selling price")
    hours=dec(data.get("labour",0) or 0,"labour hours")
    labour_rate=dec(data.get("actual_labour_rate",0) or 0,"actual labour cost rate")
    overhead=dec(data.get("overhead",0) or 0,"allocated overhead")
    extras=dec(data.get("other_costs",0) or 0,"other costs")
    labour_cost=hours*labour_rate
    total=materials+labour_cost+overhead+extras
    contribution=revenue-total
    margin=contribution/revenue*100 if revenue else None
    return {"revenue":cash(revenue),"materials":cash(materials),"labour_cost":cash(labour_cost),
            "overhead":cash(overhead),"other_costs":cash(extras),"total_recorded_cost":cash(total),
            "estimated_contribution":cash(contribution),"margin_percent":cash(margin) if margin is not None else None,
            "complete_costs":all(str(data.get(k,"")).strip()!="" for k in ("actual_labour_rate","overhead","other_costs")),
            "note":"Estimated contribution before tax; not accounting net profit. Missing costs, owner pay, depreciation and tax may change results."}
