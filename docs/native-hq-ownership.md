# Native HQ operating records — migration register (21 November 2026)

**Priority:** Native, editable HQ records rather than further read-only connection adapters. Historic workbook 1B0XULUDiMlVuW9u0kUOYem8WelBtOH-Xey_wWVYdb2M remains read-only and intact. No bulk import, J-reference allocation, backfill, Sheets write consent, or bidirectional sync has been activated.

## New native owner workflow

- Owner-only /hq/work/new: create a new Retail/Trade/Maintenance customer, or select an existing customer; add or reuse a vehicle; create a quote or job draft with service, scope notes and requested (not confirmed) date/time.
- Owner-only /hq/work/<id>/edit: update draft service, tier, scope and requested date/time with optimistic revision checking. Approved records lock their work scope. Existing job cards handle pricing/approval and the existing customer editor handles contact/address edits.
- All writes are atomic in existing customer_requests.sqlite3 under HQ_DATA_DIR. Operation IDs are replay-safe; fingerprint mismatch is a conflict. PII-minimised audit stores creation and changed-field metadata.
- Canonical hq_customers, hq_vehicles and hq_work_records IDs are reused across public request conversions and direct owner creation. New native IDs are HQ-UUID source references, **never invented J-series references**.
- Customer tiers are classification only; prices require owner review. Planned deposits are not received deposits; approved or completed work is not proof of invoicing or payment.
- Unlinked native records skip the Google Sheets outbox. Provider adapters, read-only Sheets, public requests and /book remain unchanged.

## Field-level authority / migration coverage

| Domain | Local native state | Authority and migration status | Remaining evidence |
|---|---|---|---|
| New native customers and vehicles | Direct creation, existing contact editor, linked vehicles | **HQ authoritative for newly created local records only**; legacy not migrated | Owner verifies duplicates, vehicle editing, tier permissions |
| New native jobs and quotes | Create/edit draft, existing pricing/status/costing | **HQ authoritative for new UUID records only**; no J link | Pricing parity, job cards/PDF, work stages |
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
2. Build native vehicle editor, customer lookup and versioned per-field source provenance before historical importing.
3. Stage dry-run Sheets Jobs and Customers mappings, preserving genuine J references with row-level conflict detection, rollback and explicit owner approval.
4. Advance public /book request to HQ review to approved status, without treating a requested time as available or booking anyone.
5. Verify on-volume persistence and independently restored backups. A mounted volume and offline tests are not production restore proof.

No live record, sheet cell, payment, booking or message is changed automatically by deployment. The owner deliberately submits new native records through the form.
