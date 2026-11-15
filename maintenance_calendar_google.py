"""Read-only Google Calendar busy events for a bounded appointment window."""
import requests
from datetime import timedelta
from maintenance_calendar import timestamp,check_booking_window

def live_conflicts(token,start,end,calendar_id="primary"):
    if not token:raise ValueError("Calendar authorisation required")
    if calendar_id!="primary":raise ValueError("Only primary calendar supported")
    begin=timestamp(start);finish=timestamp(end)
    if finish<=begin or finish-begin>timedelta(hours=12):raise ValueError("Booking window must be 1 minute to 12 hours")
    response=requests.get("https://www.googleapis.com/calendar/v3/calendars/primary/events",
        headers={"Authorization":"Bearer "+token},
        params={"timeMin":begin.isoformat(),"timeMax":finish.isoformat(),
                "singleEvents":"true","maxResults":250,"showDeleted":"false"},
        timeout=20)
    response.raise_for_status()
    payload=response.json()
    if payload.get("nextPageToken"):
        raise RuntimeError("Too many events to verify availability; narrow the time window")
    events=[]
    for event in payload.get("items",[]):
        if event.get("status")=="cancelled" or event.get("transparency")=="transparent":continue
        start_obj=event.get("start") or {};end_obj=event.get("end") or {}
        # All-day events have date only. They are treated as blocking that entire date.
        es=start_obj.get("dateTime") or (start_obj.get("date")+"T00:00:00+00:00" if start_obj.get("date") else None)
        ee=end_obj.get("dateTime") or (end_obj.get("date")+"T00:00:00+00:00" if end_obj.get("date") else None)
        if not es or not ee:raise RuntimeError("Calendar event has an unsupported time format")
        events.append({"start":es,"end":ee})
    result=check_booking_window({"start":start,"end":end,"events":events})
    result["source"]="Google Calendar primary (live read-only)"
    result["warning"]="Checks Google Calendar primary only. Square bookings and other calendars are not checked; this does not guarantee bookability."
    return result
