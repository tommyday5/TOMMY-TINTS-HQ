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


## 25 November — canonical customer link deployed; bounded postcode field execution

PR #277 passed CI 45551310011 (127 root + 644 integration Python tests,
9 JavaScript tests), including the exact approval/snapshot/stale-source/reversal
route test, and merged as cf5baff4. Railway deployment
b085cfe2-d7ea-4a28-91b5-3fdafcbd5995 reported SUCCESS at 05:59:42 UTC.
Original 1024 MB /data volume was observed attached before merge. Workbook
metadata still lists 48 tabs and Customers A4:AF4 confirms 32 headings;
Customer, Type, Postcode are the first three. No owner acceptance or
actual historical link was established during development.

Proposed next slice: from approved historical customer links, choose
Review bounded historical postcode import. Requires an existing active
document-reviewed link, unchanged identity/source/tier review, literal
source postcode agreeing in formula and computed reads, never-edited native
postcode (revision zero), and isolated restore verification of a snapshot
containing that exact active link. Exact owner approval writes one native
postcode plus native revision event and import provenance atomically.
No other customer fields are overwritten. No address verification, routing,
tier, customer-visible profile, job, authority or workbook changes.
Reverse postcode import clears only an unchanged imported revision;
later edits fail closed. Active postcode imports block reversal of the
parent identity link. Synthetic route tests submitted; new CI, deployment,
owner acceptance and actual imports remain pending.

Customer app regression evidence from PR #277 includes service submission,
canonical quote conversion, appointment acceptance/confirmation, combined
baskets, invoice publication and account isolation. No new public routes or
production customer data changes in this slice. Authenticated production
browser acceptance remains unverified. Remaining: missing customer creation,
vehicles/jobs, batch import, off-volume restore acceptance, finance settlement
and creditor/affordability parity. No domain authority cutover.


## 25 November — postcode import deployed; missing customer identity batches

PR #278 passed CI 45557495216 (127 root + 645 integration Python tests,
9 JavaScript tests), including the bounded postcode import and guarded
reversal route. Merged as bf5ecd18. Railway deployment
bf4ba855-2712-4027-84f2-a2cf238be8c3 reported SUCCESS at 07:04:36 UTC;
the original 1024 MB /data volume was observed attached before merge.
PR #258 remains merged. Workbook metadata confirmed 48 tabs and
Customers A4:AF4 confirmed the 32 headings. No actual historical import,
snapshot or owner browser acceptance was established in this run.

Proposed next slice: Historical Customers > Create missing historical
customers (reviewed batch). Select one to five source row numbers and a
private recovery snapshot containing the current native customer directory.
Exact manifest identifies source coordinates, fresh formula/computed
fingerprints, proposed canonical IDs, names, counts, and zero detected
collisions. Explicit owner independent identity/alias review is still
required. Apply rechecks source, directory, and snapshot and atomically
creates canonical names only, with blank contacts, no reviewed tier,
and immutable provenance. No pricing, portal accounts, vehicles, jobs,
payments or Sheets changes. A replay cannot duplicate records. Reverse
archives untouched identities only; later edits/dependent records block
the whole reversal. Archive keeps IDs and provenance and prevents reimport
of the same source row; manual resolution is required for changes.
Synthetic owner HTTP route tests submitted; CI, deployment, actual import
and owner acceptance remain pending.

Customer app: PR #278 CI logs confirmed browser service requests and
owner quote conversion, appointment acceptance/confirmation, combined
baskets, maintenance isolation and customer profile restrictions. No
public routes changed in the creation slice. Production owner/customer
browser acceptance remains unverified. Next: import verified vehicle
and J-series work relationships, extend historical field mappings and
batch reconciliation, and finish bank settlement/creditor/affordability
flows. No historical domain is authoritative.


## 25 November — historical customer batch creation deployed; optional native postcodes

PR #279 passed GitHub Actions 45565496328, including 646 integration
Python tests, dependency/syntax checks and the app-update JavaScript check.
It merged as 0398015e and Railway deployment
b9ed4b19-2f5e-4f61-8bf8-561d7da24253 reported SUCCESS at 08:05:20 UTC,
with the existing 1024 MB /data volume attached. Read-only metadata
confirms 48 workbook tabs, Customers 32 and Jobs 94 column headings.
Synthetic customer service submission, owner quote conversion, appointment
offer, customer vehicle addition, maintenance isolation and product order
tests passed. No actual historical import or owner browser acceptance
established.

This proposed slice extends Historical Customer Migration > Create missing
historical customers (reviewed batch) > Also import valid literal postcodes.
The checkbox is optional, default off. An exact one-to-five row manifest
shows names, prospective IDs and each selected postcode or blank, with
counts. Both source readings must agree; malformed/formula-derived postcodes
block the entire selected batch. The postcode is saved in the existing
native revision/event tables in the same transaction as customer creation,
without overwriting any field or granting pricing/portal rights. The
migration audit stores a keyed postcode fingerprint rather than duplicating
raw postcodes. Replay binds the option as well as the batch and approval.
Archive reversal retains original data and history; it rejects any later
postcode edit, dependent record or other native change. Existing names-only
batch history remains supported. Synthetic tests submitted; new CI,
deployment, owner acceptance and actual migration remain unverified.

No actual customer, job, payment, booking, snapshot or workbook change
made. Customer portal behaviour unchanged. Historical vehicle/J-series
job creation and relationship mapping, finance settlement and creditor
reconciliation, off-volume recovery verification, and domain-level
reconciliation/authority approval remain open. No domain cutover.


## 25 November — combined customer/postcode creation deployed; vehicle provenance

PR #280 passed Actions run 45573400209 (648 integration Python tests and
9 JavaScript tests observed in logs), merged as 76e8ec6b. Railway deployment
006df137-9312-4901-ab1d-9dd22bb05eda reported SUCCESS at 09:00:39 UTC.
Original 1024 MB /data volume was observed attached before merge; PR #258
remains merged. Read-only workbook metadata lists 48 tabs and Jobs 94
headings. Among 60 rows with syntactically valid J references, 52 have
nonblank Vehicle/Reg and only one is a modern UK plate-only string;
8 are blank. This is source inventory, not identity verification.

Proposed owner workflow: Historical Customers > Review historical vehicle
relationships. Select one original J reference and Jobs row, a Customers
row with active approved/created provenance, the same canonical customer
and their existing vehicle, and a recovery snapshot containing the current
identity. The exact preview shows source text and native vehicle side by side.
Literal modern plate-only strings must match; mixed descriptions require
independent documentary review. Fresh source and native fingerprints,
snapshot, owner approval, row and reference uniqueness, and replay are checked.
Saved provenance is reversible without Sheets access; active links block
parent approved-customer link reversal. Imported-customer archival remains
conservative where dependent history exists. No new vehicle or work record,
no field overwrite, no customer portal access or pricing change. Synthetic
route regression submitted; CI/deployment and owner acceptance pending.

PR #280 CI confirms combined basket, service submission, quote conversion,
appointment acceptance and owner confirmation, native vehicle isolation and
customer portal isolation. No production owner/customer browser acceptance
or actual historical import established. Next: owner-approved missing
vehicle creation and original J-series work creation from verified
relationships, settlement/fees and creditor reconciliation. No authority
change or workbook write.


## 25 November — historical vehicle relationships deployed; reviewed missing vehicle creation

PR #281 passed Actions 45581582307 (127 root + 649 integration Python
tests, 9 JavaScript tests), merged as 4f0b6f6b. Railway deployment
fc349bca-3035-41c2-9590-6bedc328c6c6 reported SUCCESS at 10:06:06 UTC;
startup logs show volume mount and gunicorn worker boot. Existing 1024 MB
/data volume ID unchanged. PR #258 remains merged. Workbook read-only
metadata still lists 48 tabs; Jobs and Customers retain 94 and 32 headings.
No production record approval, recovery snapshot or migration execution
was performed.

Proposed next slice: Historical vehicle relationships > Create a missing
reviewed historical vehicle. Enter exact source coordinates, existing
approved customer identity, document-reviewed make/model and optional year
and registration, and a recovery snapshot of the current directory.
Exact approval rechecks formula/computed source, parent provenance,
registration collisions, ambiguous same-customer vehicle descriptions,
snapshot, native directory and reviewed fields. A native canonical vehicle
and keyed provenance are saved together. No guessing from mixed free text,
job or payment creation, source edits or new portal entitlements; owner
must explicitly approve possible visibility in an existing linked account.
Replay is idempotent. Explicit rollback removes only the newly created,
unchanged vehicle when no dependent references or history exist, retaining
its separate creation provenance; later edits or dependencies block rollback.
A subsequent J-row relationship uses the same vehicle ID and requires
separate exact approval. Parent provenance cannot be reversed while a
historical creation is active. Synthetic owner-route regression submitted;
CI, deployment, owner acceptance and actual imports remain pending.

PR #281 regression logs confirm combined baskets, canonical quote conversion,
service request to HQ review, customer account isolation, appointment acceptance
and owner confirmation, maintenance hidden pricing and portal vehicle request
to canonical owner creation and reload. No public routes changed. Direct
production HTTP probes for /book and the owner migration route failed DNS
resolution from this environment; authenticated production acceptance unverified.
Next: original J-series native job creation and historical price mapping,
settlement/fees and creditor reconciliation, independent recovery verification.
No domain authority changes.


## 25 November — reviewed vehicle creation deployed; first native historical quote draft

PR #282 passed Actions 45592021335 (650 integration Python tests and 9 JS
tests observed in logs), merged as 13afac54. Railway deployment
19fc8c10-1695-427a-bfe2-5252bb5bb6b0 reports SUCCESS at 10:58:25 UTC.
Existing 1024 MB /data volume was attached before merge. Read-only
workbook inventory confirms 48 tabs and Jobs 94 headings; among 60 J rows,
10 have literal Quoted status, 8 literal numeric prices and 2 missing
prices (source counts, not approved eligible records). Thirty J rows have
additional service descriptions, so automatic category inference is unsafe.

Proposed owner flow: Historical Customers > Historical vehicle relationships >
Create a missing historical quote. Open page before making fresh private
recovery snapshot, select an active reviewed vehicle link and explicitly
review a native service category against all original literal descriptions.
Only Quoted rows with unchanged source/customer/vehicle evidence qualify.
Exact numeric quoted price is copied in pence or left missing; the original
J reference is retained in a permanent provenance record, draft notes and a
standard inactive review candidate. One native quote draft with canonical
customer/vehicle and historical work tier, no entitlements or portal approval,
no booking/payment/invoice/actual cost or Sheets sync link. Duplicate source
history and any existing work for this vehicle block automatic creation.
Atomic approval, keyed evidence, idempotency and guarded archive/withdraw
rollback preserve the original draft and audit; dependencies or edits block
rollback. Reversed drafts cannot be restored via ordinary archive controls.
Synthetic route tests submitted; CI, deployment and owner acceptance pending.

Customer app: PR #282 CI confirms public service request -> canonical owner
approved portal, appointment acceptance/confirmation, product and maintenance
isolation, and vehicle request/create/reload. No new public controls in this
slice. Actual authenticated production browser acceptance remains unverified.
No live record or Sheets changes authorised; paid/booked/completed historical
rows, actual costs, multiple-work duplicate reconciliation, settlement,
creditor/affordability and domain authority remain outstanding.


## 25 November — historical quote creation deployed; repeat vehicle work and recovery capacity

PR #283 passed Actions 45600336380 (651 integration Python tests and 9 JS
tests observed, including the synthetic creation/reversal route and public
booking/account isolation tests). Merged as 086ceb9d. Railway deployment
05595d44-bf75-4393-8e83-650486d6adfa reported SUCCESS at 12:03:24 UTC; the
original 1024 MB /data volume remained attached. PR #258 remains merged.
No production customer record, job, payment, booking or snapshot was created.

Fresh read-only metadata confirms 48 workbook tabs; Jobs has 94 headings
and Customers 32. The bounded Jobs A4:AZ1000 inventory now returns 76
J-shaped rows and 75 distinct case-insensitive references (one duplicated
reference across two Enquiry rows). Source status counts: 27 Paid,
14 Declined, 12 Quoted, 4 Booked, 3 Completed, 8 Enquiry, 8 Awaiting Payment.
Of the 12 Quoted rows, 10 have agreeing numeric literal prices and 2
have missing prices. Counts are inventory, not approved migration eligibility.
Resolve the duplicate through an independent identity review; no source
rewrite or automatic merge is authorised.

Proposed changes allow distinct historical quotes on a vehicle already
containing separately mapped, unchanged historical drafts. Exact approval
lists earlier J references, checks their source and canonical provenance,
and refuses unmapped work, changes, or archive/withdrawal conflicts.
Snapshot parity now binds creation and vehicle-link histories and archive
state. Native owner work search accepts an exact original imported J reference
case-insensitively, and the native editor shows this immutable provenance
independently of mutable notes. No payment or booking status is promoted.

The previous three-snapshot cap was a practical blocker to successive
customer/vehicle/link/work approvals. Proposed recovery controls allow up to
64 private snapshots of at most 16 MiB each, with a 256 MiB aggregate
backup budget and a 128 MiB minimum free-space reserve on the existing
volume (worst-case copy space reserved before each new snapshot).
No automatic deletion, new storage, external transfer, or live restore.
Independent encrypted off-volume recovery and longer-term approved
retention/rotation remain outstanding. Synthetic route tests submitted;
new CI, deployment and owner acceptance pending.

Customer app: CI confirms public service request through canonical
owner approval, portal account isolation and owner booking confirmation.
No public routes changed in this slice. Direct authenticated production
acceptance and actual migration are not established. Next: status-safe
historical completed/booked/paid work migration without implied receipts,
finance settlement and creditor reconciliation. No authority cutover.


## 25 November — repeat-work deployment verified; historical job evidence drafts

PR #284 Actions 45610596230 reports a successful tests job, including
dependency checks, Python syntax, integration/regression and JS checks.
Railway deployment 3c917787-88ad-4394-9369-997af70d5268 reported SUCCESS at
12:07:48 UTC on main e1965516. Environment inventory confirms the original
1024 MB volume 540528d1-3953-4737-9d92-3c861575c5d4 mounted at /data, no
staged changes. Read-only workbook metadata still lists 48 tabs; Jobs
A4:CP4 lists 94 headings. Fresh bounded formula inventory: 76
J-series rows; source status counts {"Paid":27,"Declined":14,"Quoted":12,"Booked":4,"Completed":3,"Enquiry":8,"Awaiting Payment":8}. Source statuses
are not proof of appointments, completion, invoices or receipt.

Proposed owner flow extends Historical Customers > Historical vehicle
relationships > Create missing historical work. Reviewed literal Booked,
Completed, Paid and Awaiting Payment rows can now be created as native
editable job review drafts with immutable source status and original quoted
price in the exact approval/provenance record. They remain draft, with
native price unset, planned deposit zero, no preferred appointment and no
external side effects. The owner editor displays the source status and
price separately; core status/financial promotion is refused pending
independent reconciliation. Quoted row behavior remains unchanged.
Duplicate/source/recovery checks, canonical identities, candidate,
idempotency, and guarded archive reversal are reused. Missing price stays
unknown. Synthetic route coverage added for all four source statuses,
stale evidence, replays, privacy, unchanged financial aggregates and
reversal. CI and new deployment pending; no owner acceptance or live
import performed. The source price is preserved as evidence, not yet a
reconciled native job amount. Actual historical lifecycle promotion,
receipts, actual costs and bank settlement/creditor reconciliation remain
outstanding.

Customer portal routes are unchanged. Existing CI passed; direct owner
and customer browser acceptance and independent off-volume recovery
remain unverified. No authority cutover, production snapshot, live record
change or workbook write in this iteration.


## 25 November — historical job evidence deployed; remaining source statuses

PR #285 passed Actions run 45619121694 (127 root + 653 integration Python
tests, 9 JavaScript tests). Merged as 1c7fefde. Railway deployment
c1c38412-4044-4b57-8168-ff8b603ad940 reported SUCCESS at 14:05:17 UTC;
gunicorn startup and original 1024 MB /data volume
540528d1-3953-4737-9d92-3c861575c5d4 verified. One running replica,
no pending changes or service issues. No owner acceptance or production
historical import executed.

Fresh read-only workbook metadata confirms 48 tabs. Jobs status inventory
still contains 76 J-shaped rows: Paid 27, Declined 14, Quoted 12, Booked 4,
Completed 3, Enquiry 8, Awaiting Payment 8. Among the 22 Declined/Enquiry
rows, four have missing quoted price and three missing vehicle information.
These are source inventory counts, not import eligibility. Previously
identified duplicate J0057 remains a separate review requirement.

Proposed slice extends the same exact owner-approved historical creation
workflow to Enquiry and Declined. Both become native quote review drafts
with null native price, no appointments, payments, customer publication
or active operational status, and separately preserved source status/price.
Core status and financial promotion guards remain in force. All seven
observed source statuses now have draft representation in code, subject
to valid source, identity, vehicle, service, duplicate and snapshot checks.
Synthetic integration scenario expanded to cover both statuses, replay,
owner visibility, unchanged financial aggregates and guarded reversal.
CI and deployment of this slice pending. No actual import or source
mutation, recovery snapshot, customer publication or authority cutover.

PR #285 CI independently confirms customer service browser submission to
owner review, combined quote conversion, appointment acceptance/owner
confirmation, maintenance pricing privacy and customer isolation.
No public routes changed. Authenticated production browser acceptance
not established. Next: independent off-volume recovery and first bounded
owner-approved import, actual historical cost and status reconciliation,
settlement/fee and creditor/affordability editing. Do not equate supported
source statuses with migrated or reconciled records.


## 25 November — all historical statuses deployed; editable creditor cases

PR #286 full CI run 45630385704 completed successfully, including historical
nonquote evidence and customer isolation/quote/booking tests. Merged as
e636cf8c; Railway deployment fd52c746-3a9f-4af0-9494-9eda178c59b4 reports
SUCCESS; original 1024 MB /data volume remains attached. All seven historical
status categories now have draft representation, not actual migration.
Fresh read-only workbook metadata: 48 tabs. Jobs header confirms distinct
Actual Hours, Actual Materials £, Actual Consumables £ and Actual Extra
Costs £; no actual cost reconciliation or payment promotion in this slice.

This proposal introduces an editable native private creditor case register
in Finance Command Centre > Creditor cases & affordability inputs. The
Cashflow & Debt row-4 schema confirms separate Creditor, Balance, Current
Monthly Payment, Proposed / Agreed Monthly Payment, Committed Monthly
Payment, Liability Scope, Include in Affordability?, Account / Reference,
Review Date, Last Confirmed, Evidence / Contact Notes and Next Action.
Native owner-entered fields are intentionally not a historical import or
formula parity claim. Unknown amounts remain null; explicit zero is zero.
Claimed balances and proposed/committed amounts remain separate. Scope,
status, affordability inclusion, evidence reference and notes are editable.
Case ID, exact pence, revisions, durable event snapshots, duplicate guard,
CSRF, owner-only access, replay guard and reversible archive/restore are
implemented. No communication, payment, admission, hold confirmation,
bank balance, commitment totals or affordability capacity inferred.
Synthetic tests added; deployment and owner acceptance pending. Source
data and existing provider links remain untouched. No creditor record
was created in production, no production snapshot or import approved,
and no authority cutover.

Next: verify CI/deploy, map Creditor Casework and Creditor Affordability
fields and formula dependencies, reconcile actual creditor identities and
independent payment evidence before aggregating or deriving capacity.
Continue historical cost reconciliation and bank settlement matching.
Customer portal unchanged; PR #286 CI reverified isolated request-to-HQ,
quote approval and appointment acceptance/confirmation. Authenticated
production browser acceptance remains unverified.


## 25 November — creditor register live; native affordability scenario

PR #287 deployed as f676da54; Railway deployment
a73a7c2a-9434-41da-b0b9-cf2ce96d4a83 reports SUCCESS, original 1024 MB
/data volume 540528d1-3953-4737-9d92-3c861575c5d4 attached, no staged
changes. Owner acceptance and historical creditor migration remain
outstanding. Fresh workbook metadata confirms all 48 tabs.

PR #288 implements a private editable monthly scenario at Finance
Command Centre > Affordability calculator, linked from creditor cases.
Read-only formula inspection reconfirmed Cash Engine E9, H9, E10, H10,
B11, E11, H11 and Creditor Affordability B14:B16. The native engine
models the arithmetic subset on explicit owner-entered monthly inputs,
with exact pence, conservative growth switch, critical director support
guard, reserve rebuild and protected creditor contingency. Full workbook
parity (including rolling transaction classification, date window,
settings, all registered commitments and actual data) is NOT established.
No automatic integration with verified bank totals or existing commitments;
current case comparison reports known partial subtotals and unknowns
separately. Capacity is an illustrative shared pool, not verified advice.

Implemented versioned persistence, owner auth, CSRF, input validation,
idempotent replay, stale revision rejection, no-store, revision history
and responsive server-rendered form. Synthetic integration tests cover
save/reload, edit, history, duplicate operation, conflicts, validation,
case comparison and public portal exclusion; CI/deployment pending for
the full UI revision. No customer-facing route modified. Production
browser acceptance, off-volume recovery and actual historical import
remain outstanding. No live finance, customer, debt, booking, Sheets or
payment records changed; no authority cutover.

Next: verify full UI CI and deployment; actual historical cost parity,
settlement/fee matching and approved bounded import, alongside independent
customer workflow acceptance. Preserve provider connections.


## 25 November — affordability live; single-payment settlement evidence

PR #288 final-head bbc2bdc3 CI run 45660486648 passed (127 root +
661 integration Python tests, 9 JavaScript tests). Merged as d555bfa2;
Railway deployment b0d8bb34-4c83-463f-b114-7b417f9d969a SUCCESS at
18:02:52 UTC. Owner acceptance and full workbook parity remain unverified.
Read-only metadata reconfirmed 48 tabs. Original persistent volume was
attached before merge. Native scenario and creditor register are planning
inputs, not reconciled actuals; no production historical migration approved.

Next proposed slice: owner-only single Square payment settlement review,
linked from the retained Square observation and Finance Command Centre.
Explicit exact source ID, independently documented fee and payout reference,
gross-minus-fee equality in pence, unchanged invoice/allocation revision,
retained completed zero-refund Square observation, fresh Monzo worksheet
read and separate approval. Private append-only proposal and withdrawal
history; stale source and duplicate active proposal checks. No automatic
expense, bank entry, invoice allocation, cash receipt, or verified settlement.
Cross-workflow match conflicts checked when creating, but no financial
reservation is created; revalidation required before any posting.
Batched payouts, refunds and independent bank/provider payout verification
remain outstanding. Synthetic route test added; CI and deployment pending.

Final-head PR #288 logs also confirm customer-to-HQ service review, owner
quote conversion, appointment acceptance and owner confirmation, maintenance
privacy and account isolation. No customer-facing route changed. Owner
production acceptance and historical authority cutover remain unverified.
No live finance, job, customer, booking, payment, Sheets or recovery records
changed by development. Next: test and deploy proposal, independently
verified payout batches and fees, historical actual-cost reconciliation,
and exact owner-approved first migration manifest.


## 25 November — single settlement live; combined payout proposal

PR #289 final-head 2598877679fb40ace370df2ccce852e6ae09a7c0 Actions run
45680511028 reports success. Merged as d59c7727; Railway deployment
798d4798-1d17-4d75-976e-7c27d8637b1b reports SUCCESS at 18:59:32 UTC.
Original 1024 MB /data volume 540528d1-3953-4737-9d92-3c861575c5d4
is attached; no staged changes. Read-only workbook metadata still lists
48 tabs. Owner acceptance and historical cutover remain unverified.

Proposed new owner interface: Finance Command Centre > Settlement and fee
review > Review combined Square payouts. Accept 2–20 distinct existing
Square allocation IDs, one exact Monzo worksheet deposit ID, one documented
total fee and independent statement reference. Each payment retains its
invoice/allocation revisions, original provider observation and latest
recheck ID. Only completed GBP payments with explicit zero refunds qualify;
gross less documented fee must equal the positive deposit in exact pence.
Fresh source read, exact preview, explicit approval, atomic append-only
proposal and whole-batch withdrawal. Reject duplicate members and overlapping
active single/batch proposals. The fee is not arbitrarily apportioned across
jobs. No bank settlement or payout independently verified, no financial
reservation across all other workflows, no fee expense or new receipt
posted. Revalidate before future posting. Synthetic HTTP test added;
full CI, deployment and owner browser acceptance pending. DNA Payments,
refund-bearing batches and independent provider/bank payout checks remain
outstanding. No public route changed; no real record, Sheets cell, booking,
payment or recovery snapshot changed. Next: CI/deploy and historical actual
cost reconciliation, bounded approved migration and customer acceptance.


## 25 November — combined Square proposals deployed; direct bank-match conflict guard

PR #290 final head 8958c09bd7c7f1316c2daad612142781ed548bc4 passed
Actions 45691356004 (127 root + 663 integration Python tests, 9 JavaScript
tests). Merged as d06ec9b5; Railway deployment
1f4f413b-e67c-4a55-9b93-b8ef470b86c1 SUCCESS at 21:03:34 UTC.
Environment inventory confirms original 1024 MB /data volume
540528d1-3953-4737-9d92-3c861575c5d4 attached, no staged configuration.
Fresh read-only workbook metadata confirms 48 tabs; Jobs row 4 has
distinct actual inputs in AG:AJ (hours, materials, consumables, extra costs).

Combined proposals are deployed, not independently verified settlements
or fee expenses. Owner browser acceptance, actual payment reconciliation,
historical imports and authority cutover are unverified. CI confirms
customer service-to-HQ review, owner quotation approval, canonical vehicle
requests, product approval visibility and customer account isolation;
no public route changes.

New proposed slice closes the reverse direction of a source-ID conflict:
direct Monzo invoice match review/save and owner-reported allocation review
reject a bank deposit already covered by an active single or combined
Square proposal. Prior tables may be absent on older databases; latest
withdrawal releases the guard. Owner error links to the settlement
workspaces. Existing checks in the opposite direction remain unchanged.
Synthetic tests cover both proposal types, unrelated source, missing schema,
withdrawal and direct report guard. Not a global reservation across all
finance paths; no receipt or expense posting, no migration or data writes.
CI and deployment of this new slice pending.

Next: DNA payout evidence, historical actual-cost reconciliation, first
bounded approved import and independent off-volume recovery/owner acceptance.


## 25 November — reverse settlement guard deployed; historical actual-cost observations

PR #291 final head f29200aa passed Actions 45700938969 (root and integration
regressions, 9 JavaScript tests). Merged as 0d8be3fa; Railway deployment
c5329d75-4ef4-4d3e-b4f4-04e032263fea SUCCESS at 21:58:09 UTC.
Original 1024 MB /data volume attachment and 48 workbook tabs rechecked.
Jobs AG4:AJ4 confirmed Actual Hours, Actual Materials £, Actual Consumables £
and Actual Extra Costs £.

New slice: active imported historical work has an owner-only actual-cost
observation route linked from historical work history. Fresh literal source
values, exact decimal hours and GBP pennies are previewed, confirmed and
saved against the existing canonical work ID, original J reference, source
fingerprint and work revision. Blank and explicit zero are distinct.
Duplicate/replay protection and append-only withdrawal retain evidence.
No native cost ledger, stock, price, invoice, receipt or profit is changed.
No actual historical observation has been saved; this is code and synthetic
test coverage pending CI/deploy. Compare with native cost ledgers before
separate approved reconciliation; no authority cutover. Owner production
browser acceptance and off-volume recovery remain unverified.

Customer service-to-HQ review, owner quote conversion, vehicle creation
requests, product approval and customer isolation passed prior regression;
no public routes changed in this slice. Next: historical cost mapping into
native ledgers (without invented rates/stock), DNA payouts, customer
acceptance and bounded approved historical import.


## 25 November — historical actual-cost observations merged; native aggregate baseline

PR #292 final head f8f56357 passed Actions 45709295606 (664 integration
tests and 9 JS tests observed). Merged as a4ec4fc2. Original 1024 MB /data
volume and 48 workbook tabs verified; Jobs AG4:AJ4 confirmed again.

New proposed owner flow: Historical work > Review actual costs > Preview
native cost baseline > Approve and save native cost baseline. An immutable
retained observation becomes a separately versioned native aggregate with
explicit approval, exact source and work revision, overlap refusal for any
active detailed time/material/expense entry, replay protection and
append-only withdrawal. Native private costing overview displays current
historical baseline separately, or suppresses amounts if stale. Unknown
and zero remain distinct; known non-labour portion is not complete cost.
No invented material quantities, labour rates, stock, payment, fee,
profit or booking. Existing detailed allocations are not changed or added
to the historical baseline. Source observation withdrawal requires baseline
withdrawal first. CI, production UI acceptance and real data reconciliation
remain pending. No historical domain authority cutover.

Customer request/approval, combined quotes, booking acceptance and
confirmation, product deposit and maintenance account isolation passed
PR #292 integration tests. No public route changed. Next: DNA Payments
evidence, owner-approved bounded import and off-volume recovery, full
financial parity and customer production acceptance.


## 26 November — native historical cost baseline deployed; DNA payout evidence

PR #293 final head fb290e9842ffa3c5dc06232ad83bc8aa066a4e92 passed Actions
45717973273 (127 root + 664 integration Python, 9 JS tests). Merged as
bfa95cc6; Railway deployment 0210c88e-ff15-4d8d-ad02-5d9ec9ac0399
SUCCESS at 00:07:01 UTC, one running replica, no issues. Original 1024 MB
/data volume 540528d1-3953-4737-9d92-3c861575c5d4 attached. Read-only
metadata confirms 48 tabs; Monzo Live Feed A7:I7 exact headings retained.
Historical aggregate baseline implemented and deployed, not owner-tested,
migrated, reconciled or authoritative. No historical costs adopted here.

Proposed DNA Payments workspace: Finance Command Centre > DNA payout
evidence. Exact owner-entered provider payout ID, documented gross and fee,
statement reference and original Monzo transaction ID; exact positive net
pence, fresh worksheet fingerprint, explicit preview/approval, persistent
append-only evidence and withdrawal. Duplicate payout IDs (including case
variants), source conflicts with Square single/batch and direct Monzo
invoice matching blocked under SQLite write transaction. Manual statement
evidence only: no DNA API, no customer payment identification, refunds,
chargebacks, verified settlement, receipt, fee expense or bank balance posting.
No live data or provider writes. CI and deployment of DNA slice pending.

PR #293 CI confirms customer service-to-HQ review, combined quote/appointment
handoff, owner approval and customer isolation, linked vehicle requests,
product withdrawal/approval and maintenance price isolation. No public
route change. Production authenticated UI acceptance remains unverified.
Next: independent off-volume recovery and bounded approved import, provider
payment evidence and fee accounting, financial parity and customer acceptance.


## 26 November — DNA evidence live; offline recovery verification

PR #294 final head 50a28732173e7def2b98c98d2e05a86d5f4a424e
passed Actions run 45726526347 (665 integration tests observed; dependency,
syntax and app update steps successful). Squash merged as 92a63043;
Railway deployment 616459d4-ec05-4dd9-80c4-e5bb41d7022a reports SUCCESS
at 01:07:40 UTC. Original 1024 MB /data volume and 48 workbook tabs
were checked before merge. DNA manual payout proposals now deployed;
independent provider/bank verification, actual fee posting and owner
acceptance remain unverified. No authority cutover.

New slice provides an offline verification command for the existing
encrypted SQLite export. The owner workflow describes how to run it on
a trusted separate computer. Passphrase is entered locally, bounded
regular files only, authenticated decryption and SQLite integrity checks
in memory, no database restore or network connection. Output contains
only hashes, byte count and check result, not records. Synthetic test
checks an exported snapshot after removing the temporary live source,
wrong password, tampering and unchanged snapshot. CI/deployment pending.
A locally printed result is not server-attested proof of off-volume
custody; retain it alongside the encrypted copy. No real snapshot,
download, import or workbook write was performed.

Owner action: Historical Jobs Migration > staged job > Private recovery
snapshot > Save private backup and check isolated restore; export using
owner-held public key and verify independently. Then prepare exact
bounded import manifest and separately approve it; do not apply if
source conflicts or missing fields remain. Production browser acceptance
and exact first import eligibility/counts are still unverified.
Customer service-to-HQ, canonical quote conversion, vehicle requests,
product approval and account isolation passed PR #294 CI. Public routes
unchanged. Next: bounded owner-approved historical import, actual
verified receipt/fee accounting and independent customer UI acceptance.


## 26 November — independent recovery verifier deployed; creditor outflow evidence

PR #295 final head 1dff2907fccb6eb95d3c9d7137bc2ef4c605c11f passed
Actions 45733655132 (127 root + 666 integration Python tests and 9 JS
tests, including authenticated offline recovery and tamper rejection).
Merged as 4030f2de; Railway deployment 0178c0ac-50f7-46cf-a260-9e6a9b2bf85e
reports SUCCESS at 01:59:11 UTC, original 1024 MB /data volume
540528d1-3953-4737-9d92-3c861575c5d4 attached, no pending work.
No real recovery copy, independent key custody or restore drill established.
Owner acceptance remains outstanding.

Fresh read-only Customers A4:C1000 inventory: 14 named rows, 14 distinct
normalized names, no repeated normalized name groups in this source range,
six missing postcodes, no missing type. All 48 tabs and Monzo A7:I7
headings verified. These are not live canonical match or conflict counts.
First bounded approval: select 1–5 independently identity-reviewed rows in
Historical Customer Migration > Create missing historical customers
(reviewed batch); leave postcode option off initially, select a current
verified private snapshot, prepare exact review. Exact preview must
confirm proposed IDs, source rows, names, no unresolved native/alias
collisions, before/after fields and unchanged other data. Reversal archives
only untouched newly created identities; dependency or later edit blocks
reversal. Keep an independently verified encrypted copy off-volume.
No production import is authorised by this inventory or development.
Exact eligible row IDs and counts depend on authenticated production
directory review; do not invent eligibility or request blanket approval.

New proposed owner workspace: Creditor cases > select case > Review Monzo
outflows and saved creditor payment proposals, or Monzo classification
review > negative source > Review creditor outflow / saved history.
Require existing current whole-transaction creditor payment classification,
non-archived native case, independent evidence reference and exact owner
approval. Source and legacy evidence, classification/case revisions and
history are bound to the preview. Durable append-only proposal and
withdrawal, replay protection, source conflicts across invoice and
settlement routes, and owner auth/CSRF/no-store. No balance reduction,
payment, bank verification, cash event, change to case, or source write.
No split creditor outflow or independently verified actual financial
posting. New synthetic route test submitted; CI/deployment pending.

PR #295 CI logs explicitly confirm customer service-to-HQ review,
canonical quote conversion, combined appointment handoff, owner booking
confirmation, maintenance pricing isolation, portal vehicle request and
customer isolation. Production HTTP probes to /book and owner creditor
workspace could not resolve DNS in the development environment.
No public route changed. Authenticated browser acceptance, exact approved
import, actual creditor reconciliation, verified receipts/fees and full
financial formula parity remain outstanding. No domain authority change.


## 26 November — creditor outflow proposals deployed; manual transaction edit history

PR #296 final head 36af5a0909b560f7b97e125b807cbcc312004d2f passed
Actions 45739558729 (127 root and 667 integration Python tests, 9 JS tests).
The exact reviewed head was merged as 5bbfdb597ace29bfb0064b0d13694d425ed4f0d8.
Railway deployment 460c027c-e008-4819-80b3-edfdf121110c reports SUCCESS at
03:00:54 UTC. Creditor outflow evidence is now deployed; it remains
non-posting, unverified and not owner-accepted. The existing source
classification, independent evidence and case revision are required.
All 48 Sheets tabs and original 1024 MB /data volume were rechecked
read-only before merge. No actual creditor outflow or other record changed.

New slice: owner Finance Command Centre > Transactions & reconciliation >
Transaction edit history. The previously metadata-only manual transaction
audit now retains observed date, signed pence, description, category and
reviewed flag in additive persistent snapshots. A previously existing
record's current state is captured immediately before its next edit,
without inventing old values or claiming the capture timestamp was its
original edit date. Repeated stale submissions remain conflicts and
cannot duplicate history; split history remains separate and active
splits still block parent changes. The latest 250 captured revisions are
shown in the private interface. Earlier missing history is not recovered.
No extra cash event, receipt, bank verification, source cutover or
classification totals from old versions. Synthetic persistence, stale
write, legacy capture, owner isolation and totals tests added; CI pending.
Native transaction archive and full per-record history pagination remain
outstanding. The 14 source customers and first 1–5 row bounded import
approval described above remain a source inventory only, not eligibility
or approval. No actual recovery snapshot or import executed.

PR #296 CI includes customer service-to-HQ review, canonical quote
conversion, accepted appointment handoff, owner confirmation and customer
isolation. No public routes changed; production owner/customer acceptance
still required. Next: actual approved recovery/import, provider-verified
receipt/fee accounting, full formula parity and native transaction archive.
No domain authority changes.


## 26 November — native transaction edit history deployed; reversible archive

PR #297 exact head 162f658739be8ffab9f07d5ef803c2f567be0014
passed Actions 45746251386 (127 root + 668 integration Python tests,
9 JS tests). Squash merged as 270b7e95d1a95663cfb5522932ccfb27f6129db8.
Railway deployment 0c7e7a4b-7dec-4d79-bc61-4d0503b5b851 reports SUCCESS
at 03:58:02 UTC. Original 1024 MB /data volume verified before merge.
All 48 workbook tab names confirmed with read-only metadata.
Transaction edit history deployed; owner acceptance remains unverified.

New proposed slice: Finance Command Centre > Transactions & reconciliation >
Archive / history; select Archived entries / restore to reopen.
Owner-only exact current revision, explicit reason and CSRF-protected
archive/restore form, append-only lifecycle history, unchanged original
manual transaction values, and native monthly report exclusion with a
visible count. Restore re-includes the original amount exactly once.
Active split blocks lifecycle changes; archived parent blocks ordinary
edits and new splits. Stale/replayed requests do not create duplicate
events. Existing transaction table schema retained. Separate from
read-only Monzo, verified receipts, payments, balances and Sheets.
Synthetic HTTP, isolation, persistence and totals test added; CI pending.
No real transaction archived or restored.

PR #297 CI includes service requests to HQ, canonical approved quote
conversion, customer appointment acceptance and owner confirmation,
maintenance pricing isolation, product approval and account isolation.
Public routes unchanged. Production authenticated UI acceptance still
unverified. Next: verified receipt/fee accounting, exact approved
historical import after independent recovery, formula parity and
remaining operational domains. No domain authority changes.


## 26 November — manual transaction archive deployed; monthly commitment schedules

PR #298 final head 3de6f94fe84963e35bda75895fe570060a4854a3 passed
Actions 45752313849 (127 root + 669 integration Python tests and 9 JS
tests), squash merged as f2fb1bdaffd0f91ce33491033ae7dc21f0e57f0c.
Railway deployment 86e4702f-4a7d-4c8a-9c5c-86b2b5b9d443 SUCCESS at
05:04:41 UTC, startup and original 1024 MB /data mount confirmed;
no pending infrastructure changes. All 48 workbook tabs confirmed by
read-only metadata. Manual transaction archiving and restoration now
deployed; owner acceptance unverified. No real transaction archived.

New proposed owner workflow: Finance Command Centre > Edit finances >
Manage monthly commitments and generate planned bills. Create/edit/archive
or restore a monthly schedule with amount and due day, retaining every
revision. Preview one month, clamp the due day to month end where
necessary, explicitly approve the exact amount/date and create one native
planned bill, with its normal editable bill history and original schedule
provenance. Schedule changes never rewrite generated commitments.
SQLite write transaction, current revision and unique schedule/month
prevent duplicate generation; same-name native bills in that month block
generation even when cancelled or archived. Different-name aliases
require owner review. No automatic generation, bank action, payment,
expense posting or Sheets changes. Synthetic HTTP tests include owner
isolation, CSRF, stale writes, duplicate conflicts, month-end dates,
archive/restore and save/reload. CI and deployment of this slice pending.

PR #298 regression confirms native archive totals and restore, customer
service-to-HQ review, native quote conversion, appointment acceptance
and owner confirmation, maintenance pricing privacy and customer
isolation. Public routes unchanged. Owner/customer production acceptance,
exact approved historical import after independently verified off-volume
recovery, verified receipt and fee posting, and full worksheet formula
parity remain outstanding. No domain authority changes.


## 26 November — recurring commitments deployed; month-wide atomic generation

PR #299 final head 78c034a894f606acf3fdd09121c4aa227946c6ee
passed Actions run 45759351864 (127 root + 671 integration Python tests,
9 JS tests). Squash merged as c1a94e4241c5928f46635f8f6614b6859d1fffc5.
Railway deployment 5fd09e93-a299-4014-bce0-38ff609e4c64 reports SUCCESS
at 05:58:10 UTC, one running replica, no issues or pending work. Original
1024 MB /data volume 540528d1-3953-4737-9d92-3c861575c5d4 was checked
before merge. Read-only workbook metadata confirms all 48 tab names.
Recurring native schedule edits and approved individual planned bills
are deployed, not owner-tested, historically migrated or reconciled.
No historical source authority changes.

New proposed slice: Finance Command Centre > Edit finances > Manage
monthly commitments > Preview monthly batch. An owner reviews a selected
month's exact eligible rows, total and separately listed blocked rows
(up to 25 active schedules), explicitly approves the manifest, then all
eligible planned bills are generated atomically. Existing same-name
native bills and previously generated occurrences are excluded; other
duplicate naming still needs owner review. Changed schedules, newly
blocked rows, changed eligibility and replay reject stale confirmation.
The immutable approved manifest and resulting bill IDs are retained,
even after later edits, and shown in batch history. Synthetic test covers
CSRF, owner isolation, stale approvals, leap date, blocked row,
injected second-item failure and rollback, replay, and reopened
persistent native records. CI and deployment of this slice pending.
No automatic generation, payment, bank verification, source write or
real financial record was performed by development. Verified receipt
and fee posting and full formula parity remain outstanding.

PR #299 CI logs explicitly show customer service-to-HQ requests,
native quote conversion and approved customer status, appointment
acceptance/owner confirmation, combined quotes and customer isolation,
maintenance and product account isolation. Direct HTTP probes of /book
and monthly commitments failed DNS resolution from the development
environment. Owner/customer production browser acceptance unverified.
No public route modified. First 1–5 customer import remains subject to
exact production directory collision review, independently verified
off-volume recovery and explicit manifest approval; no import executed.
Next: verified receipt and fee accounting with provider evidence,
approved historical migration, remaining operational domains and
independent owner/customer UI acceptance.


## 26 November — bulk monthly commitments deployed; provider-confirmed receipt classification

PR #300 final head 9ac3488579644b850a08f9a7857e241550b44582 passed
Actions 45765089671 (672 integration Python tests and 9 JS tests; root
test step also passed). Squash merged as e4822c7106b200e17b6ef5725f5e20473ea202f3.
Railway deployment bd0a4d07-918e-4efb-bd01-ee8aaf51e7b5 reports SUCCESS
with the original 1024 MB /data mount and no pending work. Workbook
read-only metadata confirms all 48 tabs. Batch generation deployed,
not owner-tested, historically migrated or reconciled.

Proposed native slice: Finance Command Centre > Provider-confirmed
receipts. Owner selects an existing reported card allocation with retained
exact Square evidence, explicitly reviews fresh provider status, then
approves the exact existing allocation/invoice/payment classification.
Fresh provider evidence is checked again before the transaction; changes
to invoice, allocation, scope, evidence, duplicate references or approval
state block the save. Append-only recognition and withdrawal history;
latest locally known changes invalidate the recognition display. No second
receipt, income total, bank settlement, refund or customer status is
created. Provider-confirmed means exact completed GBP and explicit zero
refunds at the observation time, not current live status. Partial/refunded
payments require separate review. Bank posting and processing fee
accounting remain outstanding. Synthetic approval, stale-provider,
duplicate, refund recheck, withdrawal, storage and isolation test added.
CI/deployment of this slice pending.

PR #300 CI explicitly passed service request owner review, native quote
conversion and customer approval visibility, appointment acceptance and
confirmation, customer vehicle linkage, maintenance pricing isolation,
product approval and deposit visibility. No public routes modified.
Production owner/customer authenticated acceptance still unverified.
Historical import and independent actual recovery remain unapproved.
No authority cutover, live financial edits, messages or Sheets writes.


## 26 November — provider-confirmed receipts deployed; exact provider fee evidence

PR #301 final head f542838f45e5cff0c66556179b832616024b51e3 passed
Actions 45772138958 and merged as c509dcc92cc258315f2f586acdd9be492700f5f6.
Railway deployment fdcca90c-151c-47be-ac98-7ba4a3e84b0e reports SUCCESS
at 07:01:40 UTC, original 1024 MB /data volume
540528d1-3953-4737-9d92-3c861575c5d4 attached; no pending infrastructure
changes. Read-only workbook metadata confirms all 48 tabs.
Provider-confirmed existing allocation classification deployed, not
owner-tested or bank reconciled. No authority cutover.

New slice: Finance Command Centre > Provider-confirmed receipts >
Read and preview Square processing fees on an eligible recognition >
Save provider processing fees. The exact GetPayment read now optionally
validates explicit bounded GBP card processing fee entries, including
signed adjustments, while preserving the existing adapter output for
all other callers. Missing/empty fees are unknown; unsupported types,
ambiguous duplicate rows, inconsistent currency, timestamps or amounts
fail closed. One initial fee plus adjustments, with nonnegative bounded
net fee, is the first supported subset. Both payment and fee details
come from the same read. Approval binds the unchanged recognition,
payment and exact fee rows, re-reads on save, and checks local revisions
under the write transaction. Persistent original fee snapshots, review
time, withdrawal and stale receipt warnings remain private. No new
receipt, expense entry, payout/bank balance or aggregate financial
posting; payment processing fee observations may not include later
payout adjustments or other charges. No provider write or actual
financial record changed. Synthetic HTTP tests exercise signed fees,
unknown/malformed evidence, stale approvals, replays, reload and
withdrawal, owner/CSRF isolation and unchanged receipt totals. CI and
deployment pending. No public route modified by this slice.

Remaining: bank settlement and verified fee/receipt accounting in
financial totals, exact approved historical import after actual
independently verified recovery, formula parity, authenticated owner
and customer browser acceptance and remaining workbook domains.


## 26 November — processing fee evidence deployed; settlement fee cross-check

PR #302 final head 7935c9be66835c97049b1d8c0937e37b44a519d6 passed
Actions 45779492348 (127 root and 674 integration Python tests, 9 JS tests).
Merged as c3ef29833cadedac5e2cfa5369ece37cb4a69c8b; Railway deployment
62a423b0-1f18-4728-a111-9e02932b6b84 SUCCESS at 08:58:29 UTC, original
1024 MB /data volume attached, no pending environment changes. Read-only
workbook metadata confirms 48 tabs. Actual owner approval and provider/bank
reconciliation remain unverified.

New slice: Finance Command Centre > Settlement and fee review (single or
combined). On preview and save compare retained, approved current payment fee
revisions against the documented payout fee. Persist each matched manifest,
revision and exact pence in the existing proposal; changed evidence invalidates
an outstanding preview. All known fees must agree with the documented total;
missing or withdrawn fees remain unknown, and partial coverage remains a
manual statement proposal. Stale active fee evidence blocks save. Old proposals
remain readable and explicitly lack a fee comparison. No independent payout
API or bank verification, expense posting, income/balance inference or new
receipt. No provider writes. Synthetic regression coverage of fee mismatch,
unknown/partial coverage, persistent exact comparison and stale receipt/fee
review; CI pending. No public route changed; previous regression confirmed
customer service request and native conversion, vehicle linking, customer
status/deposit visibility and account isolation. Actual browser acceptance,
recovery snapshot and bounded historical import approval still outstanding.
No authority cutover; no real records, payments or Sheets cells changed.


## 26 November — settlement fee cross-check deployed; monthly card receipt evidence

PR #303 exact head 876ff60002ca4e62cd970f0f6aace2b1adad0be1 passed
Actions 45787811185 (674 integration Python tests and 9 JS tests, syntax
and dependency checks passed). Squash merged as 41353437129aa791795b190410f00b8f23611762.
Railway deployment bf87ae35-081d-43d7-aad1-040eb1731117 reports SUCCESS
at 10:05:30 UTC. Original 1024 MB /data volume checked before merge;
no infrastructure changes. All 48 workbook tabs and Monzo A7:U7 headings
read-only verified. Existing settlement fee comparisons are deployed,
not owner-tested, bank reconciled or authoritative.

New proposed owner workspace: Finance Command Centre > Monthly card
receipts & fee coverage (also linked from Provider-confirmed receipts).
Choose payment month in UTC; native persistent recognised receipts
and current fee approvals are joined by exact allocation/payment,
revision and evidence digest. One payment counted once, withdrawn or
locally stale recognition excluded and shown separately. Known fee
subtotal and included gross separate; net only when every included
payment has current retained fee evidence. Empty or missing amounts
are unknown. The bounded whole-register query withholds totals above
its safe limit rather than showing partial totals. Editing/withdrawal
uses existing owner approval screens; refreshed report reflects the
retained revisions. No provider calls, new cash events, bank balance,
expense posting, verified total income or profit inference. Fees are
grouped by payment month, not accounting effective date. Synthetic
route coverage for month selection, missing fees, stale/refunded
recognition, privacy and exact pennies; CI and deploy pending.

PR #303 CI includes public book request to canonical owner-approved
portal, quote conversion, appointment acceptance and confirmation,
maintenance pricing isolation, vehicle requests, product approval
and deposit visibility. No public route modified. Authenticated live
browser acceptance remains unverified; first historical 1–5 customer
import requires exact conflict-free manifest and independently
verified recovery, as previously recorded. No actual live records,
source writes, financial posting, or historical authority changes.
Next: independent bank settlement verification and fee effective-date
accounting, workbook formula parity, approved bounded imports and
owner/customer production acceptance.


## 26 November — monthly receipt evidence deployed; fee effective-date separation

PR #304 exact head fb6679458a56e6e13f3ea67e1cb3228de327c8fe passed
Actions 45798184862 (674 integration Python tests, nine JS tests;
dependency and syntax checks passed). Squash merged as
d4b68bafe9c8ad33bc35739d4d94bbd705cbffeb. Railway deployment
620d0d7f-378f-4c53-af09-dda7896322ab reports SUCCESS at 11:02:21 UTC;
original 1024 MB /data volume 540528d1-3953-4737-9d92-3c861575c5d4
remains attached and no pending infrastructure work. All 48 worksheet
tabs read-only reverified; Cash Engine formulas inspected. No live
database rows inspected or changed, no actual recovery or import
approved, no historical authority changes.

New proposed slice: monthly card evidence now separately reports signed
fee lines by provider effective month (UTC), including credits relating
to earlier payment months. Reuses persistent exact receipt and fee
approvals and checks the entire bounded register, validates retained fee
lines and total, rejects duplicate evidence and excludes stale or withdrawn
approvals. Missing/unknown effective dates are counted separately across
all retained classifications; no empty subtotal assumed zero. Payment-month
fee totals remain separate and must not be added to this view. No bank
settlement, verified expense posting, cash balance or business profit
inference. Existing approval/withdrawal paths remain editable. Synthetic
HTTP test changes an October payment to have a November fee credit and
checks both reports and stale-evidence exclusion. CI and deployment pending.

PR #304 successful logs explicitly cover customer-to-HQ browser request
review, itemised quote privacy, owner native quote conversion and customer
approval status, vehicle linkage and shop customer auth. No public routes
changed. Authenticated production UI acceptance not established. First
historical import and independent recovery remain separate owner approvals.
Next: independent bank settlement verification, native finance formula
parity and approved historical import; continue owner/customer workflows.


## 26 November — effective-date fees deployed; native finance plan history

PR #305 exact head 2f7f85821ffaca1fe06a13e24b8a3e9a8f900f93
passed Actions 45806996399 and was squash merged as
81e0d688006b43dfabcdeff673597282f926768d. Railway deployment
9a9bffee-641b-41c1-b7c9-c5f0ae1441b3 reported SUCCESS at 12:01:24 UTC.
The original 1024 MB /data volume was attached before merge and there
were no staged infrastructure changes. All 48 workbook tabs and Cash
Engine A6:I12 formula schema were rechecked read-only. No live records
or workbook cells modified; no new historical authority.

New proposed owner slice: Finance Command Centre > Edit finances >
Finance input history. Saving a balance snapshot or monthly target now
retains the previous observed values and new revision in the same native
SQLite transaction. Earlier unavailable revisions are not fabricated.
Concurrent/repeated stale submissions are rejected by existing revision
checks, with no duplicate history. The overview's next-30-day estimate
is now calculated server-side in integer pence, using the UK calendar,
including overdue planned commitments, excluding cancelled/archived
commitments, and withholding the estimate when the balance is missing,
future-dated or a reported bill payment on or after its snapshot date
makes the snapshot ambiguous. This is a planning estimate of recorded
commitments, not a verified bank balance or a reproduction of the Cash
Engine's expected receipts or full cash forecast. No payment or external
action. Synthetic owner route tests cover legacy record capture, reload,
revision conflict, CSRF, privacy, and exact forecast boundaries. CI and
deployment for this slice pending.

PR #305 successful regression logs cover public service submissions,
itemised quote privacy, native quote conversion, customer approval
status, maintenance pricing privacy, vehicle linking, and product
order/deposit visibility. No public routes changed. Authenticated live
UI acceptance remains unverified. Recovery snapshot and exact bounded
historical import approvals remain outstanding; no owner approval
inferred. Next: direct bank verification requires a supported independent
source; existing worksheet and owner-reported statements are not direct
bank verification. Continue native financial formula parity and editable
owner/customer workflows while migration approval remains outstanding.

### 26 November — finance plan regression correction

Actions 45815984881 failed exactly one source-continuity test (the old
client-side expression was asserted literally after forecast computation
moved to the server). The connected Sheets endpoint, source labels,
stale indicators and separate accounting remain unchanged. Replaced
that assertion with checks of server forecast rendering, missing-value
display and reasons; retained all source continuity assertions. Added
synthetic authenticated save/reload test for overdue commitments,
ambiguous reported payment date and fresh balance recovery. No
application logic change in this correction; fresh full CI required.
Main and Railway still at #305 at inspection, original /data volume
attached; all 48 workbook titles read-only confirmed. No actual import,
recovery operation or authority cutover.

## 26 November — finance plan history deployed; Business Affordability subset

PR #306 first failed the literal old client forecast assertion. After
correction and an added authenticated HTTP save/reload test, exact final
head ddf022d94a5e65caaac8c3af2b2ab5d7383d137b passed Actions 45825760512
(127 root + 677 integration Python tests and 9 JS tests). Squash merged
as 5766ea4f899442dfd5d5946690b6a85ee28218e7. Railway deployment
27c263d9-e713-499d-b5f6-918d7cf5b8de SUCCESS at 13:06:41 UTC;
original 1024 MB /data volume attached; no staged changes.
Synthetic save/reload, exact forecast, old history capture and conflict
tests passed. Authenticated owner production acceptance unverified.

All 48 workbook tabs, Cash Engine A6:I12 and Business Affordability
A1:I35 formula schema inspected read-only. New owner slice in existing
Creditor affordability scenario: editable optional observed monthly
director drawings and additional requested payment, with immutable
revision history and old record compatibility. Reuse existing saved
monthly inputs. Match Business Affordability H12/H18/H19/H25/H27/H29
formula subset: trading excludes director support, outgoings always
include growth and observed debt, net self-funded operating cash,
reserve shortfall, shared modelled safe capacity and requested excess.
The existing capacity model rounds down to whole pence. Missing optional
values remain unknown. No bank-derived verification, SEND STATUS,
creditor-specific statement readiness or full formula parity. No
communications, payment, creditor balance or actual financial change.
Synthetic calculation and owner save/reload/history tests submitted;
new slice CI/deployment pending.

PR #306 passing logs confirm public service request to HQ, canonical
owner quote conversion, approved customer PDF parity, customer account
isolation, combined-basket appointment handoff, product and maintenance
privacy. Public /book HTTP probe from development environment still
fails DNS; authenticated live acceptance outstanding. No public changes.
Recovery PRs #258 and #295 merged and original volume preserved;
actual encrypted off-volume recovery and exact reviewed 1–5 customer
manifest remain owner approval gates. No historical cutover.

### 26 November — affordability regression correction

Actions 45826138401 failed the authenticated save/reload assertion: the saved £400 request and £360 capacity rendered an incorrect missing-request message despite correct stored inputs and formula outputs. Replaced template boolean-identity branching with explicit missing-value and integer-pence comparisons. Extended the same HTTP regression to cover exactly-at-capacity, known zero, clearing an optional input and preserved historical revisions. Fresh exact-head CI required; no merge until green. Railway inspection confirms #306 still deployed, original 1024 MB /data volume attached and no pending changes. All 48 workbook titles independently rechecked read-only. No owner acceptance, data import or financial posting performed.


## 26 November — Business Affordability deployed; editable Cash Engine observation periods

PR #307 exact head 3cfb1e4f967ae33a4a637a359683e4b485936485 passed
Actions 45834594695 (127 root + 679 integration Python tests, 9 JS tests),
including the corrected authenticated request comparison, exact-capacity,
zero and blank cases. Squash merged as d8908e577e85dbf355af19a0de8fc47ea9bbe099.
Railway deployment af356714-d344-4b7f-a86f-d8c0c85854d3 SUCCESS at
14:01:21 UTC. Original 1024 MB /data volume attached; no staged changes.
All 48 workbook tabs and Cash Engine B4:I12 / Business Affordability H12:H29
formula schema rechecked read-only. Native owner production acceptance
remains unverified; no domain cutover.

New slice: existing editable affordability scenario gains an explicit choice
of direct monthly inputs or owner-reviewed totals for an inclusive 1–366 day
observation period, with the evidence date as period end. Cash Engine
B7/E7/H7/B8/H8/B9 arithmetic subset uses period total × 30.4375 / days,
rounded once to nearest penny at the native monthly-input boundary.
Derived monthly fields and original period totals/dates persist together
in the existing immutable revision history. Missing optional drawings
remain unknown; explicit zero is known. Switching input basis preserves
prior revisions. No automatic bank classification, lookback settings
parity, independent verification, premise subset E8, or actual posting.
Synthetic formula and authenticated save/reload, replay, stale revision,
validation and history tests submitted; CI and deployment pending.

PR #307 passing logs confirm canonical owner/customer quote PDF totals,
combined-basket appointment handoff, customer isolation and native quote
conversion with customer approval visibility. No customer route changed.
Direct HTTP probe of /book and /hq/finance/creditor-capacity failed local
DNS resolution; production browser acceptance remains unverified.
Recovery export and isolated verification code deployed earlier; actual
independent copy and exact bounded customer manifest remain separate owner
approval gates. No live business records, workbook cells or payments changed.


## 26 November — period observations deployed; native transaction evidence review

PR #308 exact head d4c0dbf4f10ff94e6cfceac50ed440dd5b612194 passed
Actions 45835022791 (127 root + 681 integration Python tests, 9 JS tests).
Squash merged as a835b9b2f5cd8e72af3ccc6a8ccbb65219365eb6; Railway
deployment 88c861f9-1299-4b94-90be-df31d6ff1e65 SUCCESS at 15:07:30 UTC.
Original 1024 MB /data volume checked attached before merge; startup
logs confirm mount, gunicorn worker, and status 1/1 running with no issues.
Read-only metadata enumerated all 48 tabs, Monzo A7:U7 and Cash Engine
A4:I12 / A14:P15 formula schema. No live database contents inspected.

New proposed slice: Finance Command Centre > Affordability calculator >
Prepare native transaction observation. Choose inclusive 1–366 day range;
reuses exact native split classification engine on a bounded 5000-row
snapshot. Only reviewed manual transactions with explicit supported
categories and sign enter partial suggested period totals. Splits replace
parents, archived excluded; transfers, loans, refunds, unsupported
categories, unreviewed and wrong-sign lines disclosed separately.
Missing categories unknown, including director funding (no explicit
native category). Owner edits or supplies all required totals, confirms
partial evidence, then saves the existing scenario. Under the same SQLite
write transaction, re-read source and compare digest covering IDs,
revisions, dates, classifications, amounts and exclusions; reject stale
preview. Retain original mapped subtotals and digest alongside owner
inputs in immutable history. Exact replay does not duplicate revision.
No automatic posting, provider verification, complete feed claim,
cash balance inference or historical cutover. Synthetic HTTP tests
submitted for preview/save/reload, split conservation, exclusions,
stale source and replay. CI and deployment of this new slice pending.

PR #308 passing logs confirm customer request to canonical owner-approved
portal, combined quote/appointment handoff, owner confirmation and
customer-visible status, owner/customer PDF parity, maintenance pricing
privacy and account isolation. No public route changed. Direct HTTP probe
of /book and /hq/finance/creditor-capacity failed DNS; authenticated live
UI acceptance still unverified. Actual independent off-volume recovery
and exact bounded historical import remain separate owner approvals.
Next: direct independent bank evidence and richer classification mappings
after explicit review, remaining formula parity, bounded migration and
customer production acceptance. No actual data, payments, bookings or
workbook cells modified.


## 26 November — native transaction observations deployed; explicit funding and refunds

PR #309 initially failed a synthetic split fixture with missing Flask application
context (Actions 45846169575). Corrected the fixture without weakening the
production checks; exact head 26abf31961bb2706ba0ef5c50534d006792679ba passed
Actions 45854652965 (127 root + 682 integration Python tests and 9 JS tests).
Squash merged as d337b6ac412d1fa352e97f0f6962047ac9341c5a; Railway deployment
e4ee1859-ce39-4c1c-97fc-21b09b955e46 SUCCESS at 16:01:59 UTC. Existing /data
volume 540528d1-3953-4737-9d92-3c861575c5d4 checked attached; 1/1 replica,
no pending infrastructure work. All 48 workbook tabs and Cash Engine A4:I12
and A14:P15 formula schema were read-only rechecked. No production database
rows or Sheets cells changed by development.

New proposed slice: Finance Command Centre > Transactions & reconciliation
supports explicit director funding, external funding, loan repayment,
non-trading refund, customer refund and director loan repayment. Existing
legacy categories remain unchanged and are never automatically reclassified.
New categories require the correct signed nonzero amount on parent and split
edits. Cash Engine native review mapping v2 includes reviewed director funding
as owner support (not trading) and loan repayment as debt outflow; other new
categories are explicitly disclosed as excluded amounts. Customer refunds are
not silently netted into gross trading suggestions; owner reviews and corrects
period totals before saving. Existing v1 saved provenance is preserved; new
previews use v2 and stale digests are rejected. No verified bank balance or
posting is implied. Synthetic HTTP regression covers editable classifications,
direction rejection, legacy ambiguity, excluded refund and funding totals,
owner-edited observation, persistence and replay. CI/deployment pending.

PR #309 passing logs confirm service and combined-basket requests, canonical
quote conversion, customer-visible approval, owner booking confirmation,
maintenance pricing isolation and private product quote workflows. No public
routes changed. Direct production HTTP probes for /book and creditor-capacity
still failed DNS in the development environment; authenticated live acceptance
unverified. Recovery export and exact 1–5 customer import require separate
owner approvals; no live snapshot or import executed. Historical authority
unchanged. Next: independent bank evidence, full formula parity (including
premises subset and remaining settings), approved bounded migration, customer
production acceptance and legal/operational workflow gaps.

### 2026-11-26 — persistent job timer implementation (pending exact-head CI/deployment)

- PR #310 advanced native funding/refund categories merged; Railway deployment db9930c3 succeeded. Workbook schema rechecked read-only (48 tabs).
- Replace new device-only timing with durable canonical-job server sessions, explicit start/pause/resume/finish and reviewed whole-minute atomic ledger save. Preserve existing device-only recovery and raw elapsed seconds. Allowance and correction reason retained; synthetic multi-day and cross-device tests added. No live timer or job creation, no price acceptance. No historical cutover or authority change.
- Owner production acceptance, persistent allowance editing, session history display and handling job closure with a running timer remain pending; no background alerts or independent bank verification claimed.
- CV Motors black Kia matching workbook job not confirmed; owner search canonical HQ before creation. No J-series ID guessed.

- Follow-up: restored the closed-work correction lock explanation after the existing regression detected its omission. Added transaction-bound guards to block closing/cancelling or archiving jobs with running, paused or review-pending timers, and a synthetic native-job regression proving that saving reviewed time unlocks closure. Exact-head CI and deployment are still required before claiming production availability.

### 2026-11-26 — timer follow-up

PR #311 merged; #312 sleep-display recovery and 90% warning and #313
cross-job running-session exclusion are included in production commit
328d26dcd310dfa1f682f485d4ff8f5e48ecada3. Railway deployment
cda4d4d9-de02-48e0-b1c9-ac1e9df9a5af SUCCESS at 18:27:08 UTC, same
1024 MB /data volume attached; no staged changes. #313 exact-head CI
45875802494 succeeded. Workbook metadata confirms all 48 tabs read-only.

Proposed follow-up: edit active or paused timer allowance with exact session,
revision and operation binding, without changing elapsed work, breaks, job
price or actual ledger. Show bounded session and action/correction history to
owner only. Synthetic regressions added; exact-head CI/deployment pending.
Owner browser acceptance, actual Kia job identity and reviewed intervals
remain unverified. Keep owner passkey active; isolated synthetic testing is
preferred to a production authentication bypass. No production record or
workbook writes, migration approval, external messages, bookings or payments.
No historical authority change. Public booking routes unchanged; production
customer-browser acceptance remains outstanding.


### 2026-11-26 — timer allowance and next-session context

PR #314 exact-head regression run 45881173227 succeeded (dependency, syntax,
integration/regression and app update checks); merged as
243e81e56cb7ba71d1d58fcc76af1e5c84dedd79. Railway deployment
2824c8b4-0af1-4258-b29c-33c5712d1ed7 SUCCESS at 20:05:02 UTC.
Prior production health confirmed attached volume, 1/1 replica, no staged
changes. All 48 workbook tabs confirmed by read-only metadata. No data
cutover or historical authority change.

Next-session context: show canonical customer/vehicle/service and saved
labour alongside the persistent controls; offer the most recent saved
session allowance as an editable suggestion, never a discarded session.
No automatic start or price/time changes. Synthetic regression covers
saved/discarded sessions and authenticated form rendering. Exact-head CI
and deployment pending. Owner browser acceptance and current Kia identity
remain outstanding; passkeys unchanged. Public Bookings routes unchanged;
customer browser acceptance remains outstanding.


### 2026-11-26 — persistent cumulative timer presentation

PR #315 exact-head CI 45888450862 succeeded and merged as 6229be48c6b55e372e9982c289cbafb61f7e36d9. Railway deployment d9b5d676-36d0-457c-adca-ac6ca00c3365 was BUILDING on first inspection; deployment verification separate. Existing /data volume confirmed and 48 workbook tabs read-only verified.
This proposal displays cumulative recorded labour plus current session, a separate live excluded break clock and allowance percentage in the persistent owner view. No ledger, customer price, timer state or authentication changes. Synthetic JS and authenticated route regression added. Exact-head CI and owner acceptance pending. No historical authority change, live import, external communications or customer booking changes.


### 2026-11-26 — cumulative timer deployment and unfinished-session recovery

PR #316 exact-head a37ef12abe2b6cb08bf88b2c1ef9155da4e1fa8d passed
Actions 45895273720 (dependency, syntax, integration/regression and JS
checks). Squash merged as e4257a1c3d9e9a2a128a740e7737efe502c8129c.
Railway deployment 5344b506-a37f-4b19-aa5f-bf03dadc6a04 SUCCESS at
22:03:34 UTC, original volume 540528d1-3953-4737-9d92-3c861575c5d4
mounted at /data, 1024 MB, no pending infrastructure changes.
Workbook schema rechecked read-only (48 tabs). Jobs A1:Z1000
Kia search returned J0014 (other customer) and J0073 (CV Motors
bonnet); current black Kia not identified. Native production job
identity unverified. No record created or timer started.

Next slice: authenticated /hq/timer lists unfinished canonical
sessions independent of job search, prioritizing running then
review then paused, with explicit recovery links and bounded
display. Merely viewing never resumes or starts a timer.
Synthetic regression exercises search exclusion, owner authentication,
running/paused/review ordering and removal after reviewed labour save.
Exact-head CI and deployment pending. No changes to authentication,
job prices, timer transitions, payment or public booking routes.
Existing customer service quote conversion, approval visibility,
account isolation and product quote regressions passed in #316.
Authenticated production browser acceptance remains outstanding.
Historical migration, formula reconciliation and authority unchanged;
actual off-volume recovery and bounded import still require owner
approval. No production database row contents inspected or changed.

### 2026-11-26 — recovery queue regression and reviewed timer correction

PR #317 exact head 38e4afff47082ead48069f243a8b388406c5b56f passed
Actions 45902350476 (dependency, syntax, full integration/regression and
JavaScript checks), squash merged as f78b08e2b99b5c80a001f11f9c65f233a5a85699.
Railway deployment 0a6e77ac-f665-485d-8252-cf9666d324e2 was BUILDING
on initial check. Original /data volume 540528d1-3953-4737-9d92-3c861575c5d4
(1024 MB) confirmed before merge. Workbook 48-tab schema reverified read-only.

Next slice: owner review can reduce measured timer minutes with a required
reason, preserving original active/break seconds and posting exactly one
linked ledger entry. An audit event retains original seconds, approved
minutes and correction reason. Increases, manual-entry misuse, missing
reasons and conflicting replays rejected; existing exact-post fingerprints
unchanged. Synthetic route and persistence regression included. No live
record edits, price changes, external actions or authentication changes.
No changes to public Bookings routes; exact-head CI and deployment pending.
Native domain implementation and historical authority/reconciliation
status remain unchanged. Current Kia native identity and authenticated
production acceptance remain unverified. Off-volume recovery and bounded
historical import require separately evidenced owner approval.


### 2026-11-27 — reviewed timer correction and owner-friendly labour cost entry

PR #318 exact head bd53dd4cce0ecc15de1e43b52a28bfa0d01615ee passed
Actions 45914722241 and squash merged as 65a04f9fc1f1d1b333d2456f6dff45662baac3e1.
Railway deployment 3a2ab356-348e-40e9-a922-4203c11375ba was SUCCESS at inspection; original /data
volume (1024 MB) verified before merge. Workbook schema rechecked read-only
(48 tabs); Jobs A1:Z1000 returned only J0014 (other customer) and J0073
(CV Motors bonnet), not the current black Kia. Native identity unverified.

Next slice: owner actual labour cost entry in pounds and pence with exact
integer-pence normalization, preserving legacy pence requests, unknown rates
and idempotent fingerprints. Reject ambiguous dual-rate submissions and
fractional pence. Synthetic authenticated timer review and replay regression
included. Exact-head CI and deployment pending. No production record writes,
customer price changes, public booking changes or new historical authority.
Production browser acceptance and owner-approved recovery/import remain open.



### 2026-11-27 — pounds-rate deployment and pre-save labour cost preview

PR #319 exact head 3516f0c3a930f53fdb935c4acbe37f5f290fc104 passed Actions
45919897747 (127 + 694 Python tests and 12 JavaScript tests) and squash merged
as 198f2b72b240118f539fb4d52c721ea33eed866d. Railway deployment
b8a2f280-3ae0-4d05-a48a-4e13d7960984 reported SUCCESS at 01:59:31 UTC.
Existing /data 1024 MB volume verified before merge. Read-only workbook
metadata confirmed 48 tabs; Jobs A1:Z1000 again returned J0014 and J0073,
neither matching the current CV Motors black Kia scope. Native job ID and
authenticated production UI not verified; no live job created or timer started.

Next slice: owner-only pre-save labour preview for reviewed or manual minutes,
with exact integer-pence half-up per-entry rounding, projected cumulative
recorded labour cost only if all previous rates and current rate are known,
and review-minute bounds. Preview is not a save, customer price, invoice,
receipt or profit. Tests cover initial owner HTML, live edits, explicit zero,
unknown/invalid rates and over-measured minutes. CI and deployment for this
slice pending. Customer-to-HQ service request, mixed-quote conversion,
appointment handoff and isolation passed in prior CI. No new historical
authority, reconciliation or production record writes; original owner
approval gates for encrypted recovery and bounded historical import remain.


### 2026-11-27 — labour preview deployed and complete activity cost ledger

PR #320 exact head ccbb48810c5b4638ed510d423b9b891ad268cefc passed
Actions 45925760558 (127 + 694 Python tests; 14 JavaScript tests).
Squash merged as 05720232984ec311307525aa98c42d381076e51d.
Railway deployment 5d7c5f10-711a-4d17-8fb7-dfe4af70341c SUCCESS at
03:04:54 UTC; one replica running, zero warnings or criticals, no
pending work. Original 1024 MB /data volume confirmed before merge.
All 48 workbook tabs confirmed read-only; Jobs A1:Z1000 Kia search
returned J0014 and J0073 only, neither matching current black Kia scope.
No native job identity confirmed, timer started or live data written.

Next slice: make saved activity minutes, entry counts and internal labour
costs visible in the persistent timer (not the hidden legacy cockpit).
Aggregate all active entries from SQLite, not the latest 100 visible
history rows; exclude reversals, preserve per-entry penny rounding and
unknown rates, disclose partial known-cost subtotals. Fix history truncation
detection when reversed entries reduce the active count below 100.
Synthetic regression covers 103 entries, owner-only page, unknown and
explicit zero rates, reversals and exact total parity. Exact-head CI and
deployment pending. No changes to public booking routes, prices, payments,
authentication or production data. Prior CI confirms canonical customer
service/combined quote conversions, approvals, appointment handoff and
customer isolation. Direct /book and /hq/timer HTTP probes again failed DNS
in this environment; authenticated production UI remains unverified.
Historical domain authority unchanged; approved actual recovery snapshot
and bounded historical import still outstanding.


### 2026-11-27 — full activity ledger deployed; auditable rate correction

PR #321 exact head afd5ad041c86631fcef59dec045f98bdb01e0c04 passed
Actions 45932125318 (127 + 695 Python tests, 14 JavaScript tests).
Squash merged as 4410b51d7630725e07ac4f0249d3627f373c9653.
Railway deployment 5ce440e1-2dea-42e2-92db-8e0bee19948d SUCCESS at
04:03:14 UTC. Original 1024 MB /data volume confirmed before merge;
workbook metadata read-only confirms 48 tabs. No historical authority
changed. Customer service request, canonical mixed-quote conversion,
approval/appointment handoff, and account isolation passed in CI.
Authenticated production browser acceptance remains unverified.

Next slice: allow owner to correct previously saved internal hourly cost
without changing the time, activity, note, customer selling price or
timer source evidence. Atomic revision-checked reversal and replacement,
original-to-successor audit link, mandatory reason, replay protection,
unknown and explicit zero rate handling. Synthetic HTTP/SQLite regression
also checks combined canonical cost report parity. Exact-head CI and
production deployment pending. Archived work remains locked; no
production job, price, payment, workbook or customer-facing write.
The current black Kia has no verified canonical native job identity.
Actual encrypted recovery and bounded historical import still require
separate owner approval; no domain newly reconciled or authoritative.


### 2026-11-27 — saved rate correction deployed; saved minutes correction

PR #322 exact head 2965ba182c31d2dc6cbd97f06b49e264e17a2b3b passed
Actions 45937801631 (127 + 696 Python tests, 14 JavaScript tests).
Squash merged as 3ebe3cbd6528c06eccafdf9c206812f9ab7f1e69.
Railway deployment a6aba68f-cbcb-4805-b84d-895a678fb78f SUCCESS
at 05:01:05 UTC; one running replica, no warnings, criticals, failures
or pending work. Original 1024 MB /data volume mount verified.
Read-only workbook metadata confirmed all 48 tabs. Customer service
request/owner conversion, customer-visible approvals, appointment
handoff, product requests and account isolation passed in exact-head CI.
Direct /hq/timer and /book probes failed DNS; production owner/browser
acceptance and actual native black Kia identity remain unverified.

Next slice: reduce an already saved time entry (not only pre-save timer
review) with a mandatory reason, atomic revision-guarded reversal and
replacement, original-to-successor audit evidence and exact replay
protection. Preserve measured session, activity, note, hourly rate
(including unknown), customer price, and canonical job ID. Mixed rate
and minute correction chains preserve evidence and cost parity without
double counting. Only positive reductions are permitted; existing
closed/archived locks and owner authentication/CSRF remain. Synthetic
HTTP/SQLite tests cover validation, replay, conflicts, audit, combined
cost calculation, and unknown rates. Exact-head CI/deployment pending.
No live jobs, times, prices, financial or customer records, Sheets cells,
or customer-facing routes changed. No domain newly authoritative.
Actual recovery snapshot and first bounded historical import still await
separate owner approval and verified mapping/reconciliation.


### 2026-11-27 — saved-minute correction deployed; integrated cost allocation editing

PR #323 exact head 11fb7c88b38e0b102fc76faf51a55c0be09fc433 passed
Actions 45943347557 (127 + 697 Python tests, 14 JavaScript tests),
squash merged as 6d036266a4de78bee4a07022e6a2a09365d7e739.
Railway deployment 1d9f6f2a-a9b2-4e44-b6db-d885c6e04901 reported
SUCCESS at 05:58:01 UTC; original /data volume 1024 MB remains
attached, no pending infrastructure work. Read-only workbook metadata
confirmed 48 tabs. Exact-head CI confirms service quote conversion,
customer-visible approvals, combined quote appointment handoff,
maintenance pricing privacy and customer isolation. Authenticated
production UI and current black Kia native job identity remain unverified.

Next slice: actual material and expense/overhead allocation forms accept
normal pounds and pence, with exact integer-pence storage and equivalent
legacy replay fingerprints, rejecting ambiguous dual amounts and fractions
of a penny. The owner-only timer links directly to these versioned editors
and shows a consistent three-ledger snapshot for the same work revision.
Missing allocations remain unknown, historical aggregates and unsaved
timer time are excluded, and approved selling price less recorded
allocations is expressly not final profit. Stale/unavailable costing is
withheld without disabling the timer. Synthetic authenticated save/reload,
validation, replay and combined-cost regression added. Exact-head CI and
deployment pending. No live record writes or Sheets changes; no historical
domain newly reconciled/authoritative. Actual recovery snapshot and bounded
import approval remain separate. Next owner acceptance: select existing
job, save actual labour, open Record materials and Record expenses, then
reload timer to verify corrected amounts and job revision.


### 2026-11-27 — integrated cost allocation deployment and recovery allowance warnings

PR #324 exact head 81a53c10ee29a3f7c1770840877fc63cfcfe69c8 passed
Actions 45948994634 (127 + 698 Python tests and 14 JavaScript tests).
Squash merged as e30ab8660d0d9dac14cdbfd453f08368adfdd22d.
Railway deployment b84d2a26-c52f-4415-8ea0-d87ea49cc8ea SUCCESS at
07:04:22 UTC. Existing /data 1024 MB volume verified attached, no
pending infrastructure changes. Read-only workbook metadata confirms
48 tabs. Synthetic authenticated timer cost allocation save/reload,
legacy replay, stale-cost withholding, customer request conversion,
customer approval status and account isolation passed in CI.
No production database contents or authenticated UI independently
verified; current black Kia native ID remains unverified.

Next slice: recovery list shows saved active ledger minutes plus
current server-measured session time and allowance remaining, approaching
at 80%, reached or exceeded. Paused and review states freeze active
time, voided labour excluded, missing allowance explicitly unknown.
Private recovery page is no-store; opening it changes no timer or job
state. Snapshot on page load, not a background push notification.
Correct stale material pounds-entry example. Synthetic HTTP and SQLite
regression covers threshold boundaries, multi-day break, review and
owner-only access. Exact-head CI/deployment pending. No live data
writes, customer app changes, price changes, auth changes, migration
authority changes or workbook writes. Actual recovery snapshot and
bounded historical import remain subject to explicit owner approval.


### 2026-11-27 — deployed recovery allowance warnings; cross-device clock verification

PR #325 exact head b7ed6db8f1fffe6fcc2b6553a579867b08414d49 passed
Actions 45956040531 (127 + 699 Python tests and 14 JavaScript tests),
including synthetic authenticated recovery threshold and customer
request/approval/appointment handoff/account isolation regressions.
Squash merged as 6193a780b2de9ffb1ab7a20dfb3d7050bfc1808e.
Railway deployment fe0f3542-33dd-411e-9c76-2283c935144c SUCCESS at
08:00:46 UTC; existing /data 1024 MB volume attached, no pending
infrastructure changes. Read-only workbook metadata confirms 48 tabs.
No authenticated production browser acceptance or database record
inspection; no new domain declared authoritative.

Next slice: private no-store minimal timer status endpoint reads current
timer and saved active ledger minutes in one SQLite read transaction.
Foreground timer rechecks every 15 seconds and on returning to the page;
uses monotonic local interpolation between server reads, labels failed
verification as an estimate, and freezes stale screens if a different
session, timer revision, or job revision is detected. Explicit reload
preserves the choice about unsaved form edits; no automatic resume/pause,
no live data changes, no external communication. Align 80% threshold
and exact/over-limit display with recovery queue. Synthetic HTTP and
JavaScript regression added; exact-head CI/deployment pending. This is
foreground verification only, not background push. Actual recovery
snapshot, bounded historical import, production job identity and
customer browser acceptance still require verification/approval.


### 2026-11-27 — cross-device timer deployment; durable private alert inbox

PR #326 exact head a4ec0aecb4f745d91845990e3d26bed70e57a3bd passed
Actions 45962597049 (127 + 700 Python tests and 16 JavaScript tests).
Squash merged as f73ced044bf056685b056ee16a3f85d3b6dea15c.
Railway deployment 9baf468b-927f-4051-9ffd-ee23f01282a5 SUCCESS at
09:05:28 UTC; one running replica, original /data 1024 MB volume,
zero warnings/criticals and no pending changes. Read-only workbook
metadata confirms 48 tabs. Exact-head CI confirms customer-to-HQ
service requests, quote conversion, customer-visible approval and
account isolation. Live database content and authenticated owner
and customer browser acceptance remain unverified. No new domain
reconciled or declared authoritative.

Next slice: private durable alert inbox with explicit Check for new
alerts and Acknowledge alert controls. Scanner uses an atomic SQLite
transaction, saved unvoided minutes plus active timer seconds,
excluding breaks, and deduplicates per session, allowance and threshold
(80% and limit); skips missing allowances and avoids retroactive near
alerts after detecting limit. Records detection time, not inferred
crossing time; acknowledgment preserves first owner and timestamp.
Historical observations remain visible after allowance changes or
session closure. CLI native_work scan-timer-alerts provides a local
one-shot worker entry point; no automatic scheduling, push credentials
or outbound messages enabled. Synthetic HTTP and SQLite tests cover
authorization, CSRF, thresholds, replay, paused breaks, CLI and
acknowledgment. Exact-head CI and deployment pending. No live job,
price, payment, spreadsheet or customer-facing route changes.
Actual recovery snapshot and bounded historical import still require
separate approval with verified mappings and reconciliation.


### 2026-11-27 — durable alert inbox deployed; local background scanner

PR #327 exact head 35806964b51f6b2725469c5682c1dbab1176994f
passed Actions 45970970456 (127 + 702 Python tests, 16 JavaScript).
Squash merged as 08a1d18f86031e557c4a5740ed2c2c2e05788b3f.
Railway deployment 19dce000-42a6-4943-853d-e6a6d5d69272 SUCCESS
at 10:05:19 UTC; Gunicorn boot, one running replica and original
/data 1024 MB volume verified. No pending changes, warnings or
criticals. All 48 workbook tabs verified through read-only metadata.
Customer/quote/shop/isolation regression passed; live authenticated
browser acceptance and live database record inspection remain
unverified. No historical domain reconciled or declared authoritative.

Next slice: run the existing idempotent local scan in the existing
Gunicorn worker after initialization, every 60 seconds, no new service,
no external delivery and no application-import side effects. Requires
configured persistent storage. Retries failures without logging private
data; interruptible shutdown. SQLite stores a last-successful automatic
scan heartbeat in the same transaction as observations. Owner alert
screen distinguishes recent, stale and never-verified scans; manual
checks do not mask background failure. Synthetic tests verify alert
creation without a browser, restart-readable heartbeat, stale detection,
retry and worker lifecycle. Exact-head CI, production heartbeat and
owner acceptance pending. Phone push remains unimplemented; no
new migration or live job/price/payment/booking changes.
Recovery snapshot and first bounded import still need separately
reviewed counts, mapping, conflicts and rollback before approval.


### 2026-11-27 — automatic private timer scan deployed; owner alert triage

PR #328 exact-head 7dd37999faaf1c8e214bb15a405802f7e40fe2fc passed
Actions 45979221255 (127 + 704 Python tests and 16 JavaScript tests).
Squash merged as 731238c0a8aa1200548cc413bc2f49befbe56552.
Railway deployment 54967706-6438-4130-8cef-1043dcf7f453 SUCCESS at
11:02:26 UTC, original 1024 MB /data volume attached, no staged changes.
Runtime log at 11:02:25 UTC confirms "Private timer alert background scan
operational" (first successful scan). Continuous heartbeat over subsequent
intervals and authenticated production browser acceptance not independently
verified. Read-only workbook metadata confirms all 48 tabs. Exact-head CI
confirmed synthetic customer request, quote conversion, customer approval,
appointment confirmation, maintenance pricing isolation, and product
deposit review. No historical reconciliation or authority change.

Next slice: owner timer landing displays exact count of all unacknowledged
observations (not just visible rows), distinguishes observations matching
open sessions and allowances from historical ones, prioritizes matching
limit observations, and lets the owner acknowledge directly with existing
CSRF-protected action. Alert storage failure does not prevent timer recovery.
Observations do not represent the current clock. No new external delivery,
real booking, job/time/price/payment edits, or workbook writes by development.
Synthetic regression covers >50 observations, prioritization, authenticated
acknowledgment and failure isolation. Exact-head CI/deployment pending.
Phone push requires separately verified private subscription and delivery
controls; owner consent before any real send. Current black Kia identity
remains unverified; no creation or start. Actual off-volume recovery and
bounded historical import remain subject to separate approved manifests.


### 2026-11-27 — alert triage deployed; private device enrollment proposed

PR #329 exact-head bb90c7613a353957fd1ce4439ed7364d7227a5a4 passed
Actions 45987305871 (127 + 705 Python, 16 JavaScript tests), squash
merged as 44ce1e805b3a089fee5453da3b0f2e0fe0b3ed31. Railway deployment
80074dae-95f7-436e-b39b-2d546d64347d SUCCESS at 12:03:41 UTC; original
1024 MB /data volume remains attached, no staged infrastructure changes.
Runtime log at 12:03:39 confirms first background scan of this deployment.
Continuous heartbeat and authenticated live acceptance remain unverified
(DNS failure for both /book and /hq/timer from the development environment).
Read-only workbook metadata still lists 48 tabs; bounded Jobs read contains
J0014 (bumper) and J0073 (bonnet), neither proves current black Kia identity.

This proposal adds owner-only Apple Web Push device enrollment and revocation
under Job Timer > Labour alerts > Private phone alert setup. Disabled unless
a valid P-256 VAPID public key is configured in HQ_TIMER_PUSH_PUBLIC_KEY.
No private key generated or configured by development. Owner must explicitly
prepare and enroll from the installed iPhone app; no permission is requested
automatically. No delivery implementation, no real subscription or test
notification, no additional infrastructure. Exact Apple endpoint allowlist,
valid P-256 keys, bounded input, CSRF, owner isolation, idempotent registration,
revision-protected revocation with address/key removal and immutable audit
events. The service worker is confined to the device setup path, has no fetch
interception, and only displays a generic lock-screen message if a future
sender delivers an event. No customer or finance information in messages.
Separate reviewed sender implementation, production key management, owner
delivery consent and actual device acceptance remain required. CI/deployment
of this proposal pending; do not describe phone notifications as operational.

No live job, time, financial or booking edits or workbook writes. Existing
customer booking routes unchanged; full regression and account isolation
checks required before merge. No domain cutover, no authorised live historical
import or actual independent recovery snapshot. Next: complete enrollment
validation, opt-in safe sender/outbox, and native reconciliation/import
execution. Do not bypass owner authentication to locate the Kia.

### 2026-11-27 — deployed private device enrollment; local alert preview

PR #330 exact head dc5ed24c52e7c6f7dd6d0d1c377b80c0a55cfb7a passed
Actions 45995996307 (127 + 706 Python tests and 19 JavaScript tests).
Squash merged as 22fa96617df8097b95c6ff8bed3fea348a6c501e.
Railway deployment d6531dba-753a-441e-8e82-d07dae960a09 SUCCESS at
13:06:46 UTC, original 1024 MB /data volume attached, no pending
infrastructure work. Read-only workbook metadata confirms 48 tabs;
bounded Jobs read still does not establish current CV Motors black Kia
identity. Direct /book, /hq/timer and device setup HTTP probes failed DNS.
Synthetic customer profile, vehicle, service request, quote conversion,
appointment handoff, product order and account isolation passed in CI.
Authenticated production acceptance and live database contents unverified.

Next slice: owner-only device-specific consent to local future-alert
preview (not permission to send), revision-checked enable/disable controls
and immutable consent history. After successful background scan, a separate
transaction queues eligible new unacknowledged open-session observations
within 15 minutes, after a consent watermark; deduplicates per device
registration and alert. Cancels stale, acknowledged, superseded, revoked
or disabled entries. Preview failures do not block timer scanning. The UI
shows saved preview counts, not sent/delivered notifications. No transport,
credentials, external communication or customer data payload. Separate
explicit delivery consent and secure key configuration remain required
before any real send. Synthetic authenticated regression added; exact-head
CI and deployment pending. No live job, time, customer, finance or workbook
record changes. Recovery snapshot and first bounded historical import
still need separate owner approval and verified mappings; no new historical
authority or domain reconciliation.

### 2026-11-27 — local preview deployed; canonical timer job lookup

PR #331 exact head 01e88636a7e8e099ef086d47851824d729666b77 passed
Actions 46004448015 (127 + 707 Python tests; 19 JavaScript tests).
Merged as b387ada2fa209ba4caf315d741ddd05ca9b32292; Railway deployment
825d25b5-ed86-4ffb-896b-7ad9daf76e8e SUCCESS at 14:01:09 UTC.
Original 1024 MB /data volume attached, one running replica, no staged
changes; timer background scan startup confirmed in logs. Actual phone
delivery remains disabled, pending secure transport configuration and
separate owner consent. Read-only workbook metadata confirms 48 tabs;
bounded Jobs A1:Z1000 Kia search returned J0014 and J0073, neither
establishes the current repair identity. Direct /book, /hq/timer and
/hq/timer/alerts/push/ probes failed local DNS resolution. CI synthetic
customer vehicle, service request, quote conversion, owner approval,
customer-visible product order and account isolation passed.

Next owner-facing slice: opt-in combined-term lookup on /hq/timer searches
customer, vehicle, literal registration (including compact form), service,
repair notes and canonical ID on the same record; existing historical
J references remain searchable if linked. Results display repair scope
and full canonical ID to distinguish similar work. Existing generic
search semantics remain unchanged. Owner-only no-store route and synthetic
regression verify scope, no wildcard expansion, escaping and no timer
start or labour write. No identity inference, live job creation or
migration. Exact-head CI and deployment pending. Recovery and bounded
historical imports remain separate approvals; no domain authority changed.


### 2026-11-27 — canonical lookup deployed; paginated lookup and native draft entry

PR #332 exact head 8d51f114a0f96c288fdfaa6458b52b4a438dbe75 passed
Actions 46012325577 (127 + 708 Python tests; 19 JavaScript tests).
Squash merged as d9420107223646dd55de3b2e4b52235181b9b09f; Railway
deployment e4ffce87-3ac8-4b14-af93-a3b9968965cc SUCCESS at 15:00:29 UTC.
Original 1024 MB /data volume attached and no pending changes.
Read-only workbook metadata confirms 48 tabs; Jobs read again finds
J0014 and J0073, not verified current black Kia identity. Synthetic
combined quote appointment handoff and customer account isolation passed.
Authenticated live acceptance and database record inspection remain unverified.

Next slice: owner timer search can page through all bounded matching records
and explicitly enter the existing native job draft creation workflow, with
job preselected across customer lookup. Same-customer duplicate nonblank
registrations (ignoring case and spaces) are rejected atomically rather than
silently cloning the vehicle; existing vehicle selection permits subsequent
work. Preserve selected customer when recovering from a validation error.
Draft creation does not approve a price, book work, start timing, or write to
Sheets. Synthetic tests cover save/reload, collision rollback, reuse, access
control and pagination beyond 50. Exact-head CI and deployment pending.
No new historical domain reconciled or authoritative. Actual recovery and
bounded historical import still require separate owner approval; phone
delivery requires secure transport and explicit consent. No production job
creation, timer start, customer communications or workbook writes by this run.


### 2026-11-27 — native draft creation deployed; direct draft price and approval

PR #333 exact head 8dd5f3a18f7b41f10a827f2b6589d79bfa316edf passed
Actions 46021167703 (127 + 710 Python tests; 19 JavaScript tests).
Squash merged as 2a0ece880370c27de83292355ee86076293d3944; Railway
16684d1d-299a-4d07-91d2-339a5fdebc89 SUCCESS at 15:58:18 UTC.
Read-only workbook metadata still lists 48 tabs. Existing customer portal
service request, combined quote appointment handoff and account-isolation
regressions passed. Live authenticated acceptance remains unverified.

Next slice adds a private exact-record draft review page reached from the
native work editor and native draft job timer search. Save GBP proposed price
and planned deposit via existing canonical revision-checked operations; mark
ready, explicitly confirm approval, and open the same job timer without
starting it. Existing financial validation, historical reconciliation blocks,
archive checks, audit trail and Sheets link boundaries remain in force.
Synthetic HTTP tests exercise save/reload, price validation, stale edits,
owner-only access, same-ID quote conversion and no automatic timing.
No actual job, customer, payment, appointment, message or workbook write;
no recovery or migration approval granted; no domain authority changed.
Exact-head CI and deployment of this slice still pending.


### 2026-11-27 — exact-record price approval deployed; workshop estimate handoff

PR #334 exact head 3e7899f75fc2675bad9ca1ee04dee88c84c6fb0e passed
Actions 46029722541 (127 + 712 Python tests; 19 JavaScript tests).
Squash merged as 033417e437738eae2d1a76a3c702ea6529aaa560; Railway
20d5c0aa-0dbc-4acb-b970-a99fff081325 SUCCESS at 17:01:12 UTC.
Original 1024 MB /data volume attached; no pending environment changes.
Server listening and private background timer scanner startup confirmed.
Read-only workbook metadata returned all 48 tabs. CI includes synthetic
customer service requests, quote approval, appointment handoff, maintenance
price privacy and account isolation. Authenticated production data and
current Kia canonical identity remain unverified.

Next slice: exact native draft review saves estimated total minutes,
estimated materials in GBP and planned hourly sale rate separately from
customer price, actual time, internal hourly cost, actual materials, invoices
and receipts. Optional unknowns remain null. Explicit zero is distinguishable.
Canonical work revision and immutable audit protect changes, identical replay
is a no-op, archived/approved/historical drafts are protected. First timer
suggests saved planned minutes; subsequent saved session allowances take
precedence, including an intentional blank. Opening the timer does not start
work. Synthetic authenticated tests added; exact-head CI/deployment pending.
No live records, financial entries, bookings, messages, workbook cells or
authority mapping changes. Off-volume recovery snapshot, bounded historical
import, actual reconciliation and opt-in push delivery still outstanding.

### 2026-11-27 — workshop plans deployed; saved-work variance

PR #335 exact head ef314bdb1d219d4156338bb9fac1f0fd737673bd passed
Actions 46038627825 (127 + 714 Python tests; 19 JavaScript tests).
Squash merged as 7f3e4d50d08563f4c91f6d52f1ffd8a663a65274; Railway
229fcbc6-3615-4985-b8e0-a861810129f4 SUCCESS at 18:05:18 UTC.
Original 1024 MB /data volume attached; no pending environment changes.
Gunicorn listening and private timer scanner startup confirmed. Workbook
metadata read-only verified 48 tabs. No live records or workbook writes.

Next slice compares saved plan against active recorded minutes and material
allocations in the same private cost snapshot. Distinguishes planned sale
rate from internal cost and approved/proposed price from receipts. Unknown
and absent actual entries do not become zero, unsaved timer time is excluded,
and no final profit or migration reconciliation is inferred. Direct links to
existing editable ledgers. Synthetic HTTP access and calculation regression
added; exact-head CI and deployment pending. Customer Bookings unchanged;
owner and customer authenticated production acceptance remains unverified.
No domain newly authoritative. Independent recovery copy, bounded historical
import approval and reconciled native finance remain outstanding.


### 2026-11-27 — saved-work comparison deployed; exact transaction classification

PR #336 exact head 6b26e80238dc762827350dc349a28881be7f6a36 passed
Actions 46046895472 (127 + 715 Python tests; 19 JavaScript tests).
Squash merged as a32eeb9bc9a7ad4acb46b223d6d7d269db5b0e67.
Railway deployment 58f1e12d-0f03-4de9-8cc7-7b6ef3c4a76c SUCCESS at
18:59:08 UTC. Original 1024 MB /data volume attached, no staged changes.
Gunicorn and private timer scanner startup confirmed. Workbook metadata
read-only still lists 48 tabs. Synthetic customer service request, quote
conversion, appointment handoff, maintenance pricing and customer isolation
tests passed. Direct /book and /hq/timer probes fail DNS; authenticated
production acceptance and current Kia canonical identity remain unverified.

Next slice: finance monthly classification report exposes a private exact
record category/review editor for any active unsplit manual transaction,
including records outside the latest 200 in the command centre. Date, amount
and description cannot be changed by this action. Existing revision, archive
and split guards, explicit direction validation, immutable before/after
history and audit; immediate identical retry is non-mutating. No new
transaction or bank evidence, no settlement or workbook write. Synthetic
HTTP regression added; exact-head CI and deployment pending. No historical
domain becomes authoritative. Actual recovery copy, bounded import approval
and verified allocation reconciliation remain outstanding. No live job,
timer, price, financial or customer record edited by development.


### 2026-11-27 — exact classification deployed; outstanding review queue

PR #337 exact head 0eb71c102c3ca9f7f00125b83792dd5e9f90255b passed
Actions 46053528675 (127 + 716 Python tests, 19 JavaScript tests).
Squash merged as 45769e5069b5ace3af90817dab0859c5d3c67ffc.
Railway deployment 4f6a86ce-fca9-444f-bc01-57771b6be292 SUCCESS at
20:04:08 UTC. Existing /data 1024 MB volume remains configured, no staged
infrastructure changes. Read-only workbook metadata confirms 48 tabs.
Synthetic customer service request to native work, approved combined quote
appointment handoff and customer isolation passed. Owner/customer production
acceptance and actual database record inspection remain unverified.

Next slice: monthly manual transaction report includes a filter for
outstanding classifications (unreviewed unsplit entries or any unclassified
line) and stable ordering with 50-entry pages. Full month totals remain
unfiltered, so no hidden or duplicated amounts. Exact-record classification
editor now displays its latest 100 captured transaction states, original
description/date/signed amount, category, review flag and revision; direct
return to outstanding review queue. Fully categorised splits remain proposals,
not verified receipts or a parent review. Synthetic HTTP tests cover
pagination, persistent edit, stale/replayed saves in existing tests,
unclassified split review semantics and exact history. CI and deployment
of this proposal pending. No live data or workbook writes; no domain
authority change. Actual bank settlement, reconciled receipt allocations,
recovery snapshot, bounded historical import and owner acceptance still open.


### 2026-11-27 — classification queue deployed; duplicate allocation guard

PR #338 exact head 0fc2d52045284d3600a7f0e908c41349efcf1671 passed
Actions 46061133079 (127 + 717 Python tests and 19 JavaScript tests).
Squash merged as 0f869e1adfd506d11b94db6b5afc4a366f42e2ca.
Railway deployment 02e466a0-e794-4a72-9d11-2804d3d9cda7 SUCCESS at
20:59:45 UTC; existing /data 1024 MB volume attached, no pending infrastructure
changes. Runtime confirms timer alert scan operational at 20:59:43 UTC.
Read-only workbook metadata confirms 48 tabs. Synthetic owner/customer quote,
appointment acceptance and isolation tests pass. Direct production /book and
/hq/timer checks could not resolve the hostname in the development environment.
Authenticated production acceptance and canonical current Kia ID remain open.

Proposed next slice: owner-reported invoice allocation save rejects another
active allocation on the same invoice with matching date, exact pence and
nonempty evidence reference (case-insensitive ASCII; surrounding spaces ignored).
Atomic check inside existing BEGIN IMMEDIATE; idempotent operation replay,
reversal and correction, distinct instalments, and provider-specific guards
remain separate. Existing historical duplicates are not rewritten. No-reference
payments and nonidentical records still need review; this is not bank verification.
Synthetic HTTP and concurrent distinct-operation regressions added. CI/deployment
pending. No new historical import or domain authority, no actual jobs, payments,
financial data or workbook writes. Recovery snapshot and first bounded import
still require exact preview and owner approval.


### 2026-11-27 — exact receipt guard deployed; uncertain browser-save retry

PR #339 exact head 083f3a18e0ba6a1782f4ab753f8f76f6bd7036f2 passed
Actions 46067425291 (127 + 719 Python tests and 19 JavaScript tests).
Squash merged as bd6dc2dddc672a9bbe2136f84ca2143d01f2a5b0.
Railway deployment 00dbbb59-78f8-41b9-8b0e-b2ce8c1f2711 SUCCESS at
21:58:35 UTC; original /data 1024 MB volume attached, no pending work.
Workbook metadata confirms 48 tabs, read only. Synthetic owner/customer quote,
appointment handoff and account isolation, job timer pause and device sleep
regressions passed. Direct production /book and /hq/timer still fail DNS from
the development environment. Current Kia identity and authenticated acceptance
remain unverified.

Next proposed slice: keep an identical allocation ID and operation token in
memory if a browser save has an uncertain response, including a lost response
after successful server commit and no-reference claims. Lock edits while
outcome unknown; retry same request; distinguish confirmed save with failed
refresh and require reload instead of resubmission. No personal information is
persisted in browser storage; cross-page/restart recovery still requires
checking saved entries before retrying. Synthetic JS and SQLite replay tests
added; CI/deployment pending. Owner-reported entries remain unverified; no
new domain authority, historical data import, payment or workbook writes.
Next: actual source-evidence matching and approval-gated recovery/import.


### 2026-11-27 — uncertain allocation retry deployed; full-ledger private search

PR #340 exact head ad447d4f43b205722769072bac88bc106dfe247b passed Actions
46073801911 (127 + 720 Python tests, 22 JavaScript tests). Squash merged as
ebebcf267042496340e2640cbaa5b675e98f0e1d. Railway deployment
cf05e27e-2c81-4c29-b706-f84189306add SUCCESS at 23:04:24 UTC; existing
1024 MB /data volume 540528d1-3953-4737-9d92-3c861575c5d4 mounted, no staged
infrastructure changes in environment inventory. Workbook metadata remains
48 tabs, Monzo Live Feed A7:U7 schema confirmed read-only. Synthetic timer
server-time, breaks, multi-day replay, saved correction and customer-to-HQ
quote/vehicle/product review and isolation tests passed. Direct production
/book, /hq/timer and allocation page still fail DNS in this environment.
No confirmed identity for current Kia, authenticated acceptance or actual
snapshot/import approval; no new domain authority.

Next proposed slice: owner-only private search across all native invoices,
allocation references and IDs before display limits, including older entries.
Search uses POST + CSRF + no-store, not a URL containing customer names or
references. Full invoice totals are calculated across every active reported
allocation, independent of filtered display; matching history and bounded
results are labelled. Search preserves unresolved saves and prevents
discarding an unsaved edit. Older entries remain editable with existing
revision and reversal guards. Synthetic fixture tests cover older record
lookup outside the recent 500/200 limits, literal wildcard handling,
security, full totals and persisted edit. CI/deployment pending.
Reported claims remain unverified, no actual finance record, booking,
external message, payment, workbook or historical import performed.
Next: actual source-evidence reconciliation, bounded historical approval and
off-volume recovery; customer authenticated acceptance still open.


### 2026-11-28 — payment search deployed; direct provider evidence review

PR #341 head b306efc40b1e11d5f27b3ff9e91b9d8f75424886 passed Actions
46080506350 (127 + 721 Python tests, 22 JavaScript tests). Squash merged as
7ffc1e1b6ee1eeece7f5bf9f0cde913e465b7446; Railway deployment
bc182098-9e34-40c5-93d8-f1be616b96a2 SUCCESS 00:02:00 UTC. Railway inventory
retains the existing 1024 MB /data volume and no staged infrastructure changes.
Read-only workbook metadata returned all 48 tabs and Monzo Live Feed A7:U7
headers. Customer quote/appointment handoff and account isolation tests passed.
No new domain authority or live historical import; authenticated UI, current
Kia identity and independent recovery snapshot approval remain outstanding.

Next slice: direct navigation from any saved card allocation to its exact
provider recognition and fee review, including records beyond recent display
limits. Filter all local candidate/recognition/fee/history queries by the exact
opaque allocation ID before limits, retain focus after save or withdrawal,
and keep owner-only access, CSRF and live provider preview/approval guards.
Opening the focused page makes no provider requests. No bank-settlement or
new cash claim. Synthetic route assertions added; CI and deployment pending.
No live business records or workbook cells modified. Next: evidence-based
settlement and bounded historical approval; authenticated customer acceptance.


### 2026-11-28 — provider review deployed; exact settlement proposals

PR #342 exact head 047a9ce07aaea41449b7342b5e8f5195b8ecba1c passed
Actions 46085783618 (127 + 721 Python tests, 22 JavaScript tests). Squash
merged as f061361c3f8f5540a6730321f550409e9d3597fb; Railway deployment
53181b2f-7f59-44a3-b2b6-ffc918da6e48 SUCCESS 01:05:54 UTC.
Environment inventory before merge retained original 1024 MB /data volume
and no pending changes; read-only workbook metadata returned all 48 tabs.
No actual job, timer, financial record, payment, booking or historical
import performed; no new domain authority.

Next slice: direct owner-only single-payment settlement proposal navigation
from any saved card allocation; scope retained proposal history by opaque
allocation ID before the 50-event display cap; preserve focus after approval
and withdrawal. Independent withdrawal tokens per active proposal. Opening
a focused review does not call the source or provider; original explicit
approval, source evidence digest, replay and conflict protections remain.
Reject a documented combined fee total below the subtotal of already known
non-negative provider fees, even if other member fees are missing. Synthetic
HTTP and fee evidence regression assertions added. CI/deployment pending.
Settlement proposals remain unverified, never bank cash or a new receipt.
Authenticated customer and workshop acceptance, current Kia canonical ID,
first bounded import and independent recovery snapshot remain outstanding.


### 2026-11-28 — settlement focus deployed; active combined settlement queue

PR #343 exact head 48da88a3a38f73074e98c569315ef0dac07214b9 passed
Actions 46091441785 (127 + 721 Python tests, 22 JavaScript tests). Squash
merged as 9d522d5d7d0bc3fa62be53e55d00f9bf5b9c083a; Railway deployment
255f57d9-0a73-4f8c-a7bb-70b9429b4343 SUCCESS 01:58:30 UTC. Runtime logs
confirmed worker and private timer scanner startup. Environment inventory
retained the 1024 MB /data mount; read-only workbook metadata returned 48 tabs.
No actual historical import, recovery snapshot or domain authority established.

This slice adds owner-only active combined Square settlement proposal queue
and cursor-paged history beyond 50 events, using only local retained records
on GET. Older active proposals remain withdrawable even after later unrelated
history. Replaying an original single or combined save after explicit
withdrawal now returns a conflict rather than false success; new preview
and operation required. Synthetic HTTP regression assertions cover the
older active record, pagination, authorization, source isolation and replay.
CI and deployment pending for this slice. No new cash/receipt verification,
real job, working time, financial record, booking or Sheets mutation.
Authenticated production acceptance, current Kia identity, independent
recovery snapshot and exact bounded import approval remain unverified.
