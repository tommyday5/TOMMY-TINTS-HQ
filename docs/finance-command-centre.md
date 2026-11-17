# Private financial command centre

Read-only source: existing Tommy_Tints_HQ_formula_checked workbook; ID is reused from hq_sheets_live.py. Inspected effective values and original formulas on 17 November 2026. No live financial values are committed.

Mappings in hq_finance.FIELDS are explicit single cells in Cash Engine, Settings and HQ Dashboard. Google Sheets remains the calculation authority. Decimal conversion rounds effective numeric results to pennies; blank/error/text results remain unavailable. The bank estimate is Cash Engine!E12 (ultimately B2, an anchored transaction roll-forward), not cash after commitments. Cash Engine!B7 is a rolling monthly average; it is not calendar-month revenue. Loans/director funding and director outflow remain separate. U5/U6/U7 represent scheduled receipts, planning commitments and forecast closing cash.

Bank age is displayed through B4; freshness uses the workbook H4 status, preserving its configured threshold. Refresh time is retrieval time, not bank verification. H5 exposes unresolved classification count. There is no stored finance snapshot, localStorage, public HTML data, or analytics. On errors old cards are hidden. Owner-session decorator protects both page and API; global HQ response policy sets no-store. OAuth uses existing persistent connection refresh; service-account read-only fallback supported.

Manual refresh, foreground refresh and five-minute visible-page refresh. Charts compare verified mapped planning fields; no invented historical series. Page hides cards in background.

Remaining: inspect and verify calendar-month received-revenue calculation, invoice-level outstanding payments, exact bill dates and a historical time series before connecting those fields. The current balance is explicitly unverified banking data. Existing spreadsheet labels sometimes call it available cash; the app avoids that ambiguity. No spreadsheet writes or new ledger.
