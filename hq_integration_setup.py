"""Local configuration inventory: presence is never provider verification."""
import os

CATALOGUE = (
    ('sheets','Google Sheets jobs/pricing',('GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET'),'/auth/sheets/connect','Use existing Google owner consent and grant the actual workbook access.'),
    ('maintenance_sheets','Maintenance workbook',('GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET'),'/hq/maintenance/routes','Use existing Sheets consent; verify customer addresses, visit IDs and package balances.'),
    ('finance','Spreadsheet finance',('GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET'),'/hq/finance','Use existing Sheets consent; verify actual finance workbook mappings. Spreadsheet balance is not live banking.'),
    ('gmail','Gmail enquiries',('GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET'),'/hq/gmail-enquiries','Existing Gmail read-only consent; label genuine Inbox enquiries HQ-Enquiries and explicitly import. No sending permission needed.'),
    ('calendar','Calendar availability',('GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET'),'/auth/calendar/connect','Existing Calendar read-only consent; explicitly check availability. External booking writes remain separate development and approval work.'),
    ('square','Square bookings',('HQ_SQUARE_READ_TOKEN','HQ_SQUARE_LOCATION_ID'),'/hq/square-bookings','Production Square token with APPOINTMENTS_READ / APPOINTMENTS_ALL_READ as applicable and the actual location ID. Adapter reads existing bookings only; outbound synchronisation is not implemented.'),
    ('square_payments','Square payment evidence',('HQ_SQUARE_READ_TOKEN','HQ_SQUARE_LOCATION_ID'),'/hq/square-payment-evidence','The same production token also needs PAYMENTS_READ. Read-only snapshots and exact-reference comparisons do not allocate payments or prove bank settlement.'),
    ('maps','Maps and Routes',('HQ_GOOGLE_MAPS_BROWSER_KEY','HQ_GOOGLE_MAPS_API_KEY','HQ_ROUTE_BASE_ADDRESS'),'/hq/maintenance/routes','Use tommy-tints-ltd only. Browser key: production referrer, Maps JavaScript + Geocoding. Server key: Routes only, supported outbound restrictions. Verify APIs/billing/quotas; budgets require approval.'),
    ('monzo','Monzo ingestion',(),'/hq/transaction-import-preview','Implementation required: verify actual export/Sheets schema and provenance, then implement persistent idempotent ingestion. Offline fixtures are not a live bank connector; no key alone activates this.'),
    ('dna','DNA Payments ingestion',(),'/hq/transaction-import-preview','Implementation required: verify authorised DNA report/API access and actual event/settlement mappings, then implement authenticated ingestion. No key alone activates this.'),
)


def setup_inventory(readiness):
    evidence = {row['key']: row for row in readiness}
    rows=[]
    for key,name,fields,link,action in CATALOGUE:
        required=[{'name':field,'present':bool(os.environ.get(field))} for field in fields]
        if key in ('sheets','maintenance_sheets','finance') and os.environ.get('HQ_GOOGLE_SERVICE_ACCOUNT_JSON'):
            required=[{'name':'HQ_GOOGLE_SERVICE_ACCOUNT_JSON','present':True}]
        rows.append({'key':key,'name':name,'requirements':required,'link':link,'action':action,
                     'configuration_complete':bool(required) and all(item['present'] for item in required),
                     'implementation_required':not fields,'evidence':evidence.get(key,{})})
    return rows
