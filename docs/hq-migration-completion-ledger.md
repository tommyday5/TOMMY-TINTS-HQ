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
