"""Optional Google Routes driving matrix; only verified full addresses and a server secret."""
import os, requests

def driving_matrix(addresses):
    if not isinstance(addresses,list) or not 2<=len(addresses)<=10:
        raise ValueError("Supply 2 to 10 full addresses")
    if any(not isinstance(x,str) or len(x.strip())<12 or len(x)>240 for x in addresses):
        raise ValueError("Each stop needs a verified full street address and postcode")
    key=os.environ.get("HQ_GOOGLE_MAPS_API_KEY")
    if not key:
        raise RuntimeError("Google Routes API key is not configured")
    # Google Routes computeRouteMatrix; limit 100 origin/destination elements.
    waypoints=[{"waypoint":{"address":x.strip()}} for x in addresses]
    body={"origins":waypoints,"destinations":waypoints,"travelMode":"DRIVE",
          "routingPreference":"TRAFFIC_UNAWARE"}
    response=requests.post("https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix",
        headers={"X-Goog-Api-Key":key,"X-Goog-FieldMask":"originIndex,destinationIndex,distanceMeters,duration,status,condition",
                 "Content-Type":"application/json"},
        json=body,timeout=20)
    response.raise_for_status()
    items=response.json()
    if not isinstance(items,list):raise RuntimeError("Invalid routing provider response")
    result=[]
    for x in items:
        if x.get("condition")!="ROUTE_EXISTS" or not x.get("distanceMeters") or not x.get("duration"):
            continue
        seconds=float(str(x["duration"]).rstrip("s"))
        result.append({"origin":x["originIndex"],"destination":x["destinationIndex"],
                       "miles":round(x["distanceMeters"]/1609.344,1),"minutes":round(seconds/60,1)})
    return {"addresses_count":len(addresses),"legs":result,
            "note":"Traffic-unaware estimated driving times, not guaranteed journey times. Do not auto-book."}
