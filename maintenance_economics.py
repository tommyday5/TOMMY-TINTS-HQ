"""Maintenance route economics. No postcode-based distance guesses."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

def decimal(value,name):
    try: x=Decimal(str(value))
    except (TypeError,ValueError,InvalidOperation): raise ValueError(name+" must be a number")
    if not x.is_finite() or x<0 or x>Decimal("1000000"):raise ValueError(name+" must be finite and nonnegative")
    return x
def pounds(x):return str(x.quantize(Decimal("0.01"),rounding=ROUND_HALF_UP))

def travel_charge(one_way_miles,free_one_way_miles="10",rate_per_mile="0.56"):
    miles=decimal(one_way_miles,"One-way miles")
    free=decimal(free_one_way_miles,"Free one-way miles")
    rate=decimal(rate_per_mile,"Mileage rate")
    excess=max(Decimal(0),miles-free)
    return {"one_way_miles":str(miles),"free_one_way_miles":str(free),
            "chargeable_round_trip_miles":str(excess*2),
            "travel_surcharge":pounds(excess*2*rate)}

def route_economics(data):
    if not isinstance(data,dict):raise ValueError("Route data must be an object")
    stops=data.get("stops")
    if not isinstance(stops,list) or not 1<=len(stops)<=20:raise ValueError("Enter 1 to 20 stops")
    # Actual whole-route miles include base to first, inter-stop journeys and last to base.
    miles=decimal(data.get("total_route_miles"),"Total route miles")
    drive=decimal(data.get("total_drive_minutes"),"Total driving minutes")
    rate=decimal(data.get("mileage_rate","0.56"),"Mileage rate")
    labour=decimal(data.get("actual_labour_cost_per_hour"),"Actual labour cost per hour")
    overhead=decimal(data.get("overhead_per_hour"),"Overhead per hour")
    total_price=Decimal(0);clean_minutes=Decimal(0);materials=Decimal(0)
    for stop in stops:
        if not isinstance(stop,dict):raise ValueError("Invalid stop")
        total_price+=decimal(stop.get("price"),"Stop price")
        clean_minutes+=decimal(stop.get("clean_minutes"),"Cleaning minutes")
        materials+=decimal(stop.get("materials_cost"),"Materials cost")
    time=(clean_minutes+drive)/Decimal(60)
    labour_cost=time*labour
    overhead_cost=time*overhead
    mileage_cost=miles*rate
    contribution=total_price-materials-labour_cost-overhead_cost-mileage_cost
    return {"stops":len(stops),"revenue":pounds(total_price),"cleaning_hours":pounds(clean_minutes/60),
      "driving_hours":pounds(drive/60),"total_hours":pounds(time),"materials":pounds(materials),
      "mileage_cost":pounds(mileage_cost),"labour_cost":pounds(labour_cost),"allocated_overhead":pounds(overhead_cost),
      "estimated_contribution":pounds(contribution),"contribution_per_hour":pounds(contribution/time) if time else None,
      "margin_percent":pounds(contribution/total_price*100) if total_price else None,
      "warning":"This is an estimate using supplied verified route miles/minutes; no driving distance has been inferred from postcodes. Confirm owner labour and overhead allocations."}
