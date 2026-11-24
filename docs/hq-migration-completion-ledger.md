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
