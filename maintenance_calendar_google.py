"""Read-only Google Calendar busy events for a bounded appointment window."""
import requests
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from maintenance_calendar import timestamp,check_booking_window

def _event_time(value, calendar_timezone):
    if value.get("dateTime"):
        return timestamp(value["dateTime"]).isoformat()
    if value.get("date") and calendar_timezone:
        return datetime.combine(date.fromisoformat(value["date"]), time.min,
                                tzinfo=ZoneInfo(calendar_timezone)).isoformat()
    raise ValueError("Event timestamp is missing")


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
    items = payload.get("items")
    if not isinstance(items, list):
        raise RuntimeError("Calendar response did not contain an event list")
    events = []
    try:
        for event in items:
            if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
                continue
            events.append({"start": _event_time(event["start"], payload.get("timeZone")),
                           "end": _event_time(event["end"], payload.get("timeZone"))})
        result = check_booking_window({"start": start, "end": end, "events": events})
    except (AttributeError, KeyError, ValueError, TypeError, ZoneInfoNotFoundError):
        raise RuntimeError("Calendar returned an unsupported event interval") from None
    result["source"]="Google Calendar primary (live read-only)"
    result["warning"]="Checks Google Calendar primary only. Square bookings and other calendars are not checked; this does not guarantee bookability."
    return result
