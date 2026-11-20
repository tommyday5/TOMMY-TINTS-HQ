# Job / Sheets synchronisation stage 1 — 20 November 2026

## Verified architecture

Existing workbook: 1B0XULUDiMlVuW9u0kUOYem8WelBtOH-Xey_wWVYdb2M, title Tommy_Tints_HQ_formula_checked. Jobs sheet ID 330634230, 1000 rows / 94 columns. Row 4 contains headings; data begins row 5. Header/formula/validation inspection was read-only. No production job or sheet cell was written.

Existing HQ work IDs are UUID hex references, not historical J references. hq_work_records, hq_customers, hq_vehicles and hq_work_audit live in customer_requests.sqlite3 on /data. Work records have optimistic revisions and approved prices; planned deposits are not payment evidence. Existing quote conversion preserves the internal work ID. Existing costing and printable customer breakdown remain separate, and no financial values are changed by this stage.

## Implemented

An additive hq_job_sync_outbox table receives new internal records and changed price/status revisions inside the same SQLite transaction. The latest revision coalesces per work ID. Stale/idempotent repeats do not regress the queue; TEST sources never queue. Only internal IDs, revisions, state, attempt count, fixed error slot and timestamps are retained, not duplicate customer payloads. Outbox storage failure rolls back the HQ update instead of losing a required queue event. Google unavailability cannot affect these local saves: no Google call runs in them.

The owner-only no-store /api/hq/work-records/<id>/sheet-sync endpoint and job-card Check Sheets sync button expose pending/not-linked status and the activation blocker. No successful external sync is claimed. Existing untouched historical HQ records remain not linked; no automatic backfill or job renumbering occurs. hq_job_sheet_links reserves unique workbook/job reference mappings but no historical links are created yet.

## Actual schema constraints

Inputs include A Job ID, B Date, C Customer, D Customer Type, E Vehicle/Reg, F:J services, Q Quoted Price, R Status and AG:AJ optional actual hours/materials/consumables/extra costs. D5 validates Retail/Trader/Insurance, while formulas also contain Maintenance logic: validate target-row constraints before maintenance writes rather than silently bypassing dropdowns. Services validate against Cost Library names; HQ's broad service categories cannot simply be copied into those cells.

O is an Apps Script driving-miles formula. S:AF and many later columns contain pricing calculations. BB Deposit Received is NOT HQ deposit_pence (a planned deposit). BC/BD/BF and later payment/approval/agreement fields are derived or controlled separately. Do not write full rows or treat Completed as Paid. Missing actual costs must stay unknown, even where historical spreadsheet formulas fall back to estimates.

## Remaining implementation / activation (not keys-only)

1. Verified unique mapping of existing J references to reviewed HQ records and baseline snapshots; no fuzzy automatic merge.
2. Owner job creation UI with correct customer tier and validated Cost Library selections; unique new reference allocation must account for historical references.
3. Dedicated explicit Sheets write consent using existing OAuth client (current connector remains read-only), or an authorised restricted editor service account. No OAuth scope has been broadened by this release.
4. Formula-safe targeted cell writer, write read-back, per-job worker leases, backoff/recovery and durable attempt/audit records. No queue worker or retry execution is connected yet.
5. Conflict handling against baseline and remote changes. Sheets values APIs lack a general atomic compare-and-swap cell write; reading before update alone is not a guarantee against simultaneous owner sheet edits. Resolve the write-coordination policy before activating updates.
6. Controlled historical import/reconciliation and two-way conflict review; never activate blind bidirectional writes.
7. Link costing, customer PDFs and finance by verified stable mapping; preserve agreed prices and unknown receipt amounts.
8. Owner approval of a specific genuine record and destination before the first live write and end-to-end demonstration. No fake production test jobs.

This release is a transactional foundation, not completion of enter-once job synchronisation. There are no external write credentials, live writes, historical links, automatic retries or two-way sync claims.
