# HQ migration completion ledger — 24 November 2026

This is a conservative checkpoint, not a cutover authorisation. Main at
`ace1fa9e` passed CI (run 45380782396); Railway deployment
`61410fae-137b-41f5-9e58-78d6253a3ac2` reported SUCCESS with the existing
1024 MB /data volume. Native owner-browser acceptance remains unverified.
The workbook remains read-only; no historical canonical import is authorised.

| Domain | Implemented / deployed at checkpoint | Owner-tested | Historical migrated / reconciled | Authority |
|---|---|---|---|---|
| Customer / vehicle | Native creation and edits; staged historical identity proposals | Unverified | No / no | HQ new native records only; Sheets historical |
| Jobs / quotes | Native edits, costs, PDFs, staging, mappings, isolated rehearsal | Unverified | No / no | HQ new native work only; Sheets historical |
| Maintenance / routes | Native workspaces and packages; historical parity open | Unverified | No / no | Per-record; no cutover |
| Materials / products | Native cost and commerce workspaces, deposit review | Unverified | No / no | Per-record; no historical parity |
| Finance / transactions | Native workspace, receivables, inactive payment reviews | Unverified | No / no | Provider / Sheets evidence remains separate |
| Creditor / legal / affordability | Existing workbook and partial native functions; migration open | Unverified | No / no | Existing evidence retained |
| Public booking | Native request and reviewed quote pathways; prior synthetic coverage | Unverified | Not applicable | HQ request records only; no implied booking/payment |
| Recovery | On-volume private snapshot and isolated check deployed; encrypted export deployed (PR #259; 660f82b8; Railway 549c4982-cd2d-40ca-94d5-37cc25f7caf6 SUCCESS) | Unverified | Production snapshot not authorised or verified | No live restore |
| Provider integrations | Existing adapters; no new provider consent or cutover | Unverified | No / no | Providers for verified evidence only |

## Workbook inventory (48 tabs, read-only metadata)

Dependency groups; inventory does not imply verified mappings, exact row counts,
formula parity, or permission to import. Record source headings, cell types,
formula dependencies, identity keys, conflict counts and owner approvals in
separate bounded import manifests before each domain cutover.

- Customers / work (8): Customers; Jobs; Quick Quote Intake; Bookings Sync;
  Quote Breakdown; Quote Follow-ups; Customer Cost Explanation; Price Resolution.
- Cost / stock / staff (5): Cost Library; Product Stock; Job Cost Calibration;
  Employee Job Sheet; Staff Affordability.
- Maintenance / travel (5): Travel & Maintenance; Maintenance Clients;
  Maintenance Visits; Route Rules; Google Maps Setup.
- Finance (12): Cashflow & Debt; Monzo Raw Paste; Category Rules; CSV Import;
  Monzo Live Feed; Cash Engine; Premises Affordability; Creditor Affordability;
  Business Affordability; Director Pay Planner; Smart Pots Planner;
  Customer Debt Recovery.
- Legal / evidence (8): Legal Response Matrix; Creditor Response Generator;
  Asset & Enforcement Register; Creditor Casework; Business Risk Inbox;
  Customer Agreements; Legal Defence Centre; Contract Review Register.
- Operational configuration / QA (10): HQ Dashboard; AI Insights; Settings;
  Sheet1; Developer; QA Test Results; QA Audit Report; Formula Audit;
  Legal Evidence Queue; HQ Integrations Build.

## Recovery controls and export readiness

In HQ Historical Jobs Migration > select an existing staged job > Private
recovery snapshot, explicitly confirm the on-volume copy and choose **Save
private backup and check isolated restore**. No real copy was created by
development. After a verified copy exists, the proposed encrypted export
button takes only a public RSA key and explicitly confirmed owner download.
It does not send anything to a cloud provider. No chargeable resource is created.

On a trusted offline computer, create an encrypted private key (retain its
passphrase separately) and corresponding public key, for example:

```sh
umask 077
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -aes-256-cbc -out recovery-private.pem
openssl pkey -in recovery-private.pem -pubout -out recovery-public.pem
```

Paste ONLY the public PEM in HQ. Independently verify the intended key and
retain the downloaded encrypted file on storage controlled by the owner.
Never place the private key in Railway, GitHub, Sheets, or HQ. The envelope
uses AES-256-GCM with a fresh random key and nonce, and RSA-OAEP/SHA-256.
The header is authenticated. No application secret is used for recovery.
A lost private key cannot be reconstructed.

For isolated verification, on a trusted computer with Python 3.12 and the
repository's dependencies installed, run from a checked-out copy of the repo:

```python
from pathlib import Path
from getpass import getpass
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from hq_recovery_export import decrypt_for_isolated_verification

key = load_pem_private_key(Path("recovery-private.pem").read_bytes(),
                           password=getpass("Private key passphrase: ").encode())
data = decrypt_for_isolated_verification(
    Path("tommy-tints-hq-recovery.tthq").read_bytes(), key)
print("Authenticated SQLite copy passed isolated integrity checks.")
# No write or live restore. If recovery is separately authorised, use a
# new isolated destination and validate record parity before any cutover.
```

This covers only the canonical SQLite file and its contents at snapshot
time; other databases, attachments, Google workbook, provider configuration,
independent key custody and a complete application disaster-recovery drill
remain separate. Do not claim off-volume protection until the owner actually
retains and verifies an independent copy.

## First canonical import approval boundary

No executable import is approved by this checkpoint. Present a bounded
manifest with exact source row/J IDs, intended customer/vehicle/work IDs,
field-by-field before/after values, duplicates/conflicts (zero unresolved),
snapshot identifier and restoration result, totals, reversibility and
post-import reconciliation tests. The first safe slice should affect only
explicitly mapped, independently verified native draft records. Actual
cost aggregates, receipts, completed work, liabilities and payment status
must not be inferred from historical quotes. No row counts or conflict
counts have been established for a real import yet. The owner must approve
that exact manifest separately; recovery approval alone does not authorise
an import. No Sheets write or bidirectional sync.

Next implementation: versioned reversible execution of verified historical
links with immutable source provenance; in parallel native transaction
matching, splits and allocations; public customer acceptance/booking UI
verification without synthetic live bookings or communications.

## 24 November follow-up — bounded canonical price execution

PR #259 deployment SUCCESS was independently checked against main and the
existing 1024 MB /data volume. This change proposes actual execution for one
historical quoted-price field on an existing unpriced owner-native draft only.
It does not create customers/vehicles/jobs, merge records, move actual costs,
record receipts, or change authority for historical Jobs. Existing canonical
customer and vehicle relationships remain intact. Separate owner-document
identity review and consistent vehicle/service assessments, literal unique
registration, fresh read-only source comparisons and a verified recovery
snapshot containing the same draft and staged evidence are required.

In a singleton historical Jobs review, open its mapping editor, save the
eligible price proposal (other proposals must be preserved, not applied),
create the private recovery snapshot, select it and choose **Prepare exact
one-row import approval**. Review actual J reference, source row, HQ IDs,
price before/after, counts and snapshot. Only then explicitly confirm and
choose **Apply this one-row historical price import**. The exact manifest is
persisted and rechecked under the write transaction. **Reverse this imported
price** is a separate approved compensating revision, available only while
the draft is unchanged. History and original J reference are retained.
No production import count is established or authorised by this code change.
Deployment and owner acceptance must be recorded separately.

## 24 November follow-up — native transaction split classifications

PR #260 deployed SUCCESS on Railway at 13:06 UTC, commit 929792f4,
deployment 5debe867-0d0b-4487-8ea1-bceae21969c8, original /data volume
1024 MB. No owner-tested production import or recovery is established.
Native Finance Command Centre now proposes split classification editing for
existing owner-entered transactions, with exact signed pence conservation,
parent revision locking, immutable split history and withdrawal. The split
is a private overlay, not an extra cash event; no provider/Sheets records,
balance or receipt totals change. Owner acceptance and deployment of this
change, provider-backed import, matching, financial reporting parity and
historical reconciliation remain separate and unverified. No domain cutover.

## 24 November follow-up — classification reporting and review

PR #261 merged as 1dd11130 after successful CI run 45410089864 (625 Python,
6 JavaScript tests); Railway deployment 68bfeccf-f531-475e-ae72-8a79e65afc54
reported SUCCESS at 15:03 UTC. The split editor is deployed; owner-browser
acceptance and real reconciliation are not established.

This change adds an owner-only monthly classification view under Finance
Command Centre > Transactions & reconciliation > Monthly classification
totals and split review. It replaces the original category with the current
active split in category subtotals, counts each original amount once, separates
inflows from outflows and preserves unclassified amounts. The same view links
to the existing persistent split editor and withdrawal history. Invalid or
inconsistent split evidence and oversized reports are withheld rather than
presented as partial totals. Native manual entries only; no imported bank
evidence, no verified receipt, no income/profit or balance inference, no
combination with Sheets. No real records changed; provider matching, actual
bank settlement, customer/vehicle imports and historical parity remain open.
This proposal's deployment and owner acceptance remain unverified.

## 24 November follow-up — existing Monzo feed classification proposals

PR #262 merged as a282c99d after successful CI 45421229623 (127 root and
627 integration Python tests; 6 JS tests), Railway deployment
31c9333f-92b6-4fd4-8687-aa615fe87cb7 SUCCESS at 16:03 UTC, original /data
volume configured. Monthly manual classification view deployed; owner
acceptance remains unverified.

The read-only workbook Monzo Live Feed A7:I schema was checked: 4000
populated unique source IDs in the bounded connector read, numeric date/time,
GBP amounts with at most two decimals, three missing raw categories. S:U
contains existing transaction IDs, HQ override and category source; those
legacy overrides are NOT imported or superseded. New private owner review
under Finance Command Centre > Transactions & reconciliation > Monzo
transaction classification review stores only the original ID, source
fingerprint and immutable category revisions. Explicit owner confirmation
and fresh source read precede each save. A changed source or duplicate ID
blocks review. No cash, Sheets cells, verified receipts or canonical
financial records are created or changed. Selecting unclassified withdraws
an earlier proposal without deleting history. Existing manual transactions
and totals remain separate. Imported bank cash, split allocations, settlement,
provider authentication, actual reconciliation, owner acceptance and domain
cutover remain unverified. No real transaction was classified by development.


## 24 November follow-up — preserve existing transaction review evidence

PR #263 merged at ff1b49b9 after successful CI 45432981933 (127 root,
629 integration Python tests and 6 JS tests). Deployment b9681530 was
building at first inspection; owner acceptance remains unverified.

Read-only bounded source inspection found 4,000 distinct feed IDs and
1,152 distinct override IDs, all matched by ID in the observed feed.
The new owner form displays original category, existing Sheets override
and source, and independent HQ proposal. Overrides are joined by exact ID
rather than row position, never automatically mapped. Each new proposal
revision records the legacy evidence digest and category; old revisions
without context remain intact. Changed legacy evidence rejects stale form
submissions, and duplicate override IDs fail closed. No source writes or
canonical financial allocations. This is implemented pending CI, deploy
and owner acceptance; no domain becomes authoritative. Next: native
transaction-to-job/invoice/creditor matching and verified allocation,
bounded customer/vehicle migration and production customer UI acceptance.


## 24 November follow-up — invoice match candidates

PR #264 passed CI 45444339437 (127 root + 630 integration Python tests,
6 JavaScript tests), merged as 8507cc68 and Railway deployment
3f2a5c97-43f6-41df-8d29-5c8d6eeb302a reported SUCCESS at 18:03 UTC.
The concurrent PR #265 timer cockpit was preserved. The original /data
1024 MB volume was observed attached before merge. Owner acceptance and
real migration remain unverified.

Read-only workbook metadata still enumerates the same 48 tabs and the
Monzo Live Feed A7:U7 headings match the original ID and override columns.
No live record was changed. This change adds a private exact-ID worksheet
transaction to issued native invoice candidate editor. The whole positive
source amount can cover part or all of an invoice, subject to remaining
candidate capacity after owner-reported allocations and other active
candidates. No inference of receipt, bank verification or settlement.
Source and legacy evidence, invoice identity and revision, candidate
capacity and source review revision are checked again on save. History
is append-only; withdrawal remains available without Sheets access.
One active candidate per source ID, no duplicate cash events, no change
to receivable balances or actual payments. Existing invoice edit paths
remain unchanged; a candidate is not a financial reservation or an
authoritative balance, and must be revalidated before any future allocation.

Owner workflow: Finance Command Centre > Transactions & reconciliation >
Monzo transaction classification review > positive transaction >
Review invoice match / saved history > enter exact issued HQ invoice
reference > Review exact match candidate > explicit confirmation >
Save private invoice match candidate. Withdraw saved candidate retains
history. No production candidate was created by development.
This change is pending CI, deployment and owner acceptance. Verified bank
allocation, split-source settlement, creditor matching, historical customer
and vehicle imports, full formula parity and authenticated production
customer acceptance remain open. No domain cutover.


## 24 November follow-up — split classification of Monzo worksheet entries

PR #266 passed HQ tests (run 45455468232) and merged as aa9b29cb.
Its invoice candidates are inactive proposals, not verified receipts.
This new change adds two to five signed, exact-penny category lines to the
existing Monzo source-ID classification review. Split and single-category
edits share the same revision stream, so a stale form cannot overwrite a
different kind of classification. Source amount and legacy override hashes
are checked again before save; prior line history remains intact after a
replacement or withdrawal. Open Finance Command Centre > Transactions &
reconciliation > Monzo transaction classification review > Load current Monzo
feed for review > Edit split classification. No cash entries or source writes,
no aggregation into authoritative finance, no invoice allocation or bank
verification. New UI acceptance, deployment, historical reconciliation,
verified payment allocation, and reversible customer/vehicle imports remain
unverified. The existing customer booking pathways are preserved; no live
customer data was changed.


## 24 November follow-up — bounded invoice match amounts

PR #267 merged at e8b71954; GitHub Actions run 45466481601 passed and
Railway deployment 3c78c13a-5676-4fc1-9f31-6434516ba825 reported SUCCESS
at 19:06 UTC with the existing 1024 MB /data volume. Workbook metadata
still enumerates 48 tabs and the Monzo source/override headings match.
The new proposal extends the existing owner invoice review to accept an
explicit positive partial source amount in exact pennies, bounded by
both source amount and invoice candidate capacity. The exact preview
binds that amount and shows the unmatched portion separately. One
active candidate per source remains; withdraw and re-review to correct.
No source cash, receipt allocation, invoice balance or customer status
is changed by a candidate. Multi-invoice splitting and verified provider
allocations remain outstanding, as do owner acceptance and actual
historical import/reconciliation. This change is pending CI and deploy.


## 24 November follow-up — multiple invoice candidates per source

PR #268 merged at 57000609 after successful Actions run 45476711681;
Railway deployment was building on initial inspection. The next proposed
slice extends inactive Monzo matching to multiple invoices, with a global
source ceiling, per-invoice capacity, source-evidence binding, exact
preview revision, and independent withdrawals. Older candidates without
the new source digest remain visible and withdrawable; withdraw and
re-review before adding further candidates. No verified receipt, customer
payment status, cash entry or live historical import is implied.
Implementation and synthetic regression test are pending CI and deployment;
owner acceptance, canonical migration, and verified allocation remain open.


## 24 November follow-up — evidence-linked owner-reported allocations

PR #269 merged at 2eee922f after Actions run 45487206068 passed (127 + 635
Python tests and 9 JavaScript tests). The existing /data volume was confirmed
mounted; deployment initially building. This proposed change introduces an
owner-only two-step review and approval of a saved Monzo candidate against a
separate receipt/statement reference, recording an owner-reported allocation
and immutable source linkage atomically. It reserves already reported source
capacity, prevents exact evidence reuse by a different source or manual entry,
and permits reversal without changing the evidence. A worksheet and an owner
attestation are not independently verified bank/provider evidence. No customer
payment status, actual cash entry, verified outstanding balance, production
import or historical authority changes. Synthetic route tests are pending CI.
Owner browser acceptance and independently verified provider allocation remain
outstanding; no live receipt has been recorded in this development run.


## 24 November follow-up — exact Square card allocation evidence

PR #270 merged as 28243d77 after successful Actions run 45497499580
(127 root + 636 integration Python tests; 9 JS tests). Railway deployment
7c6bf92e-93d6-4570-a56f-e7236d8e2f84 reported SUCCESS on inspection.
No real allocation or historical import was authorised by development.
The original 48 workbook tab names were rechecked read-only.

The next proposed vertical slice links an existing owner-reported card
allocation to an authenticated, exact-ID Square payment check. Owner must
separately confirm invoice/customer identity; only a completed GBP payment
with explicitly zero refunds and exactly the allocation amount qualifies.
A second fresh provider lookup and invoice/allocation revision recheck
precede the explicit save. No new cash or allocation is created. Provider
ID uniqueness and immutable observation protect against duplicate recording;
reverse the original allocation to correct without destroying evidence.
A retained observation is historical and does not establish current
refund/chargeback status or bank settlement. The provider ID remains
reserved even after reversal pending a reviewed correction workflow.
Finance > invoice payment allocations > card entry > Check / view exact
Square payment evidence. No provider calls on page load. No customer-facing
paid status, verified balance or cash totals are published. Actual provider
consent, current evidence, production UI acceptance, settlement/refund
reconciliation and canonical historical migration remain open. Synthetic
regression, deployment and owner acceptance of this proposal are pending.


## 25 November — deployed Square evidence and changed-status/refund review

PR #271 merged at 35a358b6 after Actions run 45506909422 passed (127 root
and 638 integration Python tests, 9 JavaScript tests). Railway deployment
42f768b7-4885-4754-8bab-25a558190c3d SUCCESS at 00:04:35 UTC, startup
logs show Gunicorn listening and the original 1024 MB /data volume mounted.
Workbook read-only metadata still lists 48 tabs; the Monzo Live Feed
A7:U7 headings match the prior source and override IDs. No actual
provider call, historical import, payment allocation or Sheets write
was performed by development. Owner browser acceptance is not established.

This proposed slice adds an explicit owner-confirmed **Recheck Square refund /
status** action on an existing exact Square invoice observation. It preserves
the initial evidence and appends changed provider observations with their
allocation and invoice revisions. Unchanged responses do not duplicate
records. Unknown refund amounts are shown as unknown, not zero. Changed
location, conflicting timestamps or concurrent changes fail closed. No
new receipt, automatic refund booking, allocation change, customer status
or verified settlement claim is made. Chargeback and settlement
reconciliation, actual refund allocation, authenticated production UI
acceptance, and canonical historical customer/vehicle import remain open.
Synthetic tests are proposed; new PR CI and deployment remain pending.
No domain cutover or authority change.

Customer workflow: CI explicitly covered service request to HQ review,
owner conversion and customer-visible approval, combined quote appointment
handoff, customer acceptance without booking until owner confirms, product
deposit scope, maintenance pricing isolation and customer privacy.
Direct /book access from the development environment failed DNS resolution;
production browser acceptance remains unverified. Next customer slice:
expose only appropriately authorised canonical status, never internal
provider evidence or owner-reported allocations as verified payments.


## 25 November follow-up — full-refund correction (bounded)

PR #272 passed CI 45515251775 (127 root + 639 integration Python tests,
9 JavaScript tests), merged as 990c8902; Railway deployment
f80b517b-b12d-4645-b9d3-3d14915455e6 reported SUCCESS with original 1024 MB
/data volume still attached. No owner production acceptance is established.
Read-only workbook metadata still enumerates 48 tabs; Monzo A7:U7 headings
were rechecked. Customer request, quote conversion, accepted appointment,
combined basket, maintenance and account isolation synthetic tests passed.

This proposal adds a separately approved local reversal of an existing
owner-reported Square card allocation only when a retained recheck and fresh
exact provider response agree that the entire original GBP amount is refunded.
Finance > Invoice Payment Allocations > card entry > Check / view exact
Square payment evidence > Recheck Square refund / status > Review full refund
allocation correction > Approve full refund allocation reversal. The approval
digest binds invoice/ allocation revisions, original observation and latest
recheck; the reversal event and correction evidence are committed atomically.
The original amount, evidence and invoice face amount remain intact. The
payment reference stays reserved even after reversal; repeated exact approval
is a no-op. Partial/unknown refunds, changed evidence and non-issued invoices
are excluded. No refund is issued, no new cash entry or credit note is made,
and no independently verified bank balance or customer paid status is inferred.
This implementation awaits CI, deployment and owner acceptance. Historical
customer/vehicle migration, creditor reconciliation, formula parity and
off-volume recovery verification remain unverified; no domain authority changes.


## 25 November — cumulative partial Square refund corrections

PR #273 merged as d9a04cc8 after successful CI run 45523174012
(127 root and 640 integration Python tests, 9 JS tests); full-refund
synthetic route test passed. Railway deployment 2c1875f0-481f-4edc-8a6d-9cd82ffffb6c
SUCCESS at inspection. Existing /data volume 1024 MB
was observed attached. Workbook read-only metadata still lists 48 tabs and
Monzo A7:U7 headings match the original schema.

This proposed change allows a local owner-approved reduction of the same
reported card allocation after exact fresh cumulative partial-refund
evidence. Original gross evidence remains immutable; the amount used in
existing reported receivables calculations becomes the remaining amount.
Each incremental correction binds the exact prior amount, revision,
provider recheck and approval manifest; duplicate requests are idempotent.
Subsequent increases in cumulative refund reduce only the additional amount;
an eventual full refund reverses the remaining allocation. Unknown or
decreasing refund observations do not authorise restoration. No credit
note, refund sent, bank settlement, chargeback, verified outstanding
balance or customer-visible payment status is implied. Owner workflow:
Invoice payment allocations > Check/view exact Square payment evidence >
Recheck Square refund/status > Review partial refund allocation correction >
Approve partial refund allocation correction. Original invoice face amount
and other allocations are not changed. Synthetic route regression is
submitted; new CI, deployment and owner acceptance remain pending.

No production allocation, payment, customer, job, booking or Sheets data
was changed by development. No historical domain cutover. Canonical
customer/vehicle import, independent recovery acceptance, creditor matching,
settlement and formula parity remain outstanding. Existing customer request,
quote approval, canonical job handoff and isolation tests passed in PR #273;
direct production /book verification failed DNS resolution, and customer
payment display remains unverified.


## 25 November follow-up — partial refunds deployed; customer invoice publication

PR #274 passed CI 45530216604 (127 root + 641 integration Python tests,
9 JavaScript tests), merged as 3fad0a0d and Railway deployment
164b1310-3dc7-4254-9b2d-48e0218dbb3f reported SUCCESS at 02:58 UTC.
The existing /data 1024 MB volume was observed attached before merge.
Cumulative partial refund corrections are deployed; real owner acceptance,
verified bank settlement and historical parity remain unverified.

This proposed customer-facing slice adds a private owner form at Customers >
select customer > Portal access > Review and publish customer invoice
summaries. Only issued, non-archived invoices tied to approved work and the
same canonical customer are eligible. The owner must confirm exact invoice
and customer before publishing; withdrawal retains immutable history.
Authenticated customers see only the reference, issue/due dates and face
amount. Changes to invoice, work, allocations or retained Square observations
suppress the publication until explicit review. No receipt, paid status,
outstanding balance, internal memo, provider identifiers or bank settlement
is exposed; no external communication or payment action. Synthetic route
coverage is included; deployment and owner-browser acceptance are pending.
No actual publication was made by development.

Workbook metadata still enumerates the same 48 tabs. No historical import,
recovery snapshot or domain cutover authorised. Next: verified settlement
and payment status publication with explicit owner approval, reversible
historical customer/vehicle execution, creditor reconciliation and actual
browser acceptance. Existing customer request and booking paths preserved.


## 25 November — customer invoice publication deployed; optional historical card observations

PR #275 passed Actions run 45536906457 (642 integration Python tests,
9 JavaScript tests), merged as e5941497. Railway deployment
bdfd08cd-a843-42d9-904f-a79c88844bd8 reported SUCCESS at 04:03:44 UTC.
The original 1024 MB /data volume remains configured. Read-only workbook
metadata confirms all 48 tabs; Monzo A7:U7 source/override headings remain
unchanged. Synthetic customer service requests, canonical quote handoff,
appointment acceptance and owner confirmation, maintenance restrictions,
and customer invoice publication/isolation tests passed. Owner acceptance
and direct production browser verification are not established.

This proposed slice adds a separate explicit owner approval under Customers >
Portal access > Review and publish customer invoice summaries > Publish
invoice with card observations. Existing summary-only publications do not
gain payment data automatically. Only original exact completed GBP Square
observations on unchanged active card allocations with explicit zero refunds
and no subsequent changed recheck qualify. The customer sees only the amount
and historical observation timestamp. No current paid/unpaid status, balance,
settlement, provider IDs, internal notes or cash inference. The approval is
revision- and digest-bound, persisted atomically and idempotent. Changed
invoice/work/allocation/provider evidence hides the publication. Publish
invoice summary only removes payment observations; withdraw removes the
entire publication. Synthetic route tests submitted; new CI and deployment
remain unverified.

No actual customer publication, payment, import, recovery snapshot, booking,
or Sheets write was performed. Historical authority remains unchanged;
verified bank settlement/fees, creditor reconciliation, reversible historical
customer/vehicle execution and independent recovery acceptance remain open.


## 25 November — customer card evidence deployed; bounded canonical customer links

PR #276 passed CI 45544744084 (127 root + 643 integration Python tests,
9 JavaScript tests), merged as bf8d1c63. Railway deployment
a98a5a59-b142-4df1-8bb9-cda45ecd9fa0 reported SUCCESS at 05:05:33 UTC.
The original 1024 MB /data volume was observed attached before the merge.
The customer portal can now display separately approved historical Square
observations; current payment status, refunds, settlement and balance are
not inferred. The synthetic customer route test passed, including replay,
privacy, withdrawal to summary-only and stale evidence suppression.
Workbook metadata still enumerates 48 tabs; Customers A4:AF4 and Jobs
A4:CP4 retain 32 and 94 headings respectively. Owner browser acceptance
and actual publication remain unverified.

This proposal implements the first bounded canonical historical customer
provenance write: one existing Customers row to one existing HQ customer.
It neither creates a customer nor edits any customer profile, vehicle, job,
pricing, or workbook. Owner workflow: Historical Customers staging > select
an independently document-reviewed candidate > Review & save three source
fields (name and reviewed tier agree; postcode explicitly not mapped) >
create a private recovery snapshot AFTER the review > Prepare approved
historical link > select snapshot > Prepare exact link review > explicitly
approve manifest > Approve and save historical customer link. The manifest
records exact coordinates, HQ identity, source/native keyed fingerprints,
review/tier revisions, snapshot ID and counts; source and native evidence
are rechecked before transactional save. Active source-row and canonical
customer uniqueness constraints prevent collisions. A candidate is single-use,
even after reversal. No automatic source lookup or future synchronisation is
authorised by this provenance link. The saved record is reversible via the
approved-links history, including without live Sheets access; a linked
proposal cannot be withdrawn until the link is separately reversed.
Synthetic route regression submitted; CI and deployment pending. No actual
production snapshot, link, import or customer publication was created.

Historical customer fields, new customer/vehicle creation, job linking,
financial and legal reconciliation, and owner-tested acceptance remain
outstanding. No domain authority changes. Next bounded migration slice:
verified vehicle/job relationships and canonical historical field import
with a separately approved manifest; continue settlement/fees and creditor
reconciliation in parallel.
