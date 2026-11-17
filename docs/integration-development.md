# Tommy Tints HQ integration development

Build each provider adapter and its fixture-based tests in separate modules. Add routes and schema changes in small PRs so shared Flask and SQLite changes can be reviewed in order. Keep existing customer records and use synthetic storage in CI.

| Workstream | Implementation | Acceptance tests |
|---|---|---|
| Square and Google Calendar | Read both availability sources first. Add a canonical HQ appointment linked to a job, then persist provider IDs and versions. Require explicit approval for outbound changes. | Reject conflicts. Retry an ambiguous timeout using the same idempotency key. Replayed updates and cancellations converge without duplicate appointments. |
| Gmail enquiries | Add consent for the business inbox and a read-only adapter, then use provider message IDs for intake. Stage customer drafts for review. | Duplicate messages create one draft. Legal and debt mail stays out of the enquiry queue. Missing vehicle and contact fields remain visible. |
| Monzo ingestion | Map the existing export using stable transaction IDs and signed amounts in integer pence. Record source and import run. | Importing the same export twice changes no totals. Transfers and loans never enter trading revenue. Replaying revised classifications preserves source records. |
| Payment reconciliation | Store verified Square and DNA events with unique provider event IDs and match to jobs by payment reference. | Duplicates, partial payments, refunds, fees and batched settlements reconcile without double-counting bank receipts. Unmatched payments remain reviewable. |
| Maintenance | Build on existing client and visit records. Record completion with an idempotent visit ID, then calculate next due dates and prepaid balances. | Repeated completion consumes one visit. Customer-specific frequency, renewals and missed visits remain correct. |
| Job costing and PDFs | Attach time and material costs to the approved work record. Generate customer documents from reviewed fields. | Approved prices remain fixed. Calculations match fixture costs. Customer PDFs omit internal margin and notes. |
| Record-backed assistant | Expose owner-only read tools with dates, sources and missing-data markers. | Synthetic records yield reproducible answers. Missing data stays unknown. Mail and job text cannot authorise actions. |
| Mobile interface | Improve navigation and common job actions with existing branding. | Cover job progress, quote approval and integration status at narrow widths, with keyboard and touch input. |

Use an isolated restore for backup drills. Validate additive schema changes against both new and populated SQLite files. Store reviewable outcomes and timestamps in integration health. Provider connection, authorisation and successful synchronisation are distinct observations.
