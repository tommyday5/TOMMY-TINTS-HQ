# Business Health, Live Bank Feed and Service Performance

Status: SPECIFICATION. No live banking, Square, DNA Payments or business-health data feed is active in the new app.

## Core data model
- Booking (provider, provider ID, status, schedule, customer, vehicle) -> one or more Job records.
- Job -> multiple ServiceLine records with canonical service codes (tint, paint/bodywork, wrap/dechrome, wheels, detailing, leather, PDR, other).
- Each ServiceLine has revenue allocation, estimated and actual labour, material/consumable costs, subcontractor costs, and discounts.
- Invoice/PaymentAllocation maps a single receipt to job(s), with deposits and refunds represented explicitly.
- BankTransaction has immutable provider transaction ID, posted/authorized dates, source, amount, currency, counterparty, category, reconciliation status and bank account.
- Expenses may be job-specific or overhead; no double counting across card settlements, transfers, refunds or payout batches.
- Maintain event/audit history and idempotent import keys for all providers.

## Bank feeds
- Prefer authorised UK Open Banking integration or supported Monzo feed/API where permitted; user must connect/consent explicitly.
- Initial fallback: existing Monzo Google Sheets export or CSV, with freshness indicator and incremental reconciliation.
- DNA Payments settlements and Square deposits should reconcile to underlying customer transactions, not be counted twice as both sale and bank receipt.
- Show bank balance as of provider timestamp, pending transactions, expected deposits, liabilities, and cash runway; never label delayed imports 'live'.
- Bank access read-only initially. No payments or transfers initiated without explicit authorisation.

## Booking attribution
- Square/HQ/AI public quote bookings map through a stable service taxonomy, not free-text descriptions.
- Handle multi-service jobs with service-specific prices and actual costs; preserve original booking ID and prevent duplicate records.
- Track booked -> attended -> completed -> invoiced -> paid, with cancellations/no-shows/refunds.
- Existing Google Calendar may contain scheduling events, not reliable priced bookings; require mapping/review before financial attribution.

## Business Health KPIs
- Bank balance, net cashflow, operating revenue, gross profit, contribution margin, estimated operating profit, overhead coverage, receivables, payables, 13-week cashflow forecast, runway and utilisation.
- Revenue recognised from completed jobs/invoices under selected accounting policy; booked pipeline separately. No VAT assumed while not VAT-registered; configurable.
- Separate owner withdrawals, wages, loans, transfers, asset purchases, finance repayments and taxes from service operating profit.
- Service leaderboard by revenue, gross profit, contribution per labour hour, net contribution after allocated overhead, quote conversion, average ticket, repeat bookings and utilisation.
- Ranking must show sample sizes and confidence; exclude unmatched bank transactions from job-level profitability until reconciled.
- Compare week/month/quarter with prior period; use alerts for low margin, falling conversion, unpaid invoices, high overheads and deteriorating runway.

## Operational sequence
1. Secure authentication, PostgreSQL and mobile navigation.
2. Import existing HQ Google Sheets in read-only mode, validate against source.
3. Establish service taxonomy and map current bookings/Jobs to service lines.
4. Connect booking providers with idempotent syncing and human review for unmatched entries.
5. Connect read-only bank feed or Monzo export, DNA payouts and reconciliation.
6. Deliver dashboards and service rankings with data freshness and coverage indicators.
7. Train time and profitability forecasts only on verified actuals.

## Security
Owner-only bank and creditor views, least-privilege access, encrypted secrets, audit logs, secure backups, GDPR minimisation and deletion controls. No customer or bank data in GitHub or public endpoints.
