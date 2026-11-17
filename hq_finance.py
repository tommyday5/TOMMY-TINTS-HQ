"""Explicit, audited read-only mappings. No financial values are stored here."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Labels deliberately describe the spreadsheet's actual semantics.
FIELDS = [
 ('bank','Reported bank balance','Cash Engine','E12','estimate'),
 ('target','Monthly revenue target','Settings','B6','target'),
 ('overheads','Monthly planning overheads','Settings','B7','estimate'),
 ('trading','Trading cash — monthly average','Cash Engine','B7','90-day run rate'),
 ('operating','Operating expenses — monthly average','Cash Engine','E7','90-day run rate'),
 ('variable','Stock / growth / capex — monthly average','Cash Engine','H7','90-day run rate'),
 ('debt','Debt commitment used — monthly','Cash Engine','E10','commitment'),
 ('drawings','Director outflow — monthly average','Cash Engine','B9','not profit'),
 ('funding','Director funding — monthly average','Cash Engine','H8','excluded from trading'),
 ('daily','Daily sales target','HQ Dashboard','A11','target'),
 ('gap','7-day survival cash gap','HQ Dashboard','F66','planning estimate'),
 ('daily_gap','Required collected cash / day — next 7 days','HQ Dashboard','I66','planning estimate'),
 ('receipts','Scheduled receipts — next 30 days','Cash Engine','U5','expected, not received'),
 ('commitments','Core commitments — next 30 days','Cash Engine','U6','planning estimate'),
 ('forecast','Forecast closing cash — 30 days','Cash Engine','U7','planning proxy, not bank balance'),
 ('capacity','Safe additional debt capacity — monthly','Cash Engine','E11','spreadsheet estimate'),
 ('deposits','Deposits to collect','HQ Dashboard','O59','unpaid, not received'),
]
RANGES = [f"'{sheet}'!{cell}" for _,_,sheet,cell,_ in FIELDS] + ["'Cash Engine'!B4", "'Cash Engine'!H4", "'Cash Engine'!H5", "'Settings'!B36"]

def finance_snapshot(payload, now=None):
    now = now or datetime.now(timezone.utc)
    sheets = {s['properties']['title']:s for s in payload.get('sheets',[])}
    def cell(sheet, address):
        import re
        m=re.fullmatch(r'([A-Z]+)([0-9]+)', address)
        col=0
        for c in m[1]: col=col*26+ord(c)-64
        row=int(m[2])-1; col-=1
        for block in sheets.get(sheet,{}).get('data',[]):
            ri=row-block.get('startRow',0); ci=col-block.get('startColumn',0)
            rows=block.get('rowData',[])
            if 0<=ri<len(rows):
                values=rows[ri].get('values',[])
                if 0<=ci<len(values): return values[ci]
        return {}
    metrics=[]
    for key,label,sheet,address,basis in FIELDS:
        c=cell(sheet,address); value=c.get('effectiveValue',{}).get('numberValue')
        try:
            amount=Decimal(str(value))
            if not amount.is_finite(): raise InvalidOperation
            pence=int((amount*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError, TypeError): pence=None
        metrics.append(dict(key=key,label=label,pence=pence,basis=basis,source=f"{sheet}!{address}",formula=c.get('userEnteredValue',{}).get('formulaValue'),status='available' if pence is not None else 'missing or formula error'))
    date_value=cell('Cash Engine','B4').get('effectiveValue',{}).get('numberValue')
    try:
        bank_date=(datetime(1899,12,30,tzinfo=timezone.utc)+timedelta(days=float(date_value))).date()
        age=(now.date()-bank_date).days
    except (TypeError,ValueError,OverflowError): bank_date=None; age=None
    threshold=cell('Settings','B36').get('effectiveValue',{}).get('numberValue')
    status=cell('Cash Engine','H4').get('formattedValue','Unknown')
    return dict(metrics=metrics,refreshed_at=now.isoformat(),bank_data_date=str(bank_date) if bank_date else None,stale=age is None or age<0 or threshold is None or age>threshold or status!='CURRENT',bank_status=status,review_count=cell('Cash Engine','H5').get('formattedValue','Unknown'),source='Tommy_Tints_HQ_formula_checked · Google Sheets (read-only)',bank_verified=False,gaps=['Revenue received this calendar month is not mapped; the monthly average must not be substituted.','Exact upcoming bill dates and outstanding customer invoice totals are not yet mapped.','No verified historical time series: trend chart unavailable.','Bank balance is a spreadsheet estimate. Forecast closing cash is not an exact available-after-bills balance.'])
