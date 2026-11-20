# Connection & reconciliation readiness register

This is an **owner-only live observation register**, not a catalogue of assumed working connections. Its runtime home is the existing `/hq/integrations` page and `/api/hq/integrations` API. The repository holds the conservative definitions in `hq_connection_readiness.py`. No customer data, provider tokens, real bank balances, contact details or private integration status are committed.

## Meaning of each stage

- **Implemented**: an existing adapter / owner read flow is present in code; not proof that it works against a provider.
- **Configured**: required environment variable *names have values* or the applicable Google OAuth project is configured; not proof of validity or access.
- **Authorised**: an owner-scoped Google OAuth grant **and** a successful upstream read recorded for that specific integration establish only *historical* read-confirmed authorisation. A stored encrypted grant alone is exposed separately as `consent_present` and leaves `authorised` **Unverified**. Token/key-only providers remain unverified without separate authorisation evidence. A historical read does **not** prove current permissions or a currently valid grant.
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

### Monzo proposal integrity guard (18 November 2026)

Owner-only offline classification review now enforces single-account canonical evidence and prevents a debit or zero bank entry being labelled a customer receipt candidate or director funding. Director drawings must be debits. No category is inferred, and legitimate two-direction transfer/refund/loan scenarios remain reviewable. These are **proposal validation rules**, not additional Monzo connection, live-read, sync, settlement, bank balance or approved-job evidence. The Monzo register remains unimplemented as a real provider ingestion adapter until actual source mappings are verified.


### Audited classification rehearsal (18 November 2026; offline only)

A synthetic-only SQLite review-audit prototype now exists in `hq_offline_classification_ledger.py` with isolated tests. It supports explicit reviewed proposals, replay-safe operation IDs, corrections via append-only revisions, optimistic concurrency checks, account isolation and HMAC-scoped pseudonymous source IDs. It is **not connected to the owner HQ UI, not a Monzo ingestion adapter, not on the production volume and not a production immutable audit log**. No provider status has advanced: actual export ownership/mapping, provider authorisation, successfully persisted *live* ingestion, independently reconciled settlement and approved job attribution remain unverified. The next safe step is to verify genuine Monzo/DNA export schemas, secure the production review and backup design, then integrate a separately approved owner-only staging flow with rollback and restore tests.

### Offline audit integrity and restore rehearsal (18 November 2026)

The standalone synthetic classification ledger now checks stored keyed payload fingerprints, revision continuity, canonical timestamps and SQLite structure before offline history reads or new review writes. A fixture-only SQLite backup/restore round trip is covered by regression tests, including wrong-key and tamper failures. This advances *offline implementation assurance only*: it is not a verified production backup or live provider sync. Timestamps and idempotency keys are not included in the legacy payload HMAC; missing final events require separately anchored audit heads or independent immutable backups to detect. Monzo and DNA remain unimplemented as verified live ingestion adapters and **not configured, authorised, read-tested, sync-tested or reconciled** based on this work. The next safe step is genuine export schema verification and a production key/retention/backup design with an independently tested restore before approved staging.

### Offline audit metadata HMAC improvement (18 November 2026)

New fixture-only classification review events authenticate the keyed operation reference and recorded UTC time as well as classification fields, with signature-version downgrade detection and additive legacy fixture schema support. Legacy v1 metadata remains unsealed; independent off-database head anchoring, rollback/deletion detection, production backup restoration and encrypted owner-only staging remain incomplete. **No Monzo or DNA live stage advances**, no verified bank receipts or reconciled jobs arise from this offline integrity test.

### Sync failure visibility and read-only stage semantics (18 November 2026)

The owner-only readiness API now observes the **latest persisted-sync error state** independently of the last successful upstream read and historical persisted-sync success. If Gmail, Square bookings or Square payments successfully synced previously but a newer persistent import/snapshot fails, historical `sync-tested` remains a historical fact while `sync_needs_attention` is true, the exact recorded failure is surfaced as the current owner-only blocker, and the next safe action is a manually initiated read/sync retry. A subsequent successful sync clears attention; the old failure does not keep blocking the dashboard. No provider call occurs simply by rendering readiness.

For read-only Sheets jobs, Sheets finance, Calendar and Maps checks, `sync_applicable` is false and the display stage is **Not applicable (read-only)**, rather than demanding a storage operation that the integration does not perform. Once their upstream read is recorded, the next action is the *workstream-specific mapping or reconciliation review*, not a fictitious persistent sync. Missing configuration now directs the owner to the setup step before attempting a provider read. This is status logic only; no bank cash, live consent, settlement, job payment or provider reconciliation is inferred.

Owner-initiated Gmail, Square booking and Square payment refreshes now explicitly report failed persistent writes separately from failed provider reads. The owner-only register exposes the sanitised failed-sync reason even after a historical successful snapshot. SQLite read/write availability is not evidence of provider authorisation; the three flows remain unverified until real upstream and persistence observations are recorded. Current product connection and reconciliation stages have not been manually advanced by this code change.

### Local-only OAuth status inspection (18 November 2026)

Rendering the owner Integration Health API/dashboard now checks encrypted owner-scoped refresh-grant **presence locally**, using read-only SQLite, plus any still-valid in-memory session grant. It does **not** call Google, refresh OAuth access tokens, create provider connection tables, save a new grant or change the actual provider's state. An expired access token with an encrypted refresh grant can be shown as **consent present**, but this is **not a verified current permission**, and a revoked grant remains unknowable until the owner explicitly initiates a provider read. Malformed ciphertext, absent tables and different owner identities fail closed. The last recorded read/sync and reconciliation stages are unchanged. Readiness GET requests must remain free of provider traffic.


### Offline DNA payout-to-bank arithmetic candidates (18 November 2026)

An isolated, fixture-only \`hq_offline_settlement_pairs.py\` helper now checks **owner-explicit one-to-one** proposed DNA batch IDs against canonical Monzo transaction IDs, including signed exact-pence net-of-refund/chargeback/fee arithmetic and duplicate-bank-entry rejection. It returns counts only, including mismatches and unbatched events. Equal amounts remain **candidate arithmetic**, never evidence of a real bank receipt, reconciled settlement, trading income or a paid customer job. This is not wired to HQ, does not import a real statement and cannot prove source completeness or deal with split/combined payouts. The owner-only live readiness register has **no provider status advancement**: Monzo and DNA still need actual independently verified export mappings, verified account/merchant scope, owner-approved staging, audit/restore and distinct bank settlement evidence before live reconciliation can be introduced. Do not mark either provider implemented as a live ingestion adapter, configured, authorised, read-tested, sync-tested or reconciled based on this offline helper.


### Gmail label-race verification (19 November 2026)

The owner-triggered, read-only Gmail import now resolves `HQ-Enquiries`, `HQ-Legal` and `HQ-Debt` to real Gmail label IDs and rechecks each fetched message's **current** label IDs and INBOX state after the search. Messages relabelled, archived, or moved to legal/debt between search and fetch do not enter the sales queue, even if sender/subject/snippet appear harmless. Missing message label evidence or an absent/ambiguous HQ-Enquiries label fails closed before any batch is written. The owner-only queue only reports an aggregate label-mismatch count; no excluded content is saved. This is additional *forward-looking* isolation, not live consent/read proof, a guarantee against every type of sensitive content, or retrospective removal. Gmail connection stages advance only on actual recorded owner-initiated reads/sync commits.

### Gmail customer-enquiry separation (18 November 2026)

The business-inbox importer adds a fail-closed secondary exclusion for recognisable legal/debt/enforcement/court correspondence, even when such an email is accidentally included under `HQ-Enquiries`. Gmail query exclusions for `HQ-Legal` and `HQ-Debt` provide an additional owner-managed layer. Import results report only aggregate skipped counts, and the original email remains in Gmail for the legal/debt workflow. This is a **forward-looking sales-queue safeguard**, not a guarantee for every possible message or retroactive cleanup of prior queue rows. The readiness register must not advance Gmail authorised/read-tested/sync-tested stages merely because these filters exist. The owner must verify the business mailbox consent, current read, and persistence separately before the stages can advance.

### Canonical preview integrity before arithmetic (18 November 2026)

Fixture-only bank roll-forward, DNA batch summaries and explicit DNA-to-Monzo settlement comparisons now **revalidate entire canonical event tuples at the point of calculation**. They reject tampered amounts, duplicate provider IDs, mixed bank/merchant accounts, broken GBP/signed-pence consistency, altered proof flags and malformed or excessive events, even when a previous canonical-normalisation result was later mutated. The settlement candidate comparator uses the same strict validators to avoid divergence between preview pathways. Synthetic regression tests cover forged records and metadata.

This prevents misleading *arithmetic* based on altered intermediary previews; it does **not** authenticate exports, prove a complete bank statement, authorise provider access, verify receipts, change any owner record or reconcile job payments. Monzo and DNA remain offline, without independently verified source mapping or provider connection stages. Next: obtain genuine source schemas and account scope, then design approved encrypted owner-only staging, off-database audit anchoring and production restore drills before real data persistence.

**Integrity boundary:** A local SHA-256 canonical-event checksum detects accidental changes between normalisation and downstream comparisons, including otherwise valid changed amounts. It is **unkeyed and not a provider signature**; someone able to change both events and checksum can fabricate another apparently consistent preview. Independent authenticated source records and owner review are still mandatory.

### Independently comparable synthetic audit head (19 November 2026)

The offline classification fixture can now export an HMAC-protected digest of **all** review events and check it against a previously retained detached head. Regression tests demonstrate detection of a removed last revision and restoration of an intact but older SQLite database—situations not reliably detectable using only in-database event seals. There is **no automated trusted off-database anchor storage**, no production integration, no source authentication and no independent backup restoration; the caller must protect the key and preserve the prior anchor separately. The owner-only Monzo and DNA live readiness stages remain **not implemented/configured/authorised/read-tested/sync-tested/reconciled**. Next: verify genuine exports and design approved durable encrypted staging with separate tamper-resistant anchoring and restores before connecting anything to real money.


### Isolated payout review decision staging rehearsal (19 November 2026)

The fixture-only `hq_offline_settlement_review.py` now offers an explicit, HMAC-pseudonymised SQLite **review-decision** ledger for synthetic one-to-one DNA batch/Monzo bank candidates. It records initial candidate/investigate/rejected decisions and append-only correction revisions with replay-safe idempotency keys, optimistic concurrency, exact-pair isolation across merchant and bank scopes, and source-evidence fingerprints. It will not promote a mismatched amount to a candidate or let two proposed links claim the same bank entry or processor batch. Stored rows contain no raw references or amounts; the local test database **is not encrypted or approved for real data**. Read-back verification detects row tampering, sequence gaps, changed source evidence and duplicate claims. A separately retained detached HMAC head can detect deletion of the last event or an older database restore, but no external head retention or production restore drill has been implemented.

**No live connection status is advanced.** The module is not imported by Flask, does not have owner authentication or encrypted production storage, and does not parse genuine exports, connect to Monzo/DNA, assign jobs or verify bank cash. It is an isolated rehearsal, not a running reconciliation service. Exact owner-controlled blockers remain genuine export schemas/units/account scope, consent and source authenticity, secure owner-only production staging, approved retention and UK GDPR controls, independent anchoring, disaster recovery restore tests and verified bank settlement/job allocation. Neither a proposed candidate nor an approved review is trading revenue or a verified receipt. The next engineering step is evidence-grounded source-schema adapters and separately approved owner-scoped encrypted staging with restore/audit protections.

### Monzo classification integrity hardening (19 November 2026)

Manual owner-entered classification proposals in the existing, memory-only export preview now revalidate the **entire canonical Monzo evidence tuple** immediately before accepting a category. This closes a gap where a changed amount or timestamp that still appeared syntactically valid could bypass the downstream checksum guard. The classification helper now rejects changed digests, record order, missing canonical fields and forged receipt/balance/reconciliation flags. The change has synthetic regression coverage and makes no provider calls, persisted review writes or live balance updates.

Monzo remains **not implemented as a verified live ingestion adapter** and not configured, authorised, provider-read-tested, sync-tested or reconciled by this update. The checksum is unkeyed, not independently sourced and not proof of cleared cash. The exact owner-controlled blockers remain genuine export fields and currency units, account ownership, secure owner-only staging/backup and independent bank/processor reconciliation evidence.


### Internal workflows: owner-only non-provider readiness (19 November 2026)

The private `/hq/integrations` page and `/api/hq/integrations` now also expose a **separate internal workflow readiness** register from `hq_workflow_readiness.py`. The register covers maintenance packages/visits, routes, Retail/Trade/Maintenance customer accounts, public enquiries and booking requests, basket/checkout, finance command centre, approved job costing, notifications and passkeys/PWA updates.

Implementation in this second register means only that the named *partial code path* exists; unsupported end-to-end accounts/checkout/notification flows are explicitly incomplete. A declared persistent `HQ_DATA_DIR` is reported as a **path signal only**, not proof the volume is writable, encrypted, backed up or restorable. No runtime success timestamps or authorisation, synchronisation, account entitlement, received-payment evidence or financial reconciliation are inferred. Separate provider states above remain authoritative for external connections. The register does not inspect or create databases, invoke provider APIs, send messages, change bookings or calculate financial totals on GET.

Each workstream has an exact owner-only blocker and safe next action, even where the product code is partly available. Actual finance data, customer-account isolation, subscriptions/maintenance visits and confirmed checkout still require source verification, access-control tests, owner approval and independent reconciliation. Synthetic HTTP tests confirm owner-only API/page access and `no-store` responses.

### Split Google Sheets source observations (19 November 2026)

The pricing/jobs and Maintenance endpoints formerly wrote the **same** integration-health key (`sheets`). A successful maintenance workbook load could therefore be reported as a successful jobs/pricing read, or a maintenance failure could obscure a valid pricing read. This was not source-specific evidence.

New owner-initiated reads are recorded under **separate keys**: `pricing_sheets` for HQ Jobs/Pricing and `maintenance_sheets` for Maintenance; Finance retains its independent `finance` key. The private readiness page now displays Maintenance Sheets as a distinct read-only source with its own last success, failure, setup and mapping action. A successful maintenance read cannot advance pricing or finance, and none of the three is a persisted-sync or reconciliation test.

**Legacy evidence is intentionally not inherited.** Old `sheets` history cannot reliably identify which workbook was read. The existing database and records are not deleted or rewritten, but the pricing and maintenance read-tested stages restart as **Not yet** until the owner performs a fresh source-specific read. Configuration/consent still does not establish access or permissions; no live provider calls occur when opening the dashboard. No bank receipts, job allocations, calendar bookings or customer data are changed by this release.


### DNA-to-Monzo candidate chronology (19 November 2026; offline only)

The unconnected fixture comparator now separately counts proposed payout/bank matches whose bank posting occurs **before the latest included DNA batch event**. Such records cannot be labelled amount-equal candidates or accepted as `candidate` decisions by the standalone audited review ledger, even when the amounts match exactly. They can still be explicitly investigated or rejected. Tests cover mixed event times, the same UTC boundary, denied candidate decisions and safe idempotent investigation.

The guard does **not** identify which bank credit belongs to a merchant batch, authenticate timestamps or verify any settlement; timing differences and corrections must be reviewed against actual provider exports. Monzo and DNA remain **unimplemented as verified live ingestion adapters, unconfigured, unauthorised, not read-tested, not sync-tested and unreconciled**. Their owner-only blockers remain verified source schemas/units and ownership, an authorised read path, encrypted audited staging, backup restoration, independent bank reconciliation and approved job allocation. No live financial or customer records have changed.

### Saved OAuth grant versus read-confirmed permission (19 November 2026)

The owner-only readiness register now separates `consent_present` from `authorised`. Local encrypted Google grant presence is configuration/consent **evidence**, not verified provider permission. With a saved grant but no source-specific successful upstream read, the authorised stage reads **Grant present; permission unverified**. After a successful recorded read, the authorised stage explicitly says **Historically read-confirmed (not current proof)** and exposes `authorisation_verified_at` as the last source-specific success timestamp. Failed later reads remain visible as blockers, and old read evidence is not deleted. Non-OAuth token providers are not certified simply because an environment variable exists.

This change is status presentation and evidence semantics only: no OAuth refresh, remote API call, live permissions check, cash verification, data import or booking occurred. Fresh owner-initiated reads, source-specific schema mapping, financial reconciliation, independent bank evidence and verified backups remain outstanding. See the private `/hq/integrations` page; the public repository contains definitions/tests only, not real owner connection status.

### Health observation timestamp integrity (19 November 2026)

The owner-only provider readiness register now validates the stored upstream-read and persisted-sync success timestamps before accepting them as historical observations. Values must be timezone-aware, parseable ISO timestamps and not more than five minutes in the future relative to the server. Malformed, unzoned or future-dated metadata is **not** read/sync success: the corresponding stage remains unverified, the displayed success timestamp is suppressed and the owner-only blocker requests investigation of the persistent health history. A corrupt read timestamp cannot establish read-confirmed Google authorisation; a corrupt post-commit marker cannot establish a persisted sync. The older Integration Health summary also refuses to report an invalid read timestamp as a successful check.

This is a fail-closed validation of *local* observation metadata, not a cryptographic signature, upstream provider authentication, source completeness, current OAuth validity, verified revenue, or settlement proof. No integration status was advanced, no history was repaired automatically and no provider API was called on dashboard GET. Test fixtures cover invalid, future-dated, unzoned, recovered read and sync observations. Independent trusted evidence/backups and owner-initiated fresh provider reads remain necessary.

### Settlement audit history growth versus independent anchoring (19 November 2026)

The fixture-only DNA/Monzo review ledger can now compare an **older detached trusted HMAC head** with the corresponding prefix of a newer, append-only signed synthetic review history. `verify_anchor_prefix` accepts legitimately appended reviews and rejects anchored-prefix changes or a restored database shorter than the head; `verify_anchor` retains strict full-head equality. Tests cover normal corrections, input validation, earlier-event modification and older SQLite restoration.

This **does not implement independent external head retention, encrypted production evidence, real export authentication, provider consent, verified deposits, bank receipts or job reconciliation**. Someone who loses their only independently retained anchor cannot prove the original history was not rolled back. The owner-only Monzo/DNA readiness statuses remain unconfigured, unauthorised, not read/sync-tested and unreconciled. Next safe steps: independently validate genuine source headers, money units and merchant/account scope; approve secure owner-only encrypted staging and UK GDPR retention; establish independent protected anchors and successfully restore a production backup in isolation before enabling any live import.


### Detached-head check during offline settlement review writes (19 November 2026)

The fixture-only DNA-to-Monzo settlement ledger can now require an externally retained audit head on individual proposed review writes via `trusted_anchor`, comparing the independently held head against the checked ledger prefix **inside the SQLite write transaction**. This prevents the guarded path from silently appending after an older database has been restored while allowing valid append-only review history. Guarding is opt-in for the fixture; there is no trusted external anchor storage, encrypted production staging, actual Monzo/DNA ingestion, authenticated source mapping, bank evidence, owner UI activation or independent production restore verification. No provider `configured`, `authorised`, `read-tested`, `sync-tested` or `reconciled` state advances. Next safe action: determine and validate genuine source export headers and stable scope, then design mandatory owner-only anchored staging, encryption and tested independent restore before handling genuine customer transactions.

### Post-commit Gmail and Square observation transaction (19 November 2026)

Gmail enquiries, Square bookings and Square payments now record the *upstream read completed* and *local snapshot/import committed* health successes together in one atomic SQLite transaction after the existing provider adapter completes. A failed second observation insert rolls the first back instead of leaving a partial new success; the owner-only dashboard continues to show previous history and a separate last failure until a successful new observation is recorded. The API's existing owner-only and no-store controls remain unchanged, and other read-only Google Sheets, finance, Calendar and Maps evidence is unaffected. Provider access is still checked only on owner request.

This improves **consistency of locally recorded observations**, not live credentials, provider authorisation, source completeness, payment settlement, verified bank cash, revenue, customer booking confirmation or reconciliation. Historical read and sync rows are still historical, not independently authenticated provider events; the task does not erase or upgrade them. Monzo and DNA remain fixture-only until the owner supplies verified source mappings and approves separate encrypted/audited staging with backup restoration. The next safe action for Gmail/Square is an authorised owner-triggered refresh, once the relevant connection is actually configured and consented.


### Visible failure when committed-sync health evidence cannot be saved (19 November 2026)

The owner-triggered Gmail enquiry import, Square bookings refresh and Square payment-evidence refresh now **check the return value** of `record_committed_sync`. If the provider queue/snapshot has already committed but the separate integration-health observation transaction fails, the owner receives an explicit **HTTP 503** stating that the source records may have been stored but the read/sync evidence was **not recorded**. The Square payment page retains its read-only snapshot view with an owner-visible warning. The operation does not silently report a verified sync; tests simulate this failure without provider traffic.

This is a partial-success/error state, not an instruction to automatically replay an import, create bookings or mark payments received. Existing provider rows are not rolled back by a later health-recording failure; existing idempotent imports are preserved. An unreadable persistent health database can prevent an accurate readiness display until storage is repaired. No Monzo/DNA provider authorisation, bank evidence, revenue, job matching, settlement, encrypted financial staging or production backup restore was established. Next action: investigate the owner-only integration-health storage and perform a fresh owner-triggered read only once service/storage is verified.

### Fractional-second settlement review safety (19 November 2026)

The existing offline Monzo/DNA candidate comparison retains subsecond UTC timestamps in canonical evidence. An equal-pence DNA batch paired with a bank transaction occurring earlier **within the same second** is now flagged as a chronology conflict, rather than an eligible amount candidate. Reusing a stable provider event ID with a different fractional timestamp fails deduplication. A standalone SQLite reviewed decision likewise cannot label such an impossible chronology a candidate. This only strengthens **fixture-based implementation tests**: Monzo/DNA remain unconfigured, unauthorised, not read-tested, not sync-tested and unreconciled without genuine verified source data, bank/merchant permissions, owner-approved encrypted staging, independent audit heads and tested production restoration. No live statuses or finance figures have been advanced.

### Fail-closed Google read observation writes (19 November 2026)

Owner-triggered **Calendar availability**, **Maintenance Sheets**, **Finance Sheets** and **pricing/jobs Sheets** reads now distinguish a successful upstream read from successful recording of its source-specific Integration Health observation. `record_check()` reports whether the SQLite observation committed. When the provider read succeeds but its observation cannot be stored, the authenticated owner endpoint returns **HTTP 503** with a redacted integrity warning instead of an ordinary 200 or a false read-tested claim. Each workstream retains its own evidence key; no finance, customer or provider record is modified by this change. Synthetic regression tests simulate the failing observation database with mock provider responses. This does **not** establish live Google consent, workbook mappings, Calendar sync, job allocation, authenticated bank cash, a provider settlement or production backup restoration. The owner must repair persistent Integration Health storage and retry the relevant read to establish fresh source-specific evidence.

### Historical observation age is not live access (19 November 2026)

After seven days without a recorded successful source-specific read, the owner-only API sets `read_stale=true` and recommends repeating that workstream's existing manual read. Gmail and Square import/snapshot evidence has an independent `sync_stale` flag when its post-commit marker ages beyond seven days. Historical `read_tested`/`sync_tested` and last-success timestamps remain intact; the stage text labels stale success explicitly, and a more serious actual failure or invalid observation retains precedence. A fresh local success observation removes the advisory flag but **never proves current provider access by itself**, bank cash, revenue, allocation or reconciliation. No automatic provider API call, external message, booking, bank import or persistent-finance change is performed by the dashboard.

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


### Square read/sync source-location binding (20 November 2026)

Square booking and payment read/sync readiness now requires a local atomic observation bound by a keyed fingerprint to the exact currently configured Square location. Earlier unscoped observations cannot be assumed to belong to a changed location. The owner-only register withholds `read_tested`, `sync_tested`, and previous success dates for unmatched/legacy Square source observations; `source_scope_unverified` explains the blocker and the next safe action is an owner-initiated read-only refresh of that specific service. This does not erase the existing provider SQLite snapshots or historical health records, and it is not an automatic Square request on GET.

The source seal is stored in a new additive table in the integration-health database. The raw location ID and access token are not recorded there; `HQ_SESSION_SECRET` stability is necessary to compare historical seals. Scope binding is **local evidence**, not a current provider permission check or bank reconciliation. No Square provider is marked authorised or reconciled based on these synthetic checks. Production Square credentials, required scopes and the actual location remain owner-controlled blockers.


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

### Gmail page-list validation — 20 November 2026

Gmail's owner-initiated sales import now validates the bounded message-list envelope, unique message IDs and opaque continuation token before any local enquiry rows are committed. Missing `messages` denotes a legitimate empty page; malformed or duplicate list evidence rejects the page. Synthetic regression tests are not live Gmail read/sync or permission evidence. Current authorisation, mailbox coverage and sales/legal isolation must still be confirmed through a genuine owner-authorised enquiry import. No Monzo/DNA provider, settled cash, job payment or bank reconciliation status advances.

### 20 November 2026 — Square bookings: malformed page rejection

The read-only Square bookings adapter now validates every page before committing snapshots: maximum 100 items/page, unique bounded provider IDs across pages, exact configured location, safe version/status metadata and valid bounded, non-repeated pagination cursors. Invalid/empty cursor values no longer masquerade as the end of a successfully complete refresh. Any page failure leaves the existing SQLite booking snapshot intact and prevents a new recorded successful read/sync observation. Added fixture-based regressions; **no provider or customer record was accessed** as part of testing.

**Readiness unchanged:** implementation hardening is not Square configuration, consent, verified read, persisted live sync, appointment reconciliation or payment settlement. The owner still must configure a legitimate production read token and location, explicitly refresh, and independently verify canonical appointments before any approved outbound action. Monzo and DNA also remain offline/unverified without actual export provenance and verified bank evidence.


### Square payment source-pagination integrity (20 November 2026)

Square **payment** evidence follows the same fail-closed page contract as Square bookings: no more than the requested 100 payments per page; only an *absent* cursor marks completion, and empty, malformed, repeated or ambiguous cursors reject the entire refresh before a stored snapshot can be replaced. Synthetic tests also exercise a complete two-page window. Failed read integrity must never create a new successful provider-read/persisted-sync observation or be interpreted as zero receipts. Historical successful snapshots are preserved for the same verified configured location; existing location/access isolation still applies. This implementation safeguard does **not** establish Square authorisation, a successful live read/sync, exact job matching, a verified receipt, a fee/refund allocation or bank reconciliation. The next owner action remains configuring production Square access and explicitly refreshing provider evidence after separate credentials/scopes/location verification.

### Monzo/DNA owner export schema: ambiguous header protection (20 November 2026)

The owner-only, memory-only CSV schema inspector now rejects visually ambiguous column identities (case/Unicode compatibility equivalents), zero-width/bidirectional control characters and other hidden control characters **before displaying or mapping headers**. Exact distinct Unicode headings remain usable, including a pound symbol, but neither a column name nor a syntactically valid pence integer proves provider units, ownership or completeness. This prevents an operator from accidentally mapping one of two deceptively similar columns as the bank transaction identifier or monetary amount. Synthetic tests cover both header-only inspection and mapped/manual-classification previews. No actual Monzo/DNA sample, account permission, persistent ingestion or banking evidence was used; both providers remain unconfigured, unverified, unsynchronised and unreconciled. The next owner-controlled action is to verify genuine export headings, exact signed-pence units and account/merchant scopes; then design approved encrypted staging and independent settlement/audit/backup validation.

### Financial preview duplicate JSON safeguards (20 November 2026)

The owner-only memory-only CSV preview refuses conflicting repeated keys in text-encoded JSON column mappings and manual bank-classification proposals, rather than silently keeping a later value. Synthetic HTML and API route tests confirm that ambiguous instructions fail before canonical review and without persistent financial writes or exposing private rows. This is **offline preparation only**. Monzo and DNA remain not implemented as live provider ingestion adapters; actual source formats, authorisation, read/sync evidence, verified bank credits, approved job allocations and reconciliation remain unverified.


### Native JSON submission integrity (20 November 2026)

The private transaction export preview API now **rejects repeated member names in native JSON bodies, at every depth**, in addition to duplicate keys in string-encoded mapping/classification fields. A conflicting second `source`, `amount_pence` mapping or `customer_receipt_candidate` proposal cannot silently override the first instruction. The API enforces owner CSRF before parsing native JSON and rejects malformed or non-UTF-8 payloads. Synthetic route tests prove rejection and no persisted finance changes. Live Monzo/DNA implementation and all provider read/sync/reconciled states remain unchanged and unverified; actual source schemas and independent bank evidence are still required.

### Revision and retry integrity guard (20 November 2026)

The local-only Sheets outbox now rejects invalid, boolean, negative, overflow and **future** work revisions rather than advertising a revision which has not committed to HQ. A delayed call for an older saved revision is ignored; identical revision replays do not reset a failed item's retry history. When an actual newer HQ revision commits, its queue entry coalesces and its retry attempts/error state are reset for the new snapshot. These checks run inside the same local SQLite transaction as the approved internal HQ state change. Synthetic tests cover each case and no external Sheets call or writer is introduced. **Pending** does not mean authorised, linked, dispatched, read-back verified or reconciled.


### Sheets Jobs link preflight (20 November 2026; offline only)

The standalone, read-only hq_sheet_link_preflight helper now detects
mismatched workbook/tab identity, incomplete row-4-onward reference scans,
duplicate or formula-generated Job IDs, modified known input headings, and
occupied or formula-bearing candidate input cells before a proposed manual
J-reference linkage. Its synthetic regression suite asserts that historical
HQ-to-J mapping, baseline, owner approval, write authorisation, live sync and
reconciliation stay **false**. Unmapped formulas are left untouched.
No owner UI is connected to this helper yet; it makes no provider requests or
persisted changes. Google Sheets implemented/read-tested stages are **not**
advanced by offline preflight tests. Owner blockers remain a fresh authorised
workbook read, verified B:E/Cost Library mappings, unique historical link
review, formula-safe conflict policy and explicit scoped write consent.


### Owner-triggered Sheets Jobs link inspection (20 November 2026)

A new owner-only form at `/hq/sheets/job-link-preflight` now requests an explicit, CSRF-protected **read-only** scan of the existing Jobs tab via the already scoped Google Sheets OAuth grant. It passes raw GridData only inside the server to the existing formula-safe preflight. Its fixed response is a snapshot-only candidate verdict and next manual verification action; no customer/job cell values, monetary amounts or identifiers are returned. Success/failure is recorded under the separate `sheets_job_preflight` observation key (visible on Integration Health) without silently promoting pricing, finance, write sync, verified payments or reconciliation. Truncated/incomplete scans or schema conflicts block the candidate. No call is made on page load. Real owner access, provider response completeness, HQ historical matching, trusted baselines, separate editor consent and independent read-back are still outstanding. Synthetic test success is not evidence of any real owner-authorised source read.

### Owner Sheets inspection request integrity (20 November 2026)

The owner-only `POST /api/hq/sheets/job-link-preflight` now checks CSRF **before** decoding JSON, enforces a 512-byte body limit even without a declared Content-Length, requires valid UTF-8, and rejects duplicate JSON keys at any nesting depth. This prevents ambiguous owner-selected J references or row modes from being silently overwritten by a JSON parser before an authorised Google read. Rejections occur before touching Google, writing readiness observations or creating a link. Synthetic HTTP tests cover duplicate, nested duplicate, malformed, oversized and invalid-CSRF requests. An accepted request still produces a **read-only, ephemeral candidate** and cannot prove a historical HQ mapping, consent to write, a verified receipt or successful sync.

Current owner blockers are unchanged: actual owner-authorised workbook read and complete row coverage, verification of historical J references and sheet formulas, approved record mapping, concurrency-safe coordination, and separately explicit write authorisation. This change does not advance a provider's implemented/configured/authorised/read-tested/sync-tested/reconciled status; no spreadsheet cells or financial data are modified.


### Sheets reference casing collision guard (20 November 2026)

The fixture-tested, read-only Jobs row preflight no longer accepts a new uppercase J reference if the same historical reference appears on another row with different letter case. Existing-link proposals still require an exact owner-reviewed reference, including its case. This fixes a potential duplicate job ID allocation without saving job links or enabling a Sheets writer. This is **code validation only**, not a fresh Google read, mapping verification, authorisation, sync, receipt or reconciliation. Full-row coverage of real Google GridData remains a separate fail-closed requirement.


### Separate Jobs row-link inspection status (20 November 2026)

The existing owner-only Jobs inspection now has its own `sheets_job_preflight` readiness entry and timestamp in `/hq/integrations` and `/api/hq/integrations`. It reads only observations explicitly recorded by the owner-triggered, read-only `/hq/sheets/job-link-preflight` POST. Pricing (`pricing_sheets`), maintenance (`maintenance_sheets`) and finance (`finance`) cannot inherit that inspection's success; the reverse is also true. An invalid or conflicting candidate is recorded as a failed *inspection*, not a successful link, and the register retains the prior successful read timestamp separately from the latest error.

The inspection requires owner Google OAuth; a configured service account alone is insufficient. A successful inspection is a **historical, snapshot-only row/reference preflight**, not evidence of actual HQ identity matching, complete externally validated source coverage, ongoing permission, baseline safety, write access, a committed synchronisation or settlement. It remains **read-only** (`sync_applicable=false`, `sync_tested=false`, `reconciled=false`). Google's trailing blank GridData row behaviour has not been validated against the owner's genuine workbook; incomplete row coverage still blocks the inspection. No owner data, job refs or financial amounts are committed to the repository.

### Sheets mapped-heading minimum evidence (20 November 2026)

Owner-only sheets_job_preflight now rejects snapshots whose future writable
Jobs columns have absent, blank or whitespace-only headings, including B:E.
Fixture tests also reject formula and duplicate headings. This closes a
possible false-positive inspection against an altered or incomplete source
layout. The existing known heading subset is still checked for exact text;
**B:E heading meanings have not been independently verified**. A passing
inspection remains ephemeral and does not create an HQ-to-J link, prove full
GridData coverage or authorise a cell write. No provider configuration,
authorisation, read-tested, sync-tested, reconciled or verified-receipt stage
was advanced from code-only tests. Owner actions remain genuine workbook read,
complete row scan, semantic mapping, reviewed historical links and separate
write consent.


### Google Sheets trailing-row evidence update (20 November 2026)

The owner-only Jobs row-link preflight has a narrowly bounded *read-only*
secondary `FORMULA` Values API scan when GridData omits trailing rows.
It verifies the complete echoed A4:AJ grid range, compares raw job IDs across
reads, and will only infer missing *trailing reference rows* to be blank if
the independent Values response has no entries after the existing raw rows.
An owner-selected target outside the original raw GridData remains blocked.
This avoids accidentally treating a trimmed value response as evidence of
blank raw input cells. Concurrent edits can occur between reads, so no
write/read-back, permission, reference link, finance settlement or source-backed
reconciliation is established. Synthetic tests do not upgrade any provider's
configured, authorised, read-tested, sync-tested or reconciled status; the
live register remains owner-only. The next owner action remains a genuine
manual read with Sheets OAuth; actual historical HQ-to-J mappings and
conflict-safe write approval remain independent blockers.

### Sheets cross-read candidate-input consistency (20 November 2026)

When omitted trailing rows require two independent, read-only Google Sheets calls, the Jobs link inspector now rejects a selected row if any mapped input cell was blank in raw GridData but has become non-empty in the FORMULA-rendered follow-up. A changed customer field or formula cannot be overlooked merely because the Job ID stayed the same. Only the owner-selected row is compared, without returning its values. Existing numeric/date values may render differently, so this does not prove unchanged populated cells, an atomic read, a lock, or an approved remote baseline. Fixture tests establish this fail-closed behaviour, **not** live Google read success. No configuration, authorisation, sync, reconciled status, bank receipt or spreadsheet write state is advanced. Next action remains owner-initiated genuine Sheets inspection and individually approved historical HQ-to-J identity review.

### Sheets existing literal-value cross-read safeguard (20 November 2026)

The owner-only Jobs link inspection now rejects an existing literal mapped input that **changes or disappears** between the raw GridData read and the independent FORMULA-rendered trailing-row coverage read. Previously this secondary check caught new values in blank candidate cells, but not a changed customer/vehicle literal in an already populated input. The check compares exact string/formula representations only; numeric, date and boolean values can render differently and are **not** independently revalidated by this safeguard. No atomic read or write lock is provided. Regression tests use synthetic worksheet responses only. No actual workbook was read, no external cell was modified, no historical HQ-to-J identity or baseline was approved, and no read/sync/reconciled connection stage was advanced. Owner actions remain a genuine read-only inspection and individually reviewed source/job mapping; separate write consent and read-back are required before linking or sync.


### Typed Sheets input cross-read guard (20 November 2026)

The owner-only Jobs link inspector can compare previously occupied numeric
and boolean mapped fields through an additional independent,
unformatted **read-only** Google Sheets request when trailing GridData rows
need verification. An altered price, date serial or boolean fails closed;
currency/date formatting alone does not establish a change. Synthetic tests
cover stable values, mismatches, reference changes and failed provider reads.
This does **not** alter any implemented/configured/authorised/read-tested/
sync-tested/reconciled stage from synthetic evidence. No real owner workbook
read or historical HQ mapping is claimed, and no write, allocation or
financial settlement is activated. Full concurrency-safe write baselines,
genuine source access and owner approval remain blockers.
