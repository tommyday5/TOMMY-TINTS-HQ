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

### Distinct persisted-sync evidence (readiness integrity)
The owner-triggered Gmail enquiry import and Square booking/payment refreshes now record separate `gmail_sync`, `square_sync`, and `square_payments_sync` success observations **after** their persistent queue/snapshot operations return successfully. An independent provider read success is also required to display Sync-tested in Integration Health. Older read-only observations are never promoted into persisted-sync proof. These markers mean the application reported a successful write; they do **not** prove complete source coverage, transaction settlement, an approved HQ allocation, or independently verified bank cash. Existing SQLite customer and provider snapshot tables are untouched.

## Offline bank and processor evidence contracts (prepared, not connected)

The pure `hq_reconciliation_contracts.py` module and `tests/test_reconciliation_contracts.py` define a bounded, fixture-tested interface for **already-mapped** Monzo bank transactions and DNA Payments processor events. These are not live adapters: no source CSV/API layout has been verified, no credentials are used, and the module never writes to HQ storage or financial records. Required stable event IDs, account/merchant scope, timezone-aware timestamps, exact signed integer pence and explicit GBP currency fail closed. Exact duplicate replays deduplicate; conflicting event IDs fail the entire preview. DNA net arithmetic accounts for fees, refunds and chargebacks, while provider batch sums are **not** bank credits. A bank roll-forward can be arithmetically consistent without authenticating a statement or proving cash received. All trading receipts, job allocations and reconciliation flags remain unverified.

Before shipping any import screen, obtain the owner's actual export header/schema safely, map only the fields proven by that export, and test redaction, record-source provenance, pagination/size limits, corrections, idempotency, classification review, persistent storage and rollback. Keep source categories as untrusted hints; transfers, director funding, loans and refunds must never automatically increase trading turnover. Do not activate ingestion or alter live records without approval.

## Read-only provider setup

- Gmail: Enable Gmail API for the Google OAuth project and register
  `https://tommy-tints-web-production.up.railway.app/auth/gmail/callback`.
  Sign in to HQ as the owner and grant Gmail read-only access for the business inbox.
  Apply the `HQ-Enquiries` label only to customer enquiries, then import via Integration Health.
- Calendar: Use the existing read-only Calendar consent and run a check in Maintenance.
  Successful results apply to the primary calendar only.
- Square: Configure `HQ_SQUARE_READ_TOKEN` using the production seller OAuth scopes
  `APPOINTMENTS_READ` and `APPOINTMENTS_ALL_READ`, and set `HQ_SQUARE_LOCATION_ID`.
  Refresh under Integration Health. A refresh reads bookings from yesterday through 28 days ahead.
  The snapshot retains only booking reference, version, location, start and status.
  Missing results do not imply cancellation, and older versions cannot overwrite newer ones.
- Keep `HQ_DATA_DIR` on the mounted Railway volume. Queue and snapshot storage use separate
  additive SQLite files. OAuth consent is session-scoped for Gmail and Calendar.

## Owner passkeys and persistent Google consent

- Sign in through Google, then open **Settings → Passkeys → Add passkey**
  within five minutes. Credential management requires a recent verified sign-in.
  The iPhone chooses Face ID or device verification. HQ stores public keys only.
- **Lock HQ / Sign out** revokes the server session. Use **Sign in with a passkey**
  to unlock, or explicitly choose **Sign in with Google** for account recovery.
  After recovering, enroll a replacement passkey and remove the lost credential.
- Passkeys are bound to the production HTTPS host. Safari and the installed web
  app should be checked separately on the target iPhone. Google remains the fallback.
- Existing sessions are replaced once for this release. Reconnect each Google
  integration once to receive an offline grant. HQ encrypts grants using key
  material derived from the stable Railway session secret. Do not rotate that
  secret without planning to reconnect the integrations.
- The Railway volume stores the owner security database. Logout does not revoke
  stored Google grants or change Square configuration. Google can still revoke
  grants or require renewed consent.

## Visual maintenance routes

- **Maintenance Routes → Open visual route planner** loads the current maintenance
  sheet through the existing owner connection. Full street addresses and postcodes
  must already be recorded. No postcode-centroid pins or estimated coordinates are used.
- To enable the map after approving Google Maps usage, configure
  `HQ_GOOGLE_MAPS_BROWSER_KEY` with Maps JavaScript API and Geocoding API enabled
  in a billed project. Restrict the browser key to the HQ website referrer
  and these APIs. The key is visible to the browser and must be separate from the server key.
- To enable round-trip routes, configure `HQ_GOOGLE_MAPS_API_KEY` with Routes API
  access, and set `HQ_ROUTE_BASE_ADDRESS` to the verified workshop address and postcode.
  Restrict the server key to Routes API and apply the deployment's supported credential
  restrictions and usage limits. Route calculations only run on owner request.
- Set `HQ_ROUTE_MILEAGE_RATE` if the default planning estimate of £0.56/mile
  is unsuitable. The visual planner reports revenue after mileage. Use the advanced
  route-cost calculator for labour, materials and overhead.
- Job cards are based on internal HQ work records. The existing Google Sheets job list
  remains separate and read-only. Requested dates and recorded deposits are shown as such.

## Owner-only transaction export schema preview (not an import)

The \`/hq/transaction-import-preview\` screen and \`POST /api/hq/transaction-import-preview\` are **non-persistent, locally evaluated server-side** schema and canonical-row checks. They require an HQ owner session and CSRF token; the preview never calls the bank, processor, Google Sheets or any external service. Inputs are bounded (120 KB CSV, 32 columns, 500 rows), and neither source rows, payment IDs, sensitive notes, transaction values nor customer details are returned or written by the application. The UI does not repopulate submitted CSV after POST. Do not rely on this as a guarantee against all external infrastructure/request logging; use only authorised data and follow UK GDPR retention controls.

First submit CSV for **header inspection without a mapping**. To validate row structure, explicitly supply a JSON object mapping the required canonical fields to the exact CSV header names, and an exact account/merchant reference. The map is intentionally **not guessed** and is never marked verified. Monzo requires \`transaction_id\`, \`amount_pence\`, \`currency\`, \`booked_at\`; DNA requires \`event_id\`, \`payment_id\`, \`kind\`, \`gross_pence\`, \`fee_pence\`, \`net_pence\`, \`currency\`, \`occurred_at\`; \`batch_id\` is optional. Monetary values must already be exact signed **integer pence**, not pounds, decimals, separators, or foreign-currency conversions. The source export's units and semantics must be validated independently before these fields can be mapped. Duplicate exact provider IDs are reported; conflicting replays reject the preview.

This validation **does not establish an authorised provider connection, source ownership, verified mapping, provider read/sync, a bank balance, cash receipt, customer/job allocation, settlement or financial reconciliation**. No import, classification, writeback, transfer, statement balance change, payment creation or outbound message is possible from this endpoint. Real-source column mapping, classification review, persistent audit/idempotency, authenticated ingestion, reconciliation logic and backup/restore drills remain blocked until independent provider evidence is available.

## Owner-entered classification proposal preview (offline, not reconciliation)

The existing owner-only `/hq/transaction-import-preview` page can now accept an **optional Monzo-only** JSON map of exact transaction IDs to proposed categories **after** canonical column mapping and exact signed-pence validation. The pure `hq_transaction_classification.py` helper accepts no DNA/processor evidence, never infers labels from transaction descriptions, and refuses unknown transaction IDs, unsupported labels and unbounded input. The categories explicitly distinguish customer receipt *candidates*, internal transfers, director funding/drawings, loans, refunds, processor fees, tax, supplier expenses, wages, liability payments, other and unresolved.

The response contains **counts only**, no transaction identifiers, raw values, counterparties or financial totals. Unprovided and explicitly unresolved transactions remain pending. Proposed customer receipts are not verified revenue; any match to a job, fee, payout, bank statement balance or source ownership remains **unverified**. The endpoint does not persist the CSV or classifications, update jobs or finance, trigger payment/bookings/communications or mark Monzo configured, authorised, sync-tested or reconciled. For safe future persistence, first verify a genuine source mapping and establish an auditable, owner-approved import/review trail, idempotent corrections, backups and independent provider/bank evidence.

### Classification proposal direction and scope safeguards

The memory-only Monzo manual review now rejects attempts to propose **zero or negative bank movements** as customer receipt candidates or director funding, and rejects non-debit entries as director drawings. Mixed-account or malformed canonical evidence cannot be classified in a single preview. Transfers, loans, fees and refunds deliberately retain both possible directions, since the direction alone does not identify their nature. This check is **syntactic protection only**: a positive credit is not proof of customer payment, trading income, source ownership or cleared funds; all classification proposals remain unsaved and unverified.


## Offline classification audit ledger prototype (fixture-only, not wired to HQ)

The new `hq_offline_classification_ledger.py` provides an isolated SQLite append-only *engineering test* for future owner-reviewed bank transaction classifications. This is deliberately **not imported or activated by the Flask application**. There is no real export parsing, provider authorisation, owner login binding, financial data import, job payment allocation or production storage migration. It is tested using synthetic canonical Monzo rows and an explicitly supplied temporary database path.

Each review operation requires an explicit operator reference, idempotency key, expected revision and reason. The module validates existing bank-side classification rules, records changes as additional revisions, rejects stale and conflicting updates, and returns safe non-reconciliation flags. Bank account, transaction, actor and request references are HMAC-SHA256 keyed before storage; neither bank amounts nor source timestamps are stored in plaintext (a keyed source fingerprint identifies changed evidence). The HMAC key is supplied by the caller and is never persisted in SQLite. These are **application-level append-only rules**, not cryptographically immutable or independently authenticated bank/audit evidence. Anyone with filesystem access could delete or change the SQLite file; secure access control, encryption at rest, backups and restore verification must precede a production rollout.

**Owner-controlled blockers before enabling**: verify the real Monzo export and exact units/source IDs; decide approved import retention and UK GDPR controls; bind write routes to verified HQ owner sessions and CSRF protection; protect the key separately; prove read-only import provenance, permission boundaries, encrypted persistent storage and restore integrity; add explicit owner approval and dedicated reversal policy; test settlement allocation independently. Reviews never establish bank balances, cash receipts, trading revenue or paid jobs. The live Integration Readiness register must remain **Unverified / Not yet** for Monzo and DNA stages.

### Offline ledger integrity and synthetic SQLite restore rehearsal (18 November 2026)

The *fixture-only*, unconnected `OfflineClassificationLedger.verify_integrity()` now runs SQLite quick-check and validates chronological, gap-free per-evidence revisions, canonical UTC timestamps, allowed classifications/reasons and keyed HMAC payload fingerprints. Offline `history()` and `record()` fail closed on detected audit corruption rather than relying on a potentially altered earlier decision. Automated tests copy a populated synthetic SQLite database with SQLite's consistent backup API into an isolated restore file, reopen it with the original externally supplied HMAC key, re-verify the reviewed history and confirm idempotent request replay. Wrong-key access, tampered signed category fields and deleted *middle* revisions fail the verification check.

**Limitations:** The existing payload HMAC does not authenticate `recorded_at` or `request_key`, and without a separately anchored head/hash digest or immutable off-site backup a deleted latest revision or full database rollback cannot be detected. This is not a cryptographically immutable audit log, encrypted backup, production restore validation, Monzo/DNA authorisation, source mapping, verified receipt, job allocation or bank settlement. It does not run on Railway production financial databases. Those controls and real export schemas remain blockers before using persistent reviewed financial classifications.

### Audit metadata authentication for new synthetic review events (18 November 2026)

Offline review ledger version 2 events now sign the keyed request identity and canonical recorded timestamp in an additional HMAC seal, while retaining a stable signed intent for idempotent retries. Tampering with either metadata field, removing the new seal, or attempting to downgrade its signature version fails verification. Existing isolated fixture databases are migrated additively and their original v1 HMAC events remain readable and replay-safe; **legacy v1 event metadata is not retrospectively authenticated**. Tests cover v1 reopen/mixed-version corrections, v2 metadata tampering and read/write rejection. This is still not connected to HQ, a live bank, a provider export or approved finance records. Missing independent external audit-head anchoring means full database rollback or deletion of a final event still cannot be detected. No production encryption/backup or restore claim is made.

### Persisted-sync failure precedence and read-only readiness (18 November 2026)

`hq_integration_health.integration_status` now passes separate owner-only persisted-sync failure observations to `hq_connection_readiness.build_readiness`, alongside historical success timestamps. The latest failed post-commit Gmail or Square sync is shown as the current blocker even when an older successful read and sync exist; clearing that warning requires a subsequent successful persisted-sync observation. The historical `sync_tested` field does not claim the latest attempt succeeded. The output includes `sync_needs_attention`, `sync_last_error`, `sync_last_error_at` and `sync_applicable`; all remain within authenticated owner endpoints.

Read-only Sheets/finance/Calendar/Maps flows have no persisted sync to test, so their sync display is explicitly not applicable. A successful read leads to the specific source mapping/reconciliation task; missing configuration leads to setup rather than an impossible read. Synthetic tests cover failure after success, recovery, no-sync read-only flows and setup sequencing. This is dashboard integrity work only: do not classify any payment as received or reconcile a bank statement based on these observations.
