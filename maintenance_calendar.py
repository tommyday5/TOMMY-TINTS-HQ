"""Booking conflict checks. Pure validation; never creates or modifies events."""
from datetime import datetime, timedelta

def timestamp(value):
    if not isinstance(value,str):raise ValueError("Time must be ISO 8601")
    try:dt=datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError:raise ValueError("Invalid ISO 8601 time")
    if dt.tzinfo is None or dt.utcoffset() is None:raise ValueError("Timezone offset required")
    return dt

def check_booking_window(data):
    if not isinstance(data,dict):raise ValueError("JSON object required")
    start=timestamp(data.get("start"))
    end=timestamp(data.get("end"))
    if end<=start or end-start>timedelta(hours=12):raise ValueError("Booking must be 1 minute to 12 hours")
    events=data.get("events",[])
    if not isinstance(events,list) or len(events)>500:raise ValueError("Invalid events")
    conflicts=[]
    for i,event in enumerate(events):
        if not isinstance(event,dict):raise ValueError("Invalid calendar event")
        if event.get("cancelled") or event.get("status")=="cancelled":continue
        if event.get("transparency")=="transparent":continue
        es=timestamp(event.get("start"));ee=timestamp(event.get("end"))
        if ee<=es:raise ValueError("Invalid calendar event interval")
        if start<ee and es<end:
            conflicts.append({"index":i,"start":es.isoformat(),"end":ee.isoformat()})
    return {"available":not conflicts,"conflicts":conflicts,
        "warning":"Only checks supplied calendar events. Live Google Calendar and Square availability are NOT connected; absence of conflicts does not confirm bookability.",
        "booking_created":False}
