"""Read-only maintenance planning from existing HQ Sheets. No route-time claims without maps."""
import requests
from collections import defaultdict
from hq_sheets_live import SPREADSHEET_ID

RANGES=["'Maintenance Clients'!A4:AN500","'Maintenance Visits'!A4:S500","'Route Rules'!A4:C100","'Settings'!A4:C25"]
def rows(values):
    if not values:return []
    headers=[str(x).strip() for x in values[0]]
    return [{h:(str(row[i]).strip() if i<len(row) and row[i] is not None else "") for i,h in enumerate(headers) if h} for row in values[1:] if any(str(x).strip() for x in row)]

def maintenance_data(token):
    if not token: raise ValueError("Google Sheets authorisation required")
    response=requests.get("https://sheets.googleapis.com/v4/spreadsheets/"+SPREADSHEET_ID+"/values:batchGet",
        params=[("ranges",r) for r in RANGES],headers={"Authorization":"Bearer "+token},timeout=20)
    response.raise_for_status()
    result=response.json().get("valueRanges",[])
    if len(result)!=len(RANGES):raise ValueError("Maintenance sheets not available")
    clients=rows(result[0].get("values",[]))
    visits=rows(result[1].get("values",[]))
    rules=rows(result[2].get("values",[]))
    settings=rows(result[3].get("values",[]))
    active=[c for c in clients if c.get("Status","").lower()=="active" and c.get("Client ID")]
    groups=defaultdict(list)
    for c in active:
        cluster=c.get("Area / Route Cluster") or c.get("Postcode","").split(" ")[0] or "Unknown area"
        groups[cluster].append({"id":c.get("Client ID"),"customer":c.get("Customer"),"postcode":c.get("Postcode"),
          "frequency":c.get("Frequency"),"next_due":c.get("Next Due"),"vehicle_slots":c.get("Cars / Vehicle Slots"),
          "price":c.get("Selected Visit £") or c.get("Normal Visit £"),"route_block":c.get("Route Block")})
    return {"active_clients":len(active),"clients":active,"visits":visits,
       "route_rules":rules,"settings":settings,
       "clusters":[{"name":k,"clients":v,"count":len(v),"suggestion":"Candidate area grouping only; verify full addresses, driving times and booking availability."} for k,v in sorted(groups.items())],
       "routing_status":"Postcode/area candidates only; no Google Maps travel-time integration or automatic booking.",
       "source":"Existing HQ Google Sheets, read-only"}
