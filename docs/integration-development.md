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

### Atomic post-commit provider observation (19 November 2026)

The owner-triggered Gmail enquiry import, Square booking refresh and Square payment refresh now use `record_committed_sync(provider)` **only after the provider adapter reports its durable local snapshot/queue operation completed**. Integration Health writes its read-success and matching provider-specific persisted-sync-success observations in **one SQLite transaction with a shared UTC timestamp**. If that observation transaction fails (for example, on its second insert), neither new success marker is committed; prior success/failure history is preserved. This avoids the earlier two-write window where a newly recorded read success could be left without its matching post-commit evidence. Explicit upstream failures and snapshot failures continue to be recorded separately. The method rejects unknown provider keys and never calls the provider itself.

Synthetic regression tests simulate failure of the second SQLite write, verify rollback of both observations and recovery after the fault is removed. This is **health-metadata consistency only**, not proof that provider permission still works, the remote dataset is complete, source records are authentic, a bank credit cleared, or a job was paid. Stored historical observations from before this change remain local historical evidence, not a correlated live reconciliation or proof of cash. No credentials, customer or payment data, provider writes, outbound messages or financial records are changed by the health recorder. The owner still needs to initiate permitted fresh provider reads, then verify actual export mappings, protected audit custody and production restore before real money reconciliation.


### Visible failure when committed-sync health evidence cannot be saved (19 November 2026)

The owner-triggered Gmail enquiry import, Square bookings refresh and Square payment-evidence refresh now **check the return value** of `record_committed_sync`. If the provider queue/snapshot has already committed but the separate integration-health observation transaction fails, the owner receives an explicit **HTTP 503** stating that the source records may have been stored but the read/sync evidence was **not recorded**. The Square payment page retains its read-only snapshot view with an owner-visible warning. The operation does not silently report a verified sync; tests simulate this failure without provider traffic.

This is a partial-success/error state, not an instruction to automatically replay an import, create bookings or mark payments received. Existing provider rows are not rolled back by a later health-recording failure; existing idempotent imports are preserved. An unreadable persistent health database can prevent an accurate readiness display until storage is repaired. No Monzo/DNA provider authorisation, bank evidence, revenue, job matching, settlement, encrypted financial staging or production backup restore was established. Next action: investigate the owner-only integration-health storage and perform a fresh owner-triggered read only once service/storage is verified.

### Subsecond settlement chronology evidence (19 November 2026)

The *offline fixture-only* canonical Monzo/DNA normalisers now preserve fractional seconds (UTC microsecond precision when supplied) rather than silently truncating both sources to whole seconds. This closes a false-candidate edge case: a bank posting at 10:00:00.100 cannot be compared as chronologically eligible for a DNA batch whose latest event occurred at 10:00:00.900, even though both have equal net pence and share the same clock second. Exact provider-ID replays differing only by fractional event timestamp are also treated as conflicting source evidence rather than identical duplicates. Tests cover canonical normalisation, timezone offsets, one-to-one settlement candidates and audited offline decision gating. Python datetime parsing supports up to microsecond precision; these checks do not authenticate a bank statement, DNA payout, time source or clock accuracy, and no actual bank, payment, booking or financial record is changed.

### Fail-closed Google read observation writes (19 November 2026)

Owner-triggered **Calendar availability**, **Maintenance Sheets**, **Finance Sheets** and **pricing/jobs Sheets** reads now distinguish a successful upstream read from successful recording of its source-specific Integration Health observation. `record_check()` reports whether the SQLite observation committed. When the provider read succeeds but its observation cannot be stored, the authenticated owner endpoint returns **HTTP 503** with a redacted integrity warning instead of an ordinary 200 or a false read-tested claim. Each workstream retains its own evidence key; no finance, customer or provider record is modified by this change. Synthetic regression tests simulate the failing observation database with mock provider responses. This does **not** establish live Google consent, workbook mappings, Calendar sync, job allocation, authenticated bank cash, a provider settlement or production backup restoration. The owner must repair persistent Integration Health storage and retry the relevant read to establish fresh source-specific evidence.

### Advisory freshness check for recorded provider evidence (19 November 2026)

The owner-only Connection & Reconciliation Readiness register now identifies source-specific read observations and persisted Gmail/Square sync observations **older than seven days**. Its `read_stale` and `sync_stale` fields flag an owner recheck; the historical `read_tested`, `sync_tested`, timestamps and historical Google read-confirmed authorisation remain visible rather than being erased. When no more serious configuration, provider failure or metadata-integrity blocker takes priority, the next safe action becomes the existing owner-initiated read or import for the **same** source. Read-only Sheets/Calendar workstreams never acquire an invented sync step. This is an *advisory local-observation age*, **not** provider token expiry, bank statement freshness, proof of source completeness, or reconciliation. Rendering the register makes no provider calls. Synthetic tests age both read and sync records independently and demonstrate recovery on a new observation.

### AES-GCM fixture-only Monzo/DNA staging rehearsal (19 November 2026)

An isolated `hq_offline_encrypted_evidence.py` module now stores **already-canonical, synthetic** Monzo/DNA previews in an explicit test SQLite file using AES-256-GCM authenticated encryption. A separate HMAC key pseudonymises owner/source/request lookup keys; no raw transaction IDs, account references, merchant references, amounts or owner IDs are intentionally stored in plaintext indexes. Each staged snapshot binds its provider, source, recorded-at metadata and unique request scope into authenticated associated data. Replaying the same request and identical evidence is idempotent; conflicting replays, wrong keys, altered ciphertext/metadata, cross-owner reads, mixed source scopes, forged verified flags and corrupted canonical evidence fail closed.

Tests cover both canonical providers, exact-pence/timestamp preservation, encrypted round trips, ciphertext opacity, owner separation, replay safety, tamper detection and an isolated synthetic SQLite backup/restore. Stage/read functions have **no Flask route, provider client, live export mapping, credentials, customer data, financial classification or settlement side effects**. The owner_ref argument is merely fixture scoping, **not an authenticated identity**. Storage encryption protects the snapshot bytes in this local rehearsal; it does not encrypt other HQ tables, prove trusted event provenance, prevent deletion/rollback of an intact old database or verify actual bank credits.

**Connection & Reconciliation Readiness remains unchanged:** Monzo and DNA are **not live adapters, not configured, not authorised, not read-tested, not sync-tested, and not reconciled**. Neither a staged preview nor an exact arithmetic match counts as revenue, a verified receipt, a Square/DNA payout, a bank balance, a customer-job payment or a liability settlement. Next safe action: owner verifies genuine source exports, column names/units and account/merchant scope; then separately approve owner-session access control, independently managed encryption keys and rotation, source provenance, UK GDPR retention, external append-only audit anchors and a real isolated production backup restore before enabling real-record staging or ingestion.


### Detached anchored-prefix protection for synthetic encrypted evidence (19 November 2026)

`OfflineEncryptedEvidenceStore.checkpoint()` now returns an HMAC-protected, non-plaintext head and row count for its **synthetic, already-canonical** encrypted SQLite snapshots. A previously **independently retained** head can be supplied to `checkpoint(trusted_checkpoint=...)` or `stage(..., trusted_checkpoint=...)`: both compare the authenticated **ordered historical prefix** (including metadata and ciphertext), detect missing rows/older database rollback or altered rows, and permit legitimate append-only growth. Guarded writes check the prior head while holding the same SQLite write transaction, including idempotent replay. Regression tests use a detached earlier SQLite backup, edited rows, deleted rows, malformed or wrong-key checkpoints and legitimate appended records.

**Trust boundaries:** This method does not store a checkpoint outside the database, make it mandatory for fixture writes, secure key custody, verify newly appended but not yet independently anchored records, authenticate provider origin, or constitute a production restore drill. Recomputing a locally generated head from an already compromised database is not an independent trust anchor. There are no Flask routes, Monzo/DNA provider calls or live financial writes. The provider register remains **unconfigured, unauthorised, unread-tested, unsynchronised and unreconciled** for Monzo/DNA. Before production use, verify the genuine source reports, owner-session and encrypted-key management, trusted off-database checkpoint custody, retention policy and actual production backup recovery, with explicit approval.


### Optional checkpoint-guarded encrypted fixture reads (19 November 2026)

The isolated `OfflineEncryptedEvidenceStore.load(..., trusted_checkpoint=...)` now verifies an independently retained HMAC checkpoint **inside the same SQLite read transaction** as the requested fixture. A locally decryptable older row cannot be presented as safely recovered when an unrelated later row was removed by database rollback, and tampering with a different row is detected if that row was covered by the trusted checkpoint. An older retained checkpoint still permits legitimate append-only growth. Wrong-key and malformed checkpoints fail closed. The guard is **optional in this fixture** to preserve existing offline tests and APIs; production use must require off-database checkpoint custody, authenticated owner sessions, validated real exports, key management, independent recovery testing and a mandatory policy. A checkpoint cannot authenticate the original Monzo/DNA records, identify a real bank balance or mark a customer job paid. No Flask route or provider-read stage changes, and no live financial/customer records are accessed.

### Gmail profile verification is distinct from enquiry import (19 November 2026)

The owner-only Gmail OAuth callback now records a successful mailbox `/profile` identity check under the separate local `gmail_profile` observation key, **not** as a successful `gmail` enquiry-source read. Reconnecting cannot clear an earlier failed enquiry import or make a label/message read appear successful. The first valid `gmail` read and `gmail_sync` success are recorded together only after the explicit owner-initiated, labelled, sensitive-filtered enquiry import has successfully committed its queue and integration observations. Callback failures continue to record a redacted `gmail` blocker, and a successful profile check alone does not clear that failure. Previously retained `gmail` success observations from older callback releases may include profile-only checks; they should not be treated as independent proof of historical message intake without a corresponding persisted-sync observation. Neither profile nor import evidence establishes outbound email permission, booking authority or financial reconciliation.

### Transaction-paired integration sync proof (19 November 2026)

Gmail enquiry imports, Square booking snapshots, and Square payment snapshots now record an **additional local committed-sync proof row**, atomically with the upstream read and persisted-sync success observations. The proof timestamp must exactly match both observations before the owner-only Connection & Reconciliation Readiness register will display `sync_tested` as true. The additional table is additive; existing integration history and customer/provider snapshots are not deleted. A failure writing the proof rolls back all three observation writes. Readiness GET remains read-only and does not create the proof table, contact providers, or update real financial records.

Older standalone `*_sync` success markers (or markers written separately from the corresponding read) remain available as *unpaired historical observations*, but are **not** promoted to verified committed-sync evidence: the owner-only register exposes `sync_proof_unverified` with a safe manual re-import/refresh action. A subsequent real owner-initiated successful import/snapshot records a fresh atomic proof. A later read-only success without a matching committed snapshot also requires a fresh paired sync check. This deliberately errs toward a recheck rather than certifying a write from matching timestamps alone. SQLite proof rows are **local application evidence only**, not a provider signature, independently protected audit anchor, guarantee of import completeness, consent validity, payment settlement, bank receipt, or reconciliation. Production restore and external audit custody remain outstanding.


### Cross-run provider-event conflicts in encrypted fixtures (19 November 2026)

The disconnected `OfflineEncryptedEvidenceStore.stage()` now checks **all prior encrypted snapshots in the same pseudonymised owner/provider/source scope** within the SQLite write transaction. A new request ID cannot silently stage a different Monzo amount/timestamp or DNA amount, fee, kind, timestamp or batch for the *same stable provider event ID*. Identical events from repeated fixture imports remain permitted, with a count of `events_already_seen_in_scope` for diagnostic purposes; repeated snapshots **must never be summed together as new receipts**. Historical rows are decrypted and canonical-schema-validated before accepting fresh evidence, including databases created before this check. Conflicts fail closed with no new record committed; other owner, bank account and merchant scopes remain isolated. Synthetic tests cover both providers, conflicts, identical re-imports and independent scopes.

This is an engineering safety rehearsal, **not** production ingestion or financial reconciliation. The local fixture owner reference is not a login and repeated data is not independently source-authenticated. The existing optional detached checkpoint cannot replace independently retained audit custody, provider provenance, protected encryption keys, verified source mappings, UK GDPR controls or a real production restore test. Monzo/DNA remain **unconfigured, unauthorised, unread-tested, unsynchronised and unreconciled** until genuine evidence and owner-approved activation are available.


### Guarded synthetic settlement-review history (19 November 2026)

The fixture-only `OfflineSettlementReviewLedger.history(..., trusted_anchor=...)` can now verify an **independently retained HMAC audit head** and the requested review history inside one SQLite read transaction. This detects a valid but older database copy when an unrelated payout review has disappeared, not just tampering with the requested pair. An older authentic head remains valid for an intact append-only prefix; missing, malformed or wrong-key anchors fail closed. Unguarded legacy reads remain available for offline fixtures but **cannot establish completeness** following a rollback.

This change is **not** connected to production HQ, an owner session, bank evidence or provider credentials. No Monzo or DNA authorisation, verified bank receipt, reconciliation status, job-payment status or balance has advanced. Trusted off-database anchor custody, real export mapping, encrypted production staging, key rotation and independent production backup restoration remain outstanding. Do not use fixture histories to report paid jobs or verified revenue.

### Square payment snapshot scope redaction (20 November 2026)

The owner-only Square Payment Evidence API and page now **withhold** cached payment IDs, amounts, counts, dates and window metadata if the current Square location differs from the one that produced the persisted snapshot, or when the read token/location is missing. A `scope_unverified` diagnostic identifies the withheld historical snapshot without disclosing its previous location. Read-only access never calls Square or deletes the existing SQLite snapshot; restoring the original configuration exposes the historical evidence again. An owner-initiated provider refresh is required to obtain evidence for a newly configured location.

This is a **confidentiality and wrong-location attribution safeguard**, not a successful read, authorisation or finance reconciliation. Same-location credentials and old provider timestamps alone do not prove that permission is still active. No customer bookings, payment allocations, refunds, receipts or bank balances are changed by this read-side check. The historical Integration Health read/sync observations remain distinct and cannot certify the currently configured Square location on their own.

### Exact Square payment/job review uses the same source scope (20 November 2026)

The owner-only exact-reference Square payment/job review now checks the stored snapshot location **before selecting or displaying the referenced payment**. If the active location has changed or its scope is malformed, both API and HTML views withhold the old provider status, amount and refund evidence, report a refresh blocker, and cannot label the comparison complete. This extends the earlier list/snapshot redaction to the separate review route; simply showing a warning while returning old provider amounts was insufficient. The original database remains intact and is reviewable after restoring the intended configuration. This check is not independent authorisation, a verified receipt, a booking or bank reconciliation.


### Square location-bound Integration Health observations (20 November 2026)

Owner-initiated Square booking and payment snapshot refreshes now pass the exact configured location to the committed read/sync observation path. The existing integration-health SQLite transaction writes a separate HMAC-sealed location fingerprint and its observation timestamp alongside the read, persisted-sync and atomic proof markers. No raw location ID, token or payment amount is written to that health-scope table. Its key is derived from the stable `HQ_SESSION_SECRET`: rotating the session secret requires a fresh Square refresh before location-bound readiness can be re-established.

The owner-only Integration Health and Connection Setup GETs compare the current location with that locally retained seal. **Unscoped legacy records, changed locations, altered scope proofs and read/sync timestamps not paired in the same commit no longer establish Square read-tested or sync-tested status for the active location.** Historical database records remain intact. Restore the original configured location to review valid historical evidence, or explicitly refresh the new intended location. The comparison is local and read-only, never contacts Square, changes appointments or touches customer payment records. The Square read-only snapshot APIs separately enforce their own scope isolation.

This is a local source-provenance control, **not** current Square consent validation, a provider-issued signature, independently protected audit evidence, paid-job allocation, bank deposit or settlement reconciliation. Test with synthetic locations only; owner must supply a legitimate production token, correct scopes/location and initiate each provider refresh. After verified read/sync, independently prove payment/refund/fee and bank settlement records before advancing reconciliation.


### Gmail enquiry sender-metadata fail-closed guard (20 November 2026)

The owner-triggered Gmail customer-enquiry importer now requires exactly one syntactically usable `From` address for each fetched message, with bounded, well-formed header metadata. Missing, duplicate, multi-address or malformed sender fields **reject the entire page before its queue transaction** rather than admitting a contactless or ambiguous message as a sales lead. The existing current-label check and legal/debt subject/domain exclusions still apply. A rejected import records a sanitised error in Integration Health and cannot create a successful read/sync observation; existing previously imported customer messages are preserved.

This is a local metadata/queue safety check, **not sender authentication**, proof that every legal/debt message is excluded, current Gmail authorisation or evidence that the live business mailbox has been read. A legitimate message rejected by the new rule must be reviewed within Gmail, not forced into HQ. Real provider read and committed sync evidence still require a successful explicit owner-initiated import. Monzo/DNA mapping, bank receipts and reconciliation remain unrelated and unverified.

### Gmail header-based legal/debt isolation hardening (20 November 2026)

The existing read-only, owner-labelled Gmail enquiry importer now checks a single optional `Reply-To` identity as well as the mandatory `From` sender for recognised legal/debt cues. A clear legal/collections reply address is excluded even if the apparent sender and subject look like sales mail. More than one Subject or Reply-To header, or an ambiguous/malformed Reply-To address, fails the entire page **before** the local enquiry queue is written. This preserves existing queue records and source messages while preventing duplicate Subject headers from masking a sensitive first header. Synthetic tests cover valid customer reply addresses, sensitive reply addresses, malformed/duplicate headers and atomic page rejection.

This does **not** authenticate message authors, fully classify all legal correspondence, guarantee live mailbox coverage, prove Gmail is currently authorised/read-tested/sync-tested or authorise sending. Owner-managed Gmail labels and manual review remain required. No provider APIs are called when viewing readiness; no bookings, messages, financial transactions or customer records were changed by this code release.

### RFC 2047 Gmail subject isolation (20 November 2026; owner-initiated import only)

The existing read-only, manually labelled Gmail enquiry importer now decodes RFC 2047 encoded Subject metadata **before** applying the conservative legal/debt/enforcement text exclusion and before saving an ordinary sales subject. An encoded court subject cannot bypass the plain-text classifier; invalid, unsupported or undecodable encoded words fail the entire import page before SQLite changes. Synthetic tests cover genuine encoded customer subjects, sensitive subjects and malformed encodings. This is a forward-only privacy safeguard, **not** sender authentication, a complete legal-mail detector, retrospective queue cleanup, a verified Gmail provider read, or financial evidence. No OAuth grant, external message, booking or financial record is changed.


### Gmail RFC 2047 sender-name isolation (20 November 2026; read-only import)

The manually labelled Gmail enquiry importer now screens both ordinary and RFC 2047-encoded **From** and **Reply-To display names** for the existing clear legal/debt/enforcement phrases, in addition to the mailbox addresses, decoded Subject and snippet. This closes a narrow case where a legal sender used a neutral email address and a legal/court display name with an innocuous subject. Names are only inspected in-memory for the page; they are **not** added to the sales queue or treated as authenticated identities. Malformed encoded display names fail the whole batch before storage, preserving already-saved enquiries. Synthetic tests exercise plain and encoded names, Reply-To and malformed metadata. This is not comprehensive legal-mail detection, live Gmail consent verification or retrospective cleanup; owner labels and review remain necessary. No external messages, customer bookings, money movements or provider configuration are changed.

### Gmail list-page integrity before persisted-sync evidence (20 November 2026)

The existing owner-triggered Gmail enquiry importer now checks the *list response* before fetching or storing any message: a valid empty search can omit `messages`, but any present list must contain at most the requested ten distinct, valid provider IDs. Null, non-list, duplicate, malformed or over-limit listings fail closed before the queue database is written. Incoming and returned opaque pagination tokens are bounded, printable ASCII; malformed values do not reach the next provider fetch or appear as successful import evidence. A legitimate zero-result page still returns zero imported rows, not an invented sales enquiry. Synthetic tests cover the fail-closed cases and valid pagination.

This guards against declaring a successful page import after structurally invalid Gmail list evidence; it does **not** authenticate a current OAuth grant, prove complete mailbox coverage, identify every legal/debt message, justify auto-replies, or establish any financial or reconciliation state. The owner must still label genuine business enquiries and initiate a current read under HQ consent. Monzo/DNA authentic export mappings, staging, independent bank evidence and production restore verification remain outstanding.

### Square booking page integrity (20 November 2026)

The existing **owner-triggered, read-only** Square booking refresh now fails closed when the provider supplies a malformed/non-list or oversized (>100) booking page, duplicate booking IDs within or across pages, ambiguous/invalid continuation cursors, or invalid booking identifiers, versions, statuses or source locations. Provider pages are validated **before** SQLite booking snapshot writes or integration-health success observations. The previous snapshot survives a rejected refresh; a failed refresh does not imply zero bookings or cancellations. Regression tests use synthetic, multi-page and corrupted provider payloads only.

This hardening does **not** verify live Square credentials, current seller/location access, completeness of historical bookings, canonical HQ appointment alignment, an outbound booking or financial settlement. Owner-only readiness must remain tied to a genuine configured-location read and committed snapshot before advancing.


### Square payment page-integrity guard (20 November 2026)

The existing **owner-initiated, read-only** Square payment refresh now verifies that each provider page is a list containing no more than its requested **100** payments. A missing pagination cursor is the only end-of-window signal; empty, non-string, repeated, whitespace-containing, non-printable, non-ASCII or overlong cursors fail the full refresh. Previously verified local payment snapshots remain intact when the new provider response is ambiguous. Bounded two-page success and rejection with snapshot retention are covered by synthetic regression tests. The owner-only connection-readiness marker must be recorded only after a complete provider read and durable snapshot write. **No live Square credentials were installed, provider read executed, payment allocated to a job, settlement verified or bank balance/revenue updated by this change.**

### Offline Monzo/DNA CSV header identity hardening (20 November 2026)

The existing owner-only `/hq/transaction-import-preview` rejects duplicate headings after Unicode NFKC normalization and case folding, and headings containing invisible formatting/bidirectional or control characters. Validation happens in both header-only and explicitly mapped preview before any manual bank classification proposals. Safe distinct headings (including ordinary accented text and `£`) remain supported with exact mapping. This is an input-integrity guard, not a real provider parser, not a conversion between pounds and pence, and not authenticated bank or processor evidence. Synthetic regression tests assert early rejection and no financial claims; export ownership, documented monetary units, persistent staging and independent reconciliation remain owner-controlled blockers.

### Reject duplicate JSON review instructions (20 November 2026)

The owner-only, non-persistent Monzo/DNA schema preview now rejects repeated keys in **text-encoded JSON** submitted for exact column mapping or Monzo classification proposals (including nested repeated keys). Standard JSON parsers otherwise silently keep the last conflicting mapping or classification, making the owner's intended instruction ambiguous. Both the private form and API route fail closed with a generic error, without echoing IDs, amounts or notes. Distinct JSON keys and explicit offline mappings continue to work. This **does not** validate the original Monzo/DNA export schema, authorise ingestion, persist classification decisions, verify source ownership or establish a receipt/settlement.


### Native JSON owner-preview duplicate-member validation (20 November 2026)

The owner-only `POST /api/hq/transaction-import-preview` now decodes native JSON using the same strict duplicate-key rejection already used for text-encoded mapping and classification JSON. This protects top-level fields, nested canonical mappings and Monzo classification proposals from last-write-wins ambiguity when sent by an API client. The CSRF check runs before native JSON parsing; invalid UTF-8 or malformed native JSON fails closed. Fixture tests verify that rejected requests do not save source data or alter financial records. This is still **offline schema/classification preparation**, not verified Monzo/DNA mapping, bank authorisation, provider sync, cash receipt, settlement or reconciliation.

### Revision and retry integrity guard (20 November 2026)

The local-only Sheets outbox now rejects invalid, boolean, negative, overflow and **future** work revisions rather than advertising a revision which has not committed to HQ. A delayed call for an older saved revision is ignored; identical revision replays do not reset a failed item's retry history. When an actual newer HQ revision commits, its queue entry coalesces and its retry attempts/error state are reset for the new snapshot. These checks run inside the same local SQLite transaction as the approved internal HQ state change. Synthetic tests cover each case and no external Sheets call or writer is introduced. **Pending** does not mean authorised, linked, dispatched, read-back verified or reconciled.


### Jobs worksheet live read-only row-link inspection (20 November 2026)

New owner-only `/hq/sheets/job-link-preflight` page and CSRF-protected POST `/api/hq/sheets/job-link-preflight` let the owner submit a specific manually reviewed J reference, Jobs row (5–2000), and `existing` or `new` candidate mode. Only an explicit owner POST calls Google Sheets. The adapter `hq_sheets_live.read_job_link_snapshot` uses the existing owner-authorised **spreadsheets.readonly** OAuth token and `spreadsheets.get` with a restricted `'Jobs'!A4:AJ` GridData range/field mask. The prior pure `preflight_job_link` evaluates a complete workbook-identity/header/ID/target-cell snapshot; it fails closed on absent coverage, duplicate IDs, formulas in mapped inputs and occupied new rows. No Google Sheets values, customer names, balances, credentials, job references or raw GridData are returned to the browser or stored by this inspection. The owner-only, no-store UI reports only a pass or fixed conflict, with the external write and reconciliation flags remaining false.

The operation records its own `sheets_job_preflight` read observation, separately from pricing/finance observation and committed-sync proofs. If the observation write fails, HTTP 503 prevents a successful readiness claim. OAuth absence, invalid input, source conflicts and read failures each fail safely; tests use synthetic snapshots and tokens. No live source read has been performed by deployment or automated testing. A full GridData row scan may be rejected when Google omits trailing blank rowData: this deliberately blocks rather than invents complete coverage until verified through an authorised owner-triggered read. This is not a row mapping, historical import, approved write, quote conversion, provider sync, bank settlement, cash receipt, or reconciliation. Owner must first verify actual source coverage, B:E headings and historical J IDs; writing remains independently blocked pending a concurrency/baseline policy and explicit restricted write authorisation.

### Sheets link inspection strict-request boundary (20 November 2026)

The owner-only read-only Jobs-link inspection POST now bounds the request stream to 512 bytes regardless of Content-Length, rejects invalid UTF-8 and malformed JSON, and rejects duplicate JSON object fields (including nested conflicts). CSRF is checked before parsing; all invalid candidate instructions are rejected before contacting Google or recording a successful inspection. Synthetic owner HTTP tests cover these error cases. A valid snapshot remains an unlinked, nonpersistent proposal; no write scope, background worker, job sync, receipt allocation or provider reconciled state is enabled. Real workbook row coverage and historical job identity verification remain necessary.


### Jobs J-reference casing guard (20 November 2026)

The existing owner-triggered read-only Jobs preflight now uses the **same case-insensitive comparison** for a manually proposed J reference as for duplicate detection throughout the worksheet. A stored `j0083` blocks allocating a new `J0083` even if the chosen target row is blank. Existing-link inspections require the precise original-case historical reference. Synthetic helper and owner API tests confirm a 409 block without exposing sheet IDs or customer rows, mutating production data, enabling writes or upgrading live provider readiness. The full GridData range/blank-row coverage issue remains fail-closed until a genuine owner read validates its exact response contract.


### Distinct owner Jobs preflight observation (20 November 2026)

The Integration Health readiness register now displays a separate `sheets_job_preflight` workstream for the previously implemented, owner-triggered read-only Jobs row inspection. This is distinct from `pricing_sheets`, `maintenance_sheets` and `finance`; no observed inspection can advance those sources or vice versa. The setup step explicitly requires the owner OAuth flow used by `read_job_link_snapshot`, not merely the service account. Failed candidate checks remain inspection failures and never count as successful row links. A historical successful inspection cannot demonstrate present provider permission or verified HQ-to-J identity; sync and financial reconciliation remain disabled and unverified. The outstanding engineering blocker is genuine, complete Google GridData coverage including trailing blank rows; do not relax the strict full scan based solely on fabricated fixture data.

### Sheets job-link candidate: required writable headings (20 November 2026)

The owner-triggered read-only row preflight now requires nonempty literal
headings for every potential writable Jobs input column (A:E, F:J, Q:R,
AG:AJ); previously the unverified B:E headings could be missing while a
candidate was labelled snapshot-safe. Synthetic cases exercise absent,
whitespace, formula and duplicate B:E headings. Exact B:E semantic mapping,
Cost Library validation, complete actual provider row coverage, historical
HQ-to-J identity, conflict-safe write coordination and separate approved
editor consent are **still unresolved**. No external write or finance
reconciliation is introduced. Readiness stage evidence remains unchanged.


### Jobs row-link read-only tail coverage (20 November 2026)

The existing `/api/hq/sheets/job-link-preflight` owner POST now passes the
selected row to the Sheets adapter. A Google `GridData` response that omits
trailing blank rows can be supplemented by a second, explicit A4:AJ<rowCount>
`spreadsheets.values.get` request using `valueRenderOption=FORMULA`.
The pure completion helper requires exact Jobs range identity, ROWS dimension,
no populated rows beyond the raw tail, and agreement of existing job IDs across
the two reads. It fills *only* the omitted trailing rows for reference-uniqueness
analysis; an owner target absent from the first raw GridData stays blocked.
Partial or inconsistent responses and provider failures do not advance health
evidence. No new OAuth scope, Google write request, external transaction,
persistent mapping, payment allocation or reconciliation is activated.

Google's values API explicitly omits trailing empty rows and returns formulas
when `FORMULA` is requested (Google Sheets API basic reading documentation).
The two reads are non-atomic; concurrent workbook edits, verified historical
HQ-to-J identity, remote-edit baseline, owner-specific write consent and
independent finance evidence remain hard blockers. Synthetic tests cover the
safe completion and blocked cases. Do not classify this as live Sheets sync.

### Sheets follow-up read candidate-input race guard (20 November 2026)

A bounded read-only GridData + FORMULA Values scan now compares the manually selected candidate row's mapped input cells, not just Job IDs, when the raw response omits a trailing row. Any follow-up value in a cell that was blank in the original raw candidate causes a fixed, customer-data-free conflict. Tests cover a newly inserted customer field, formula and numeric value in a proposed blank row, and a changed historical candidate input. Pre-existing numbers/dates are not naively compared to formatted Values output. This improves detection of inconsistent snapshots but does not create atomicity, prove all existing cells unchanged, validate genuine owner access, implement Sheets writes, or verify a payment, job identity, bank receipt or settlement. All connection-readiness stages remain evidence-gated and live writes disabled.

### Sheets candidate existing text stability (20 November 2026)

The two-read, owner-triggered Jobs link preflight now rejects an existing literal string input being changed **or deleted** between its raw GridData snapshot and FORMULA-rendered trailing-row check; it still rejects additions to previously blank input cells. This protects against one class of concurrent workbook edits without mistaking differently formatted numeric/date values for changes. It is only a best-effort read-only race check: the reads are not atomic, existing numeric/date/boolean input changes are not fully checked, and future owner-approved writes must separately verify a protected baseline and conflict/rollback policy. Fixture tests are not Google authorisation, real read evidence, sync, job payment, or reconciliation.


### Typed read consistency for Sheets job-link inspection (20 November 2026)

The read-only Jobs preflight now cross-checks occupied numeric/boolean mapped
input cells against an exact-range `UNFORMATTED_VALUE` read when Google
GridData omits trailing rows. Its earlier `FORMULA` check still detects
added formulas and literal-text changes; a typed mismatch or third-read
failure blocks the inspection. An unchanged formatted currency/date may
legitimately differ from the raw underlying number, so the unformatted read
is compared instead. This is fixture-tested, non-atomic evidence screening,
not a verified production provider read, historical mapping, secure write
baseline, authorised Sheets write, live job sync, bank receipt or
reconciliation. No connection-readiness stage is automatically advanced.


### Read-only historical Jobs identity review (20 November 2026)

Added `hq_sheet_identity_review.py` and synthetic regression tests alongside the existing Sheets job-link preflight. Owner-entered optional `work_id`/`expected_revision` fields in `/api/hq/sheets/job-link-preflight` are accepted **only** for `mode=existing`; the server re-reads the internal HQ work record after the owner-authorised Google inspection to reject stale revisions. A PII-free comparison shows provisional agreement/difference/unavailable for Customer C and Quoted Price Q (exact integer pence comparison). The existing read-only owner UI exposes these flags without customer names, registration numbers, source rows, exact prices or tokens. Tests include stale-work races, malformed field combinations, pricing discrepancies and privacy checks.

**Blocker:** The real workbook B:E semantics, historical business identity and current write baseline remain owner-unverified. Two fields with equal-looking values cannot establish identity, let alone bank-reconciled receipts. This review never creates link rows, updates outbox states, expands Google permissions, calls write APIs or marks provider sync/reconciliation complete. The next safe work is owner-reviewed exact historical identity and a separately approved, auditable link transaction with strict uniqueness/concurrency controls; external Sheets edits still require distinct write authority and collision policy.


### Fixture-only historical Sheets candidate reservation ledger (21 November 2026)

The isolated `hq_offline_sheet_link_proposals.py` rehearsal now stores only owner-scoped, **unverified** HQ work/J-row candidates in a caller-supplied SQLite database, with one-to-one work/reference/row uniqueness, exact operation replay, revision metadata and same-transaction event entries. Synthetic tests cover duplicate references, conflicting replays, cross-owner scope and rollback when the event write fails. There are **no Flask routes, production database tables, real work-record edits, Google calls, owner approvals or actual HQ-to-J links**. The local SHA-256 event payload digest is not an authenticated audit chain; production requires key management, external audit anchoring, backup/restore and access enforcement.

The staged state is permanently `unverified_candidate`. Even a shape-valid historical preflight is not source authenticity or independently validated B:E workbook semantics. Owner-reviewed identity, current HQ revision, independently re-read Sheets source, permission to write, protected write baseline, transaction/audit retention and explicit approval remain blockers. No Sheets or finance readiness state advances; do not turn proposals into verified receipts, bank income or reconciled jobs.


### Offline historical candidate ledger replay integrity (21 November 2026)

The unconnected fixture-only `hq_offline_sheet_link_proposals.py` ledger now offers a **read-only owner-scoped consistency inspection** before any additional proposal or idempotent replay is accepted. It requires exactly one matching local staging event per candidate; changed revision/reference rows, missing/orphaned events and mismatched checksums fail closed. It refuses a caller-owned open SQLite transaction rather than risking rollback of unrelated caller changes. Tests use isolated synthetic databases. Inspection returns only counts and negative readiness flags, not customer/job details.

**Limits:** These are local, unkeyed SHA-256 checksums and do not authenticate the Google workbook, current HQ revision, a provider permission, owner login, backup, or an independently retained audit head. Someone able to alter both the row and its checksum (or remove both row and event) can evade this local check. This module is not registered with Flask, does not create live historical links, and cannot approve or synchronise Sheets. The owner-only Connection & Reconciliation Readiness stage remains unchanged: a genuine authorised Sheets inspection, owner-verified B:E field meaning and historic identity, separately approved durable secure staging and write baseline are still required.


### Detached offline historical-link audit-head rehearsal (21 November 2026)

The standalone `hq_offline_sheet_link_anchor.py` adds a **fixture-only, owner-scoped HMAC-SHA256 snapshot head** for proposed historical HQ-to-Sheets links. It first checks the local candidate/event ledger, then reads candidate identifiers, operation IDs, revisions, timestamps and their matching events in one SQLite read transaction. The returned versioned digest and counts contain no raw job, customer or spreadsheet values. A separately retained original head and a caller-protected 32-byte-or-longer secret allow future comparisons to detect a deleted final candidate plus event, rewritten local checksums, altered idempotency IDs or restored older copies that could evade unkeyed local checks. The module writes no database rows, saves no secret or anchor, is not imported by Flask and has no Google/API calls.

**This is not a live provider connection or an immutable production audit.** The caller must independently and securely preserve the head *outside* the same database and protect the key separately. An attacker holding the key can forge another head; no Google source authenticity, current HQ revision, owner consent, job identity, sheet write baseline, bank funds, backup or settlement is proven. Nothing has been connected to production or approved for use with customer data. Google Sheets, Monzo and DNA readiness stages remain unchanged; actual historical linking and external spreadsheet writes require fresh authenticated owner source reads and separate explicit approval.

### Sheets final typed-read candidate cell consistency (21 November 2026)

The owner-triggered Jobs link preflight's third (unformatted, serial-number) provider read now compares **all** owner-selected mapped input cells to the original GridData snapshot rather than only typed numeric/boolean fields and Job ID references. Changed/removed existing literal customer/service/status values, and newly populated formerly blank inputs, fail closed; original mapped input formulas are rejected. The separate FORMULA-rendered read and numeric/boolean comparisons remain in place. Four fixture-only regression tests cover a newly populated input, literal replacement/deletion, unchanged text and an original formula. Google Sheets is not changed.

Even three consistent provider reads are not atomic and cannot prove that the workbook will remain unchanged after inspection. `UNFORMATTED_VALUE` does not expose a newly inserted formula that evaluates to an empty string; a separately coordinated write baseline is necessary. This fix is not independent proof of live OAuth, real workbook inspection, approved HQ-to-J identity, editor authorisation, Sheets sync or financial reconciliation. Owner-only readiness stages remain evidence-gated and unchanged; next step is a genuine manual inspection and reviewed historical mapping.

### Square read-only payment timestamp chronology (21 November 2026)

`hq_square_payments._payment` now normalises the Square `created_at` and `updated_at` offsets to UTC, rejects updated-before-created provider records, and rejects future updates beyond five minutes of allowed local clock skew. This avoids committing obviously contradictory temporal evidence into the 30-day bounded, owner-only Square payment snapshot. An invalid page rolls back without replacing a previously valid snapshot. Three synthetic scenarios exercise reversed dates, far-future data and valid cross-offset chronology. Nothing calls Square during the test; no refunds, charges, bookings, payouts, source-account verification or bank reconciliation are performed. Square connection and reconciliation stages are unchanged pending genuine owner-authorised source verification and independent settlement evidence.

### Square booking same-version source integrity (21 November 2026)

The existing read-only `hq_square_bookings.refresh_bookings` snapshot now treats a same-location, same-booking, same-version record with a different persisted status or start timestamp as contradictory source evidence. The full refresh is rejected atomically before any UPSERT, preserving the last consistent local snapshot. Exact replays and late older-version pages retain their prior behaviour. Tests use synthetic Square responses; this does not prove a seller connection, external booking confirmation, provider coverage or fresh availability. Real canonical HQ appointments and idempotent outbound changes remain owner-approval gated.


### Square payment cached snapshot metadata guard (21 November 2026)

The read-only Square snapshot helper verifies saved row count against SQLite payments, requires exact sane UTC report-window/refresh chronology (maximum 31-day window; no future refresh beyond five-minute clock tolerance), and runs on the same read transaction as the payment results. The owner-only evidence response exposes an integrity_issue signal and withholds all amounts and identifiers on mismatch; the exact-reference HQ job comparison uses the same helper and cannot return a comparison candidate when cached evidence is inconsistent. Existing successful snapshots are not deleted by validation, and no background/provider refresh is performed. Synthetic tests cover deleted rows, forged counts and chronology.

This does not hash/authenticate individual stored payments or prevent well-formed tampering, and is not evidence of bank settlement, fees, refunds, source permission, live sync, approved HQ job payment or production restore. Next step remains a genuine owner-triggered Square read, exact provider/source verification, independently proven bank payouts, controlled job allocation and tested production backup restoration. Connection-readiness status remains evidence-gated.

### Square cached payment row integrity (21 November 2026)

The existing bounded Square payments snapshot now checks every cached row's provider-shape invariants (status, printable ID, GBP/other three-letter currency, nonnegative integer minor units, refund limits and chronological zoned timestamps) on the **same read transaction** as its snapshot metadata and row-count verification. This is shared by the owner-only Square evidence list and exact HQ job-reference review. Any contradiction withholds all amounts and candidate matches; evidence from rows beyond the UI's first 50 is not ignored. A manually initiated read-only refresh is the recovery route. The check cannot authenticate an otherwise plausible forged row, prove bank settlement, allocate a receipt, or authorise a refund/payment; no finance state or provider stage is promoted by these local tests.

### Square bounded payment evidence validation (21 November 2026)

The existing `hq_square_payments.py` owner-triggered GET adapter requests a rolling 30-day `begin_time`/`end_time` window. Per Square's pinned ListPayments semantics this bounds **Payment.created_at**, so each upstream item is checked against those timestamps before the all-or-nothing SQLite snapshot commit. On subsequent read-only HQ views, the entire cached set (including records beyond the 50-row display) is checked against the stored window and creation/update chronology. Violations withhold all amounts, payment references and exact-reference candidate matches rather than asserting zero activity. The previous snapshot survives a rejected upstream refresh; a local corruption requires a fresh owner-initiated read.

Provider responses can be eventually consistent; a 30-day creation window does **not** capture historical payments updated outside that period, prove complete refund/fee coverage, provide settlement data or authenticate bank cash. Synthetic tests include source rows outside the window, changed cached creation dates and altered cached window metadata. All receipts, job allocations, current Square permissions and reconciliations remain **unverified** until independent owner/provider evidence is obtained; there is no automated payment, booking, refund, email or financial writeback.


### Owner-initiated exact historical Square payment inspection (21 November 2026)

The existing authenticated `/hq/square-payment-evidence` page has an additional
**GetPayment by exact ID** form. The rolling ListPayments snapshot is filtered by
payment `created_at` and does not prove current refund status for every older
payment. Given a known payment ID, the owner can manually request
`GET /v2/payments/{payment_id}` with the existing read token (PAYMENTS_READ).
The response is checked against the exact requested ID, configured seller
location, status, timestamps and monetary bounds. Only minimal ID/status,
currency, original amount and provider-reported refund are displayed. No buyer,
card, customer identity or returned untrusted free text is rendered.

The one-off inspection is **in memory only**: no persistent provider payload,
customer/job match, readiness timestamp or committed-sync marker is written.
It cannot establish complete historical coverage, source-side payout fees,
bank settlement, receipt verification or approval to change any payment.
GET page requests do not contact Square. Missing permissions, unavailable
provider access and malformed/mismatched evidence fail closed without changing
the existing rolling snapshot. A successful result is historical provider
evidence at inspection time, **not** a verified cash receipt. Separately
obtain bank/payout evidence for any financial reconciliation. No live Square
connection was exercised or authorised by the synthetic tests.

### Square exact-reference review: request and HQ revision integrity (21 November 2026)

The owner-only, **read-only** HQ job versus Square payment candidate comparison now rejects ambiguous native JSON instructions: duplicate JSON member names (including nested), unknown/missing fields, non-string references, malformed UTF-8/JSON and request bodies above 2,048 bytes. CSRF is checked **before** JSON decoding; an omitted Content-Length cannot bypass the bounded read. Existing form submissions remain supported. A valid request never calls Square; it compares only the configured-location cached snapshot with one exact HQ work record.

The comparison now re-reads the authoritative HQ job after inspecting the cached Square payment. If the work revision, status, kind, selling price, planned deposit or source-request identity changed during the comparison, amount-match flags and the stale job figures are withheld and no candidate-complete result is emitted. This is **a race screen, not an atomic snapshot across two databases**: edits immediately after the second read are still possible. The review never marks a payment allocated, received, reconciled or bank-settled; no financial, customer, Sheets or Square record is changed. Real permissions, provider evidence, fees, independent payouts and owner-approved allocation remain outstanding. Synthetic regression tests cover revision/price/status races and invalid request envelopes. No integration readiness stage is advanced by these tests.
