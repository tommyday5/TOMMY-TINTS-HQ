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

The three existing owner-initiated persistence flows now raise a dedicated `PersistedSyncError` for unavailable SQLite storage or a failed snapshot/queue commit. Gmail enquiry import, Square bookings refresh and Square payment evidence refresh record a sanitised `*_sync` failure marker rather than incorrectly classifying a storage failure as an upstream provider read failure. Provider request/validation failures retain their separate upstream read-failure marker. The last successful snapshot/queue is not overwritten by a failed transaction; no customer message, booking or charge is initiated. Fixture-only route tests exercise failure classification and the owner-only readiness blocker. Never take a previously successful provider read or sync as evidence of cash settlement.

### Connection dashboard does not refresh Google tokens (18 November 2026)

The owner-only Integration Health GET paths use a read-only lookup of existing encrypted Google refresh grants and nonexpired in-memory session grants. They do **not** trigger token refresh, network requests, provider reads or grant updates. This avoids silent OAuth calls simply from opening the dashboard. Consent presence is **historical local evidence only**: provider scopes, account access and revocation cannot be confirmed without a separate explicit provider read. Missing/malformed grant records return false without creating a database or new consent. Existing live-read pathways still refresh authorisation on owner-initiated operations as before; no connected provider status or reconciliation is advanced by this change.


### Offline proposed DNA-to-Monzo payout comparison (synthetic only; 18 November 2026)

The isolated \`hq_offline_settlement_pairs.py\` contract and \`tests/test_offline_settlement_pairs.py\` exercise **explicit, one-to-one** candidate mappings between a canonical DNA processor \`batch_id\` and a canonical Monzo \`transaction_id\`. The comparison accepts only the fixture-tested, already-normalised GBP structures; it does not parse any actual DNA/Monzo export, call either provider, access HQ finance, store data, or accept unreviewed matches. References and signed exact pence are validated again, including merchant/bank scope, chargebacks, refunds and batch fees. An equal net batch and signed bank movement produce an **arithmetic candidate only**, not an authenticated settlement.

Duplicate assignment of one bank transaction to different batches, unknown IDs, malformed canonical rows and zero movements fail closed. Split payouts, combined batches, partial reports, timing discrepancies and incomplete source coverage are **not** solved by one-to-one pairing. Unbatched processor events remain separately counted. Only aggregate counts are returned, with no financial figures or references. This module is not imported by Flask or connected to the owner HQ UI. **Monzo and DNA remain not configured, authorised, read-tested, sync-tested or reconciled by this code.** Actual provider source schemas, proof of account and merchant ownership, bank statement authority, reconciliation approval, durable owner-scoped audit/backup and exact job payment allocation remain blocked. Do not publish or act on match candidates as receipts or trading revenue.


### Gmail per-message label integrity check (19 November 2026)

The owner-only labelled enquiry import now fetches Gmail label definitions read-only and checks each message's fetched `labelIds` against the actual HQ-Enquiries, HQ-Legal and HQ-Debt IDs, including its INBOX status. Query filtering alone can race with Gmail relabelling or archiving. Changed-label messages are skipped and only the count appears in the HQ queue; missing/invalid label metadata fails closed before the batch can commit. The existing content-based secondary filter remains in place. The importer never changes Gmail labels, sends emails or imports legal/debt messages by design; this does not establish successful live provider reads or solve historical queue contamination. Synthetic tests cover label drift, absent label IDs and missing enquiry-label configuration.

### Gmail enquiry isolation safeguard (18 November 2026)

The existing owner-only, Gmail read-only, manually labelled enquiry intake now searches `in:inbox label:HQ-Enquiries -label:HQ-Legal -label:HQ-Debt` and applies a secondary **local** check against recognisable legal, enforcement, debt, penalty and court-related sender/subject/snippet cues before queuing customer leads. Excluded messages remain in the original Gmail mailbox; only an aggregate skipped count is returned, and neither their subjects nor bodies are persisted in the sales queue. Ordinary customer quote and booking enquiries retain their existing deduplication and manual review. Synthetic tests cover mixed, incorrectly labelled batches, recognised sensitive sender domains and normal sales mail.

This safeguard is **not a comprehensive legal-mail classifier**: Gmail snippets and headers cannot reliably identify all sensitive emails. The owner must continue applying the correct labels and reviewing ambiguous messages directly in Gmail. Existing previously stored queue entries are **not deleted or reclassified** by this change; assess any historical mislabelling separately with owner approval. No new Google scopes, provider consent, live Gmail reads or outbound actions are activated by the deployment.

### Revalidate canonical evidence at each offline calculation boundary (18 November 2026)

`hq_reconciliation_contracts.py` now provides shared `_validated_monzo_events` and `_validated_dna_events` safeguards. Bank roll-forward, DNA batch grouping and the fixture-only one-to-one settlement comparator re-normalise all supplied canonical events, compare the full ordered tuples and reject altered proof flags, duplicate IDs, mixed account/merchant scope, altered signed-pence amounts, kind inconsistencies and unbounded data. A mutable Python preview dict is not itself authoritative evidence, even if it was originally returned by `normalise_monzo_rows` or `normalise_dna_rows`. Synthetic tests exercise these failures without importing a provider export or using any actual financial records.

All returned arithmetic and equal-amount comparisons remain **unverified candidates only**. This adds no live Monzo/DNA connector, source mapping, provider authorisation, bank balance authentication, job receipt, durable staging ledger or financial reconciliation. The owner-only readiness dashboard remains unchanged in its provider stage definitions. Before activating persistence, require source-level account ownership and monetary units, owner-approved import scope/retention, encrypted storage, independent audit anchoring and backup restoration.

**Checksum limitation:** A deterministic, unkeyed SHA-256 digest records the locally normalised event tuple and helps detect subsequent accidental mutation. It is not HMAC, a provider signature, transaction authenticity, a trusted source hash or anti-tamper evidence against someone who can alter both the tuple and digest. Do not label matching arithmetic as reconciled.

### Detached audit-head comparison (19 November 2026; synthetic only)

The fixture-only `OfflineClassificationLedger.export_anchor()` now creates a **detached HMAC-SHA256 head over the entire ordered audit event table**, including the final row and keyed metadata. `verify_anchor(previously_retained_head)` checks a strictly validated, caller-supplied head and fails closed when an otherwise individually valid older database is restored, or the latest event is deleted. Synthetic tests cover full-database rollback, final-event deletion, anchor tampering and reopening with the same external HMAC key. **This does not store, automatically rotate, publish or independently preserve the anchor.** The caller must protect the HMAC key separately and retain the prior head in a different trusted system before any incident. Automatically exporting a replacement head after a rollback defeats detection. Legacy v1 metadata can only be checked against the values captured at the time the detached anchor was originally retained; the head cannot retrospectively prove their historical authenticity.

This is not an immutable audit service, encrypted production store, independently verified backup, approved Monzo/DNA importer, bank proof or reconciliation. No HQ endpoint imports this fixture and no owner data or production database is touched. Before production staging: define an approved independent head storage/rotation policy, evidence ownership/retention, backup restoration, read/write access control and externally verified provider mapping. Do not treat a passing fixture anchor as a live finance control.


### Synthetic settlement decision audit rehearsal (19 November 2026)

Added fixture-only `hq_offline_settlement_review.py` and `tests/test_offline_settlement_review.py` to bridge the gap between the existing in-memory one-to-one settlement comparator and future owner-reviewed financial staging **without connecting to production**. An isolated SQLite ledger records only keyed HMAC references, explicit `candidate` / `investigate` / `rejected` review decisions, revision/correction metadata and HMAC event seals. It revalidates canonical bank/processor evidence on each operation, rejects nonmatching payout amounts as candidates, prevents ambiguous reuse of the same bank transaction or processor batch, binds decisions to bank/merchant scope, enforces idempotent request keys and optimistic revision checks, and fails closed on tampering. No raw amounts, bank IDs, processor IDs, account numbers, customer identities or payment status flags are persisted or returned. A detached HMAC head comparison rehearses rollback detection only **when an older trusted head is independently retained**. This is a logical append-only audit, not physically immutable SQLite.

These tests use synthetic GBP-pence rows only. The ledger has **no Flask route or connection** and must not be pointed at `HQ_DATA_DIR`, actual transactions or customer records. It is not encrypted, not an authenticated production staging workflow, not a tested production backup, and not proof of provider authorisation, original export provenance, bank balance, settlement, taxable income, or an HQ job receipt. The register's Monzo and DNA statuses remain **not implemented as live adapters, not configured, not authorised, not read-tested, not sync-tested, not reconciled**. Before real usage, obtain actual owner-confirmed export schemas and monetary units, define account/merchant consent and data-retention policy, add encrypted owner-only storage and independently protected audit heads with a real backup/restore test, and implement approved source-backed allocations with explicit evidence and reversible corrections. No payment, email, customer record or appointment is altered by this module.

### Manual Monzo classification — canonical evidence revalidation (19 November 2026)

The memory-only `preview_manual_classifications` now reuses `_validated_monzo_events` before applying owner-entered proposals. This checks the original local canonical-event digest, chronological/event fields, exact signed GBP pence, account scope, source-unverified flags, duplicate IDs and normalization semantics. A valid-looking debit changed into a credit, edited timestamp, reordered record sequence or falsely marked verified receipt fails closed even if the operator-supplied proposal itself is syntactically valid. Synthetic regression tests cover amount, timestamp, order, forged verification flags and missing digests.

**Trust boundary:** The digest is unkeyed and local. It cannot authenticate an external Monzo export or resist replacement of both source values and a consistently recalculated digest. This is **not** production Monzo ingestion, source mapping, approval, receipt verification, bank balance verification, reconciliation or owner-only encrypted persistence. No live finance data or providers are touched. Independent provider-origin evidence, real schema/units, protected review storage, restore drills and bank/settlement proof remain required before those stages can advance.


### Internal workflow readiness coverage (19 November 2026)

`hq_workflow_readiness.py` adds a **non-provider**, static operational checklist to authenticated Integration Health. The existing `hq_connection_readiness.py` provider observations still control actual Google/Square/Monzo/DNA/Maps stages. The new internal register tracks nine independent workstreams, identifies which have partial implementation and which lack complete customer account, checkout or outbound messaging controls, and gives each an owner-only blocker and safe next action.

`persistent_storage_configured` comes only from `customer_request_store.storage_ready()`: it means a non-temporary `HQ_DATA_DIR` is declared, not that the storage is writable, encrypted, protected from rollback or restore-tested. All internal `authorised`, `read_tested` and `sync_tested` fields remain unknown; `reconciled` stays false and `last_success` remains null. Actual upstream read/sync evidence remains in the separate provider register. Rendering the page must not read customer/finance rows, create a database, call providers, send email/SMS or change appointments/payments. `tests/test_workflow_readiness.py` tests these conservative boundaries and the owner-only UI.

### Separate pricing and maintenance read evidence (19 November 2026)

Use `pricing_sheets` only after a successful owner-initiated `/api/hq/pricing` workbook read; use `maintenance_sheets` only for a successful owner-initiated `/api/hq/maintenance` workbook read; preserve `finance` for the finance command centre. Each failure also records against its corresponding key. The older shared `sheets` observation remains in SQLite for audit continuity but is **not promoted** to either new source-specific read-tested stage: prior records cannot distinguish the workbook, and guessing would give false readiness.

Regression tests simulate separate successful and failed reads, check the private API and ensure neither source advances the other or claims sync/reconciliation. This is an additive health-observation change only. It does not change Google scopes, production workbook values, Sheets formulas, maintenance visits, pricing, reconciliation or provider connection grants. Owner must run each read explicitly before the dashboard can show observed source-specific success.


### Offline payout candidate chronology guard (19 November 2026)

The synthetic-only `hq_offline_settlement_pairs.py` now checks canonical UTC posting chronology in **explicitly proposed** DNA batch-to-Monzo transaction comparisons. A bank movement posted before the *latest* processor event included in its proposed batch is a chronology conflict: matching signed pence is reported separately as `amount_equal_but_chronology_conflicts` and is **not** counted as an eligible amount-equal candidate. The fixture-only audited settlement review permits `investigate` or `rejected` for this situation but refuses the `candidate` decision, including on correction/retry. It does not invent settlement lag thresholds, infer payout dates or use free-text matching. Multiple processor events in a batch and same-time UTC boundaries have synthetic regression coverage.

This is a conservative plausibility check, **not a verified provider chronology**. Delayed exports, correction records, posting-date semantics and timezone differences require genuine source documentation and owner investigation. Exact amounts and plausible chronological order still cannot prove source completeness, authorised account ownership, verified bank cash, settled job payments or trading revenue. No production import, provider call, HQ finance write or live connection-readiness stage changes. Next safe action: verify real Monzo/DNA report schemas and date semantics, then build owner-approved encrypted staging, independent audit anchors and a production restore drill before any real-data decision.

### Saved OAuth grant versus read-confirmed permission (19 November 2026)

The owner-only readiness register now separates `consent_present` from `authorised`. Local encrypted Google grant presence is configuration/consent **evidence**, not verified provider permission. With a saved grant but no source-specific successful upstream read, the authorised stage reads **Grant present; permission unverified**. After a successful recorded read, the authorised stage explicitly says **Historically read-confirmed (not current proof)** and exposes `authorisation_verified_at` as the last source-specific success timestamp. Failed later reads remain visible as blockers, and old read evidence is not deleted. Non-OAuth token providers are not certified simply because an environment variable exists.

This change is status presentation and evidence semantics only: no OAuth refresh, remote API call, live permissions check, cash verification, data import or booking occurred. Fresh owner-initiated reads, source-specific schema mapping, financial reconciliation, independent bank evidence and verified backups remain outstanding. See the private `/hq/integrations` page; the public repository contains definitions/tests only, not real owner connection status.

### Malformed/future integration-health timestamp rejection (19 November 2026)

`hq_integration_health.valid_observation_time` centralises bounded timezone-aware ISO observation timestamp checks (including a five-minute clock-skew allowance). `integration_status` suppresses malformed or future read-success timestamps and flags invalid history. `hq_connection_readiness.build_readiness` additionally validates independent post-commit Gmail/Square sync markers, exposes an owner-only integrity blocker and refuses to turn invalid local metadata into historical read, sync or OAuth authorisation evidence. The read-only GET path neither rewrites SQLite nor calls upstream providers.

Regression tests corrupt a synthetic health SQLite timestamp, exercise invalid, future and unzoned values, and verify owner-facing stage semantics. Restoring a well-formed **new local observation** is a test of timestamp handling, not a verified backup drill or upstream authorisation. Production OAuth grants, bookings, transactions, bank balances, reconciliation and payment statuses are unaffected.

### Historical settlement audit anchor prefix verification (19 November 2026; fixture only)

The isolated `hq_offline_settlement_review.py` now distinguishes **exact full-head** checks (`verify_anchor`) from **previously anchored prefix** checks (`verify_anchor_prefix`). A prior detached, independently retained HMAC head can be compared with the signed prefix of a longer append-only synthetic review history. New reviewed corrections therefore do not invalidate older trustworthy anchors, while a missing/replaced anchored event, malformed anchor, tampered signature or rollback below the anchor's recorded event count fails closed. Regression tests exercise append-only growth, earlier-event tampering, and a restored older fixture database. All current events are integrity-checked before comparing the prefix.

**No production anchoring is configured.** The fixture relies on a caller-provided secret and a prior head stored independently of the SQLite database; no trusted off-database retention, key management, encryption at rest or production restore test exists. A locally computed head or matching prefix cannot authenticate a provider report, Monzo bank entry, DNA settlement, bank balance, paid job or trading revenue. No live integration stage is advanced. Real source mappings, private owner-approved staging, independent audit storage, UK GDPR retention decisions and an isolated production backup restore remain prerequisites before handling financial records.


### Guarded fixture settlement-review writes (19 November 2026; not live)

The isolated `OfflineSettlementReviewLedger.record(..., trusted_anchor=...)` now optionally compares a caller-supplied, independently retained HMAC audit head against the **verified entire current SQLite history** while holding the same `BEGIN IMMEDIATE` write transaction. A later head blocks writes and idempotent replays after database rollback; an older valid head permits legitimate append-only reviews. Malformed heads, modified anchored history and untrusted source evidence fail closed. `verify_anchor_prefix()` uses the same prefix verification. Existing unguarded fixture calls remain compatible; future production review writes **must require an independently protected anchor**, rather than using this optional default. This does not store anchors externally, authenticate provider data, encrypt SQLite, verify a production restore, connect Monzo/DNA, change any bank/job record, or establish cash settlement. The caller must protect the HMAC key and head separately, establish custody/retention, obtain actual source schemas and owner approval before any real-data staging.
