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
from hq_sheet_link_preflight import FIRST_HEADER_ROW, MAX_ROWS, complete_trailing_blank_rows

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


def read_finance(access_token=None):
    """Read effective formula results and formulas together; never write cells."""
    from hq_finance import RANGES, finance_snapshot
    if not access_token:
        credentials = _credentials()
        if not credentials.valid:
            credentials.refresh(Request())
        access_token = credentials.token
    response = requests.get(
        'https://sheets.googleapis.com/v4/spreadsheets/' + SPREADSHEET_ID,
        params=[('ranges', r) for r in RANGES] + [('includeGridData','true'),
            ('fields','sheets(properties(title),data(startRow,startColumn,rowData(values(effectiveValue,formattedValue,userEnteredValue))))')],
        headers={'Authorization':'Bearer '+access_token}, timeout=15)
    response.raise_for_status()
    return finance_snapshot(response.json())


def read_job_link_snapshot(access_token, *, selected_row):
    """Owner-initiated read of the existing Jobs tab's raw A:AJ GridData.

    This must be called only after a verified owner POST and read-only consent.
    The unredacted Google response stays server-side; callers must return
    only hq_sheet_link_preflight's fixed, aggregate result and no row values.
    No spreadsheet writes, local storage or provider status changes occur here.
    """
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("Missing authorised read-only Google token")
    url = "https://sheets.googleapis.com/v4/spreadsheets/" + SPREADSHEET_ID
    response = requests.get(
        url,
        params=[
            ("ranges", "'Jobs'!A4:AJ"),
            ("includeGridData", "true"),
            ("fields", "spreadsheetId,sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),data(startRow,startColumn,rowData(values(userEnteredValue,formattedValue,effectiveValue))))"),
        ],
        headers={"Authorization": "Bearer " + access_token},
        timeout=(3, 15),
    )
    response.raise_for_status()
    snapshot = response.json()
    if not isinstance(snapshot, dict):
        raise ValueError("Malformed Jobs GridData evidence")
    # Sheets GridData can omit trailing blank RowData entries. Only when the
    # *chosen* candidate already has complete raw cell evidence can a second,
    # explicit FORMULA-rendered values read prove the missing A:AJ tail has no
    # data. Never infer blank candidate input cells from trimmed values.
    sheets = snapshot.get("sheets")
    if not isinstance(sheets, list) or len(sheets) != 1:
        return snapshot
    sheet = sheets[0]
    if not isinstance(sheet, dict):
        return snapshot
    properties = sheet.get("properties")
    blocks = sheet.get("data")
    if not isinstance(properties, dict) or not isinstance(blocks, list) or len(blocks) != 1:
        return snapshot
    grid = properties.get("gridProperties")
    if not isinstance(grid, dict) or not isinstance(blocks[0], dict):
        return snapshot
    count = grid.get("rowCount")
    rows = blocks[0].get("rowData")
    if (type(count) is not int or not FIRST_HEADER_ROW < count <= MAX_ROWS
            or not isinstance(rows, list) or len(rows) >= count - FIRST_HEADER_ROW + 1):
        return snapshot
    if (type(selected_row) is not int or
            selected_row > FIRST_HEADER_ROW + len(rows) - 1):
        # Fail closed in the existing preflight without a second provider call.
        return snapshot
    tail_response = requests.get(
        url + "/values/'Jobs'!A4:AJ" + str(count),
        params=[("majorDimension", "ROWS"), ("valueRenderOption", "FORMULA")],
        headers={"Authorization": "Bearer " + access_token},
        timeout=(3, 15),
    )
    tail_response.raise_for_status()
    return complete_trailing_blank_rows(
        snapshot, tail_response.json(), candidate_row=selected_row)
