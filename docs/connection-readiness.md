# Connection & reconciliation readiness register

This is an **owner-only live observation register**, not a catalogue of assumed working connections. Its runtime home is the existing `/hq/integrations` page and `/api/hq/integrations` API. The repository holds the conservative definitions in `hq_connection_readiness.py`. No customer data, provider tokens, real bank balances, contact details or private integration status are committed.

## Meaning of each stage

- **Implemented**: an existing adapter / owner read flow is present in code; not proof that it works against a provider.
- **Configured**: required environment variable *names have values* or the applicable Google OAuth project is configured; not proof of validity or access.
- **Authorised**: a usable owner OAuth consent was found for Google; API key/token-only providers remain **Unverified**, never authorised merely because a token exists.
- **Read-tested**: a successful upstream read was actually recorded in the persistent integration health history. This is **historical**, not proof that permissions still work today. Successful pricing, finance, Calendar, Gmail, Square bookings and Square payments reads have separate provider keys.
- **Sync-tested**: an owner-initiated Gmail enquiry import or Square booking/payment snapshot finished its persistent write **and** a separate post-commit sync success marker was recorded alongside upstream read success. A provider read alone never implies a sync, including historical read observations from before this change. Older import successes without a distinct marker remain **Not yet / Unverified**, not retroactively promoted. Sync does not prove completeness, job attribution or bank settlement. Sheets/finance/Calendar reads are never sync tests.
- **Reconciled**: requires separately validated, auditable approved-job allocation and independent bank-settlement evidence. No provider is marked reconciled. Identical amounts do not qualify.
- **Last success** is the UTC timestamp of the recorded successful provider read; **sync last success** is reported separately from its post-commit marker. Neither is the freshness of the underlying bank transaction or invoice. When history is unavailable, stage values are **Unverified**.

## Provider / workflow readiness plan

| Workstream | Existing implementation | Distinct first-owner action | Reconciliation still required |
|---|---|---|---|
| Google Sheets jobs/costs | OAuth and read-only Jobs, Cost Library, Settings mappings | Connect owner Sheets OAuth and read source workbook | Actual job IDs, approved prices, costing and materials mapped without changing sheet formulas |
| Sheets finance | Read-only effective values and formulas in Cash Engine / HQ Dashboard | Open Finance Command Centre, refresh; verify exact workbook | Distinguish cash balance, monthly trading receipts, funding, transfers, commitments and uncollected deposits |
| Gmail enquiries | Business mailbox check, labelled read-only import, deduped provider message IDs | Enable Gmail API and read-only business inbox consent; apply HQ-Enquiries ONLY to customer mail | Review customer identity; legal/debt mail must not be mislabelled or turned into enquiries |
| Google Calendar | Read-only primary-calendar conflict checks | Owner consent, then explicit private live conflict check | Canonical HQ appointments, provider version checks, conflict and idempotent change handling |
| Square bookings | Bounded read-only snapshot on persistent SQLite, versioned by booking ID | Configure HQ_SQUARE_READ_TOKEN, HQ_SQUARE_LOCATION_ID and correct appointments scopes; refresh | Link authoritative booking IDs to canonical appointments before actions |
| Square payments | Bounded read-only evidence and exact-reference candidate comparison | Configure token, PAYMENTS_READ and location; refresh | Refunds, processing fees, split payments, approved allocation and bank settlement |
| Monzo | Fixture-tested **offline canonical transaction contract** only; no live export mapping/ingestion | Confirm actual export/Sheets schema and map exact IDs, timestamp, signed GBP pence and account scope | Bank roll-forward, transfers, director funding, classification and liabilities; arithmetic match is not bank verification |
| DNA Payments | Fixture-tested **offline canonical event contract** only; no verified provider mapping/ingestion | Confirm actual DNA export/API schema for event IDs, payment IDs, signed refunds, fees and batch references | Exact IDs, fees, chargebacks, refunds and independent bank-settlement matching |
| Maps / Routes | Optional server route and owner-only visual map code | Approve billable APIs, set restricted browser and server keys, verify actual addresses | Validate mileage/time and pricing, not booking confirmation |

Other open workstreams: maintenance visits and prepaid package balances; retail, trade and maintenance account isolation and tier pricing; customer booking requests versus confirmed appointments; basket and checkout security; notifications with owner approval; passkeys/PWA update handling; approved quote/job costing and customer-safe PDFs. A public booking request is **not** a confirmed booking, and a planned deposit is **not** a verified receipt.

## Security and safe advancement

All readiness data is only returned through existing owner-authenticated HQ endpoints with `Cache-Control: no-store`. The readiness page does not call providers, create databases, export financial data, charge customers, send messages, book appointments or alter records. Connection secrets and OAuth refresh tokens must remain server-side, encrypted where stored and scoped to the owner. Preserve `HQ_DATA_DIR` on Railway's persistent /data volume. Backups and restore drills remain a separate unverified workstream.

Production may have a working login or token without an authorised provider read. Provider permission changes and failed recent reads must remain visible. Never replace unknown receipts with £0 or count transfers, refunds, planned deposits or invoices as bank cash.

**Next safe engineering sequence:** validate real Monzo and DNA export schemas against the offline canonical contracts without importing customer records; build a bounded, owner-only preview and persistent review log only after source mapping is verified; then develop reviewed, idempotent payment allocations with rollback/audit and independent bank reconciliation. This contract does **not** establish live ingestion, provider authorization, or verified bank cash. Never merge if CI fails; verify deployed commit and persistent volume after each merge.

## Offline CSV schema inspection — independent engineering progress

The owner-only [transaction export schema preview](/hq/transaction-import-preview) can inspect headings and validate explicitly mapped canonical Monzo/DNA rows against the offline contracts. It does not contain a verified source-specific export parser and it does not mark Monzo or DNA as **Implemented**, **Configured**, **Authorised**, **Read-tested**, **Sync-tested** or **Reconciled** in the live provider register. Missing provider consent, actual account/merchant scope, exact export headers and documented original monetary units remain the owner-controlled blockers.

The preview has no persistence path, no provider calls and no customer-financial data writeback. It reports counts and validation issues without returning source values, bank totals or sensitive references. See \`docs/integration-development.md\` for safe use and explicit mapping requirements. It cannot turn a consistent arithmetic result into proof of a bank settlement or an HQ job receipt.

## Offline owner classification proposal review (non-persistent)

The schema preview now offers an **optional Monzo-only manual classification proposal** after explicit canonical mapping. It returns only per-category counts and unresolved counts, never amounts, account/transaction IDs or verified-revenue flags. DNA evidence is intentionally not eligible for bank transaction classification. No records are saved; no balance, bank settlement or customer job receipt is verified. This is **offline preparation**, not a live Monzo integration or a status advancement in the owner-only readiness register. Real export units, source scope, bank statement authenticity, manual approval, immutable classification audit and reconciliation remain outstanding.
