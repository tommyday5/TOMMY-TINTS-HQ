"""Read-only parsing of Tommy Tints HQ Settings, Cost Library and Jobs.

This module does not connect to Google Sheets. It accepts values fetched by an
authorised service account; the workbook remains the source of truth.
"""
from decimal import Decimal, InvalidOperation
from quote_engine import amount

def money_cell(value):
    if value is None or str(value).strip()=="":
        return Decimal("0")
    return amount(str(value).replace("£","").replace(",","").strip(), "sheet amount")

def settings_from_values(rows):
    values={}
    for row in rows[1:]:
        if len(row)>=2 and str(row[0]).strip():
            values[str(row[0]).strip()]=row[1]
    required=("Retail Labour Rate","Trader Labour Rate")
    if any(k not in values for k in required):
        raise ValueError("Required labour rates missing from Settings")
    return {"retail":money_cell(values["Retail Labour Rate"]),
            "trade":money_cell(values["Trader Labour Rate"])}

def library_from_values(rows):
    headers=None
    result={}
    for row in rows:
        if row and str(row[0]).strip()=="Service":
            headers=[str(c).strip() for c in row]
            continue
        if headers is None or not row or not str(row[0]).strip():
            continue
        rec=dict(zip(headers,row))
        service=str(rec.get("Service","")).strip()
        if not service:
            continue
        if service in result:
            raise ValueError("Duplicate service in Cost Library: "+service)
        result[service]={
            "hours":money_cell(rec.get("Default Hours")),
            "materials":money_cell(rec.get("Material £")),
            "consumables":money_cell(rec.get("Consumables £")),
            "retail_multiplier":money_cell(rec.get("Retail Mult.")),
            "trade_multiplier":money_cell(rec.get("Trader Mult.")),
            "pricing_method":str(rec.get("Pricing Method","")).strip(),
            "approval_rule":str(rec.get("Customer Approval Rule","")).strip(),
        }
    if not result:
        raise ValueError("No Cost Library services found")
    return result

def jobs_for_timer(rows):
    headers=None
    output={}
    for sheet_row, row in enumerate(rows, start=4):
        if row and str(row[0]).strip()=="Job ID":
            headers=[str(c).strip() for c in row]
            continue
        if headers is None or not row:
            continue
        rec=dict(zip(headers,row))
        job_id=str(rec.get("Job ID","")).strip().upper()
        if not job_id:
            continue
        key = job_id
        if key in output:
            key = f"{job_id} (sheet row {sheet_row})"
            while key in output:
                key += " duplicate"
        raw=rec.get("Quoted Hours") or rec.get("Total Hours") or ""
        hours=money_cell(raw) if str(raw).strip() else Decimal("0")
        output[key]={"job_id":job_id,"sheet_row":sheet_row,
            "duplicate_reference":key != job_id,"estimated_hours":str(hours),
            "status":str(rec.get("Status","")).strip(),
            "services":[str(rec.get("Service "+str(i),"")).strip() for i in range(1,6) if str(rec.get("Service "+str(i),"")).strip()]}
    return output
