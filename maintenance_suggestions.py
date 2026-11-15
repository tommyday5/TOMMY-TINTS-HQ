"""Conservative maintenance booking suggestions using existing recorded dates and areas."""
from datetime import date, datetime, timedelta
from collections import defaultdict

def parse_due(raw):
    raw=str(raw or "").strip()
    if not raw:return None
    for fmt in ("%Y-%m-%d","%d/%m/%Y","%d-%m-%Y","%d %b %Y"):
        try:return datetime.strptime(raw,fmt).date()
        except ValueError:pass
    return None

def booking_suggestions(clients,visits,reference_date=None,window_days=7):
    today=reference_date or date.today()
    if not isinstance(today,date):raise ValueError("Invalid reference date")
    if not isinstance(clients,list) or not isinstance(visits,list):raise ValueError("Invalid maintenance data")
    if not isinstance(window_days,int) or window_days<0 or window_days>31:raise ValueError("Window must be 0–31 days")
    booked=set()
    for visit in visits:
        if not isinstance(visit,dict):continue
        status=str(visit.get("Visit Status","")).strip().lower()
        if status in {"cancelled","canceled","no show","no-show","void"}:continue
        day=parse_due(visit.get("Visit Date"))
        if day and status in {"booked","scheduled","confirmed","completed","in progress"}:
            ident=str(visit.get("Client ID","")).strip().lower()
            if ident:booked.add((ident,day))
    groups=defaultdict(list)
    for c in clients:
        if not isinstance(c,dict) or str(c.get("Status","")).lower().strip()!="active":continue
        ident=str(c.get("Client ID","")).strip()
        due=parse_due(c.get("Next Due"))
        if not ident or not due or due>today+timedelta(days=window_days):continue
        if (ident.lower(),due) in booked:continue
        area=str(c.get("Area / Route Cluster") or "").strip()
        if not area:area=str(c.get("Postcode") or "").strip().upper().split(" ")[0]
        if not area:continue
        groups[area].append({"id":ident,"customer":str(c.get("Customer") or ""),"postcode":str(c.get("Postcode") or ""),
            "due":due.isoformat(),"days_until_due":(due-today).days,
            "vehicle_slots":str(c.get("Cars / Vehicle Slots") or ""),
            "route_block":str(c.get("Route Block") or "")})
    result=[]
    for area,items in sorted(groups.items()):
        items.sort(key=lambda x:(x["due"],x["customer"]))
        if len(items)<2:continue
        result.append({"area":area,"customer_count":len(items),"customers":items,
            "earliest_due":items[0]["due"],"suggested_action":"Contact customers to confirm compatible dates before booking.",
            "verification":"Area grouping only; not confirmed nearby by driving time."})
    return {"reference_date":today.isoformat(),"window_days":window_days,"groups":result,
      "excluded":"Inactive customers, missing due dates, and matching existing scheduled/completed visits are excluded.",
      "notice":"Recommendations only; no customer contact or booking changes."}
