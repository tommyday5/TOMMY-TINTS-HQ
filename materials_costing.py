"""Material consumption costing, independent of customer selling prices."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

PENNY=Decimal("0.01")
def dec(value, label):
    try: number=Decimal(str(value))
    except (ValueError, InvalidOperation, TypeError): raise ValueError(label+" must be numeric")
    if not number.is_finite() or number<0: raise ValueError(label+" must be non-negative")
    return number

def cost_line(item):
    """Purchase price and package size define unit cost; consumption defines job cost.

    For two-part clearcoat enter clear and hardener separately, each with its
    own purchase price, package volume and amount used. For mixed basecoat
    supplied by the mixing system use method=direct.
    """
    method=item.get("method","quantity")
    name=str(item.get("name","Material")).strip()[:100]
    if method=="direct":
        raw=dec(item.get("cost",0),"direct cost")
        detail="Mixing system / manually entered actual cost"
    elif method=="quantity":
        purchase=dec(item.get("purchase_price"),"purchase price")
        package=dec(item.get("package_quantity"),"package quantity")
        used=dec(item.get("used_quantity"),"used quantity")
        if package==0: raise ValueError("Package quantity must be greater than zero")
        waste=dec(item.get("waste_percent",0),"waste percentage")
        if waste>100: raise ValueError("Waste percentage cannot exceed 100")
        raw=purchase/package*used*(Decimal(1)+waste/100)
        detail="Purchase cost / package quantity × usage, plus waste"
    elif method=="filler_preset":
        purchase=dec(item.get("purchase_price"),"purchase price")
        package=dec(item.get("package_quantity"),"package quantity")
        if package==0: raise ValueError("Package quantity must be greater than zero")
        preset=str(item.get("preset","")).lower()
        grams={"small":Decimal("30"),"medium":Decimal("90"),"heavy":Decimal("200")}
        if preset not in grams: raise ValueError("Choose small, medium or heavy filler")
        raw=purchase/package*grams[preset]
        detail="Provisional "+preset+" filler allowance ("+str(grams[preset])+" g)"
    else: raise ValueError("Unknown material costing method")
    return {"name":name,"method":method,"cost":str(raw.quantize(PENNY,rounding=ROUND_HALF_UP)),
            "detail":detail}

def calculate_materials(lines):
    if not isinstance(lines,list) or len(lines)>100: raise ValueError("Provide up to 100 material lines")
    results=[cost_line(line) for line in lines]
    total=sum((Decimal(line["cost"]) for line in results),Decimal(0))
    return {"lines":results,"total_cost":str(total.quantize(PENNY)),
            "currency":"GBP","note":"Internal actual-cost estimate, not a selling price"}
