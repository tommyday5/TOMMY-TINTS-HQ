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

### Revision and retry integrity guard (20 November 2026)

The local-only Sheets outbox now rejects invalid, boolean, negative, overflow and **future** work revisions rather than advertising a revision which has not committed to HQ. A delayed call for an older saved revision is ignored; identical revision replays do not reset a failed item's retry history. When an actual newer HQ revision commits, its queue entry coalesces and its retry attempts/error state are reset for the new snapshot. These checks run inside the same local SQLite transaction as the approved internal HQ state change. Synthetic tests cover each case and no external Sheets call or writer is introduced. **Pending** does not mean authorised, linked, dispatched, read-back verified or reconciled.


### Formula-safe, read-only Jobs link preflight (20 November 2026)

A new pure helper, hq_sheet_link_preflight.preflight_job_link, accepts only a
complete owner-obtained Google Sheets GridData snapshot of the verified Jobs tab
(row 4 through the reported last row, A:AJ). It requires spreadsheet/tab IDs,
checks the known job/service/pricing/status/cost heading subset, verifies global
Job ID uniqueness and the selected row's exact ID or blank mapped input cells,
and rejects target input formulas, ambiguous raw cell evidence and incomplete
snapshots. Calculated columns outside the mapped inputs are never targeted.
The module never calls Google, stores customer content, modifies HQ records or
creates links; tests use synthetic GridData only.

This is an engineering preflight, NOT a live owner UI or proof of current Sheets
access. It does not prove the B:E headings, an approved HQ-to-J record mapping,
a trusted baseline, a concurrency-safe edit, customer-specific pricing, consent,
write permissions, a persisted link, a successful sync or reconciliation.
No background worker or write endpoint is enabled. Before any activation,
obtain genuine row evidence via an owner-triggered read, verify all remaining
headings and existing J links, establish edit-conflict policy and owner-specific
write approval, then perform an explicitly approved targeted write/read-back.


### Owner-triggered read-only row inspection (20 November 2026)

The HQ owner can open `/hq/sheets/job-link-preflight`, supply one manually reviewed J reference and row, and explicitly inspect the existing Jobs worksheet. The new owner/CSRF-protected POST `/api/hq/sheets/job-link-preflight` reads the exact existing workbook with the **already consented Sheets read-only OAuth grant** and passes the server-only raw GridData to `preflight_job_link`. It returns only fixed pass/block metadata, never workbook values, customer names, credentials or source references. Malformed/partial snapshots and row/ID/header conflicts fail closed. The result is an ephemeral proposal; the selected row is not persisted, linked, booked, allocated or written. The standalone read outcome is recorded under `sheets_job_preflight`, never as a finance/pricing read, provider sync or reconciliation. A missing health observation prevents a claimed successful result.

**Important limitation:** Google's GridData may omit trailing blank row data. The conservative complete-row scan currently rejects any snapshot without the documented full row coverage. In that situation it reports a blocked inspection rather than treating omitted rows as proof of uniqueness; an explicit range-coverage contract must be verified against a genuine owner read before relaxing this guard. This feature does not verify unreviewed B:E headings, Service Cost Library selections, existing HQ-to-J identity, a trusted baseline, concurrent-edit safety, owner write authorisation, any bank receipt or full end-to-end job sync. No external Sheet changes occur.
