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
