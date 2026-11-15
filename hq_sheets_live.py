"""Read-only Google Sheets connector using a dedicated service account.

Credentials are supplied via a Railway secret; never commit or expose them.
"""
import json
import os
from functools import lru_cache
import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from hq_pricing_adapter import settings_from_values, library_from_values, jobs_for_timer

SPREADSHEET_ID="1B0XULUDiMlVuW9u0kUOYem8WelBtOH-Xey_wWVYdb2M"
SCOPES=["https://www.googleapis.com/auth/spreadsheets.readonly"]
RANGES={"settings":"'Settings'!A4:C60","library":"'Cost Library'!A4:L500",
        "jobs":"'Jobs'!A4:AO600"}

def configured():
    return bool(os.environ.get("HQ_GOOGLE_SERVICE_ACCOUNT_JSON"))

@lru_cache(maxsize=1)
def _credentials():
    raw=os.environ.get("HQ_GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("Read-only Google Sheets service account not configured")
    info=json.loads(raw)
    if info.get("type")!="service_account" or not info.get("client_email"):
        raise ValueError("Invalid service account configuration")
    return service_account.Credentials.from_service_account_info(info,scopes=SCOPES)

def read_workbook():
    credentials=_credentials()
    credentials.refresh(Request())
    url="https://sheets.googleapis.com/v4/spreadsheets/"+SPREADSHEET_ID+"/values:batchGet"
    response=requests.get(url,params=[("ranges",r) for r in RANGES.values()],
                          headers={"Authorization":"Bearer "+credentials.token},timeout=15)
    response.raise_for_status()
    ranges=response.json().get("valueRanges",[])
    if len(ranges)!=3:
        raise ValueError("Missing Google Sheets ranges")
    settings=settings_from_values(ranges[0].get("values",[]))
    library=library_from_values(ranges[1].get("values",[]))
    jobs=jobs_for_timer(ranges[2].get("values",[]))
    return {"rates":{k:str(v) for k,v in settings.items()},
            "services":{k:{name:str(v) for name,v in item.items()} for k,item in library.items()},
            "jobs":jobs,"source":"Google Sheets (read-only)"}

def read_workbook_with_access_token(access_token):
    """Read HQ using a user-consented, short-lived OAuth access token."""
    if not access_token:
        raise ValueError("Missing authorised Google access token")
    url="https://sheets.googleapis.com/v4/spreadsheets/"+SPREADSHEET_ID+"/values:batchGet"
    response=requests.get(url,params=[("ranges",r) for r in RANGES.values()],
                          headers={"Authorization":"Bearer "+access_token},timeout=15)
    response.raise_for_status()
    ranges=response.json().get("valueRanges",[])
    if len(ranges)!=3:
        raise ValueError("Missing Google Sheets ranges")
    return {"rates":{k:str(v) for k,v in settings_from_values(ranges[0].get("values",[])).items()},
            "services":{k:{name:str(v) for name,v in item.items()} for k,item in library_from_values(ranges[1].get("values",[])).items()},
            "jobs":jobs_for_timer(ranges[2].get("values",[])),
            "source":"Google Sheets (user OAuth, read-only)"}
