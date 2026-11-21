# Native HQ operating records — migration register (21 November 2026)

**Priority:** Native, editable HQ records rather than further read-only connection adapters. Historic workbook 1B0XULUDiMlVuW9u0kUOYem8WelBtOH-Xey_wWVYdb2M remains read-only and intact. No bulk import, J-reference allocation, backfill, Sheets write consent, or bidirectional sync has been activated.

## New native owner workflow

- Owner-only /hq/work/new: create a new Retail/Trade/Maintenance customer, or select an existing customer; add or reuse a vehicle; create a quote or job draft with service, scope notes and requested (not confirmed) date/time.
- Owner-only /hq/work/<id>/edit: update draft service, tier, scope and requested date/time with optimistic revision checking. Approved records lock their work scope. Existing job cards handle pricing/approval and the existing customer editor handles contact/address edits.
- Owner-only /hq/work/<id>/progress: record/review durable internal milestones on approved/in-progress canonical jobs, including owner-reviewed public-enquiry conversions; work revision and audit are updated, with no booking or payment side effect.
- Owner-only /hq/vehicles/<id>/edit: edit a linked canonical HQ vehicle (make/model/year/registration) with SQLite persistence, no-cache form, CSRF, owner authentication, input validation, duplicate-registration review, stale-revision conflicts and PII-minimised field-name audit. Existing linked draft job cards reflect saved vehicle details; vehicles referenced by approved/active/closed work are locked until a separate evidence-preserving correction workflow is approved. Cross-customer reassignment is not available.
- All writes are atomic in existing customer_requests.sqlite3 under HQ_DATA_DIR. Operation IDs are replay-safe; fingerprint mismatch is a conflict. PII-minimised audit stores creation and changed-field metadata.
- Canonical hq_customers, hq_vehicles and hq_work_records IDs are reused across public request conversions and direct owner creation. New native IDs are HQ-UUID source references, **never invented J-series references**.
- Customer tiers are classification only; prices require owner review. Planned deposits are not received deposits; approved or completed work is not proof of invoicing or payment.
- Unlinked native records skip the Google Sheets outbox. Provider adapters, read-only Sheets, public requests and /book remain unchanged.

## Field-level authority / migration coverage

| Domain | Local native state | Authority and migration status | Remaining evidence |
|---|---|---|---|
| New native customers and vehicles | Direct creation, existing contact editor, versioned vehicle editor | **HQ authoritative for newly created local records only**; legacy not migrated | Owner verifies duplicates, historical locked-vehicle corrections and tier permissions |
| New native jobs and quotes | Create/edit drafts, pricing/status, work stages, actual time/material ledgers | **HQ authoritative for new UUID records and new native entries only**; no J link | Costing parity, customer PDF, historical migration and archive |
| Public requests and /book | Durable request + owner draft conversion | Local incoming requests; **not confirmed booking/payment** | Customer login and approved status tracking |
| Historical Sheets Jobs | Read-only inspection and link preflight | **Sheets historical source**; not migrated | Exact J-to-HQ identity, conflicts, source approval and rollback |
| Historical Customers / Cost Library | Read-only/partial adapters | **Sheets historical source** | Header, duplicate and tier mapping |
| Maintenance clients and visits | Read-only Sheets and local overrides/packages | **Not reconciled/migrated** | Visit ledger, packages, routes, owner acceptance |
| Materials and shop | Existing job costing and commerce modules | **No historical parity proven** | Inventory, purchasing and stock reconciliation |
| Transactions, finance and creditor cases | Existing offline schema/audit rehearsal and reports | **Not migrated/reconciled** | Bank statements, liability evidence, classification parity |
| Gmail, Calendar, Square, Monzo, DNA, Maps | Safe adapters and previews | **Not proved live or reconciled by this change** | Owner consent, provider reads, independently confirmed matching |
| Production backups | Mounted Railway volume; offline SQLite drills | **Independent production restoration unverified** | Encrypted off-site backup and isolated restore |

## Next safe development slices

1. Verify native creation, save, reload and quote-to-job promotion on synthetic records; do not change genuine live business records for verification.
2. Expand native customer lookup and versioned per-field source provenance before historical importing. Vehicle edit is implemented; an approved-job correction workflow remains outstanding.
3. Stage dry-run Sheets Jobs and Customers mappings, preserving genuine J references with row-level conflict detection, rollback and explicit owner approval.
4. Advance public /book request to HQ review to approved status, without treating a requested time as available or booking anyone.
5. Verify on-volume persistence and independently restored backups. A mounted volume and offline tests are not production restore proof.

No live record, sheet cell, payment, booking or message is changed automatically by deployment. The owner deliberately submits new native records through the form.

## Vehicle edit acceptance boundary (21 November 2026)

The new vehicle screen is linked from native work drafts and operates on canonical `hq_vehicles` rows used by HQ and reviewed public-enquiry conversions. Synthetic tests cover save/reload, replay idempotency, stale submissions, validation, duplicate registration review, owner-only access, approved-job lock and public enquiry isolation. No routine customer-account login or booking confirmation is introduced. A customer-created request remains unconfirmed and its original submitted evidence remains untouched. These are tests on temporary SQLite databases, **not** proof of production restore, a live owner browser session or a completed Sheets migration.

### Canonical HQ work progress ledger (21 November 2026)

- Owner-only `/hq/work/<id>/progress` is linked from all HQ job cards, including requests converted from public `/book`, and from native work editors. Approved or in-progress **jobs** can record predefined actual workshop milestones (check-in, preparation, repair, primer, paint, installation, finishing, quality check, ready for collection). The view displays persisted history; no customer-visible status endpoint or notification is created.
- Each action appends a row to `hq_work_stage_events` on existing `HQ_DATA_DIR` SQLite storage, increments the *same canonical work revision*, and writes a PII-minimised work audit event. Owner identity is HMAC-fingerprinted, rather than copied in plaintext. A stale competing save fails; repeating the current stage does not produce duplicate events. Only deliberate owner submissions save stages. This does not update the job's lifecycle status, approved price, cash ledger, calendar, Square booking, Google Sheet or outbound messages.
- Native creation/vehicle editing and existing `/book` intake are preserved. This progress ledger is **HQ-authoritative only for new milestones entered in HQ**, not a migration of historical sheet stage entries. Original J-series values and all genuine financial/legal evidence remain untouched. Owner authentication, CSRF, input limits, status gate, no-store responses and synthetic persistence/concurrency/public-conversion tests protect the workflow.
- Pending: independent production backup restoration; exact historical Sheets mapping and owner approval; record/customer access controls for authenticated customer-facing status; field-level work-stage reconciliation with any historical evidence; actual live owner-browser verification.


### Canonical HQ actual workshop time (21 November 2026)

- Owner-only \`/hq/work/<id>/time\` now links from existing native job editors and work-progress pages, including jobs converted from public enquiries. Authenticated owner can CREATE a time entry on an approved/in-progress job (activity, whole minutes, optional actual internal hourly cost and short internal note), inspect the persisted ledger, and **reverse mistaken entries**. Reversals are retained, never deleted; closed/completed work is locked.
- Canonical \`hq_work_records.revision\` advances atomically with every new entry or reversal. Repeated identical operation identifiers return the same entry; conflicting replays and stale editors fail closed. All changes are owner-authenticated, CSRF-checked, versioned and audit-stamped, with actor HMAC fingerprints and PII-minimised work audit metadata.
- Labour minutes are factual owner-entered time, not billable or invoiced hours. Hourly actual internal cost is **optional**; missing rates remain unknown, so complete labour cost is null until **every active entry has a rate**. A partial known-rate subtotal is explicitly labelled partial. No assumed labour rate, payment verification, profit/margin, booking, invoice, customer status or Sheets write is produced.
- Additive \`hq_work_time_entries\` SQLite table resides on the existing mounted \`HQ_DATA_DIR\`; no pre-existing work rows or historical J-series references are modified. Synthetic tests cover durable reload, independent direct database read, stale/mutating replay, duplicate request protection, approval/close restrictions, reverse audit, public enquiry conversion and customer isolation. Production owner-browser save and independent restored backup still require verification.
- Historic Sheets time/job-cost data, \`hq_job_costing\` manual profitability logs, staff time allocations and customer-visible progress remain separate and **unreconciled**; this ledger is authoritative only for time entries intentionally entered in native HQ. Approval is required before bulk import or replacement of other cost systems.

### Canonical HQ actual material usage (21 November 2026)

- Owner-only `/hq/work/<id>/materials` is now linked from native job editing, stage history and workshop time, including owner-reviewed public enquiry conversions. Owners can record actual consumed material/product, quantity (up to three decimal places), unit, optional actual allocated cost in **integer pence**, and an optional private source note on an approved/in-progress job. The records live in additive `hq_work_material_entries` in the same HQ_DATA_DIR SQLite database. Closed jobs are locked from ordinary edits; each entry remains visible with its historical timestamp.
- An operation ID/fingerprint blocks duplicate submissions or conflicting replays; a shared canonical HQ work revision prevents concurrent stale updates to time, materials and job state. Mistaken entries can be reversed with the original preserved, a new revision and a PII-minimised audit event. Customer details, supplier notes and material names do not enter the audit JSON.
- Unknown unit/consumption costs remain **unknown**, not zero. Only deliberately entered actual allocated usage costs form the separate ledger subtotal. Even a complete ledger subtotal is **not** final job costs, stock depletion, an invoice, a verified receipt, bank balance, approved customer selling price or profit. `hq_job_costing` manual reports, historical Sheets costs, supplier invoices and product inventory are **not automatically combined or reconciled**.
- Owner-only CSRF-checked routes, bounded inputs, synthetic regression tests, save/reload, reversal, concurrent write protection and public-enquiry identity preservation are covered. No genuine jobs, appointments, customer messages, payments, inventory, Sheets cells or financial records were modified by this implementation.
- Not yet evidenced: authenticated owner-browser input on production; production restart and independent backup restore; historical material/stock mapping; cost-ledger parity with existing profitability log; customer login and customer-visible approved status.

### Live workbook read-only schema observation (21 November 2026)

An authorised **read-only** metadata and A1:AR7 inspection of workbook `1B0XULUDiMlVuW9u0kUOYem8WelBtOH-Xey_wWVYdb2M` confirmed the historical workbook remains a multi-tab operational source. The six candidate migration tabs have title/legend or summary rows **above** the apparent table headings. At row 4, the observed nonblank cell counts (within A:AR) were: Jobs 44, Customers 32, Cost Library 12, Maintenance Clients 40, Maintenance Visits 19 and Product Stock 19. This is **only preliminary layout evidence**, not a verified column-to-domain mapping or proof that every section shares the same header. Row 1 must not be used as a universal header, and generated formulas, grouped sections, real J-series identities, duplicates and source ownership require per-tab dry-run inspection before any import. No row/cell was modified.


### Canonical HQ native work archive/restore — 21 November 2026

- Owner-only `/hq/work/<HQ-UUID>/archive` now provides archive and restore of **native** HQ quote/job records; `/hq/work/archived` lists archived records, while active search/recent cards hide them. The existing `/hq/work/new`, draft editor and /book request workflows remain intact.
- Archive is a *visibility state*, not a work lifecycle or financial status. Only native draft, ready-for-review, completed or cancelled work can be archived; active approved/in-progress work must first follow its normal lifecycle. An archived draft is locked from ordinary scope, status and price edits until restored. Completed job identity, approved price, planned deposit, time, materials and original history remain untouched.
- Archive state and operation replay journal are additive tables on the **existing** customer_requests.sqlite3 volume. Every change advances the canonical work revision, stores a PII-minimised audit event, and supports idempotent retries and stale-write conflict detection; action history remains after restore and subsequent archive. No J-series number is allocated, no historical source is imported, and no Sheets job or outbox is written by an archive action.
- Synthetic regression tests exercise owner-only GET/POST, CSRF, archive/restore after save and reload, duplicate operation replay, simultaneous stale writes, approved work safeguards, and exclusion of owner-reviewed **public-source** enquiries from this first native-only archive workflow. Private archive state is never exposed on the public /book route.
- Active job search hides archived records; existing historical summary totals and direct owner record-history access are deliberately not rewritten or represented as reconciled finances. Backup restoration and authenticated owner-browser use in Railway remain **unverified** until explicitly exercised.
