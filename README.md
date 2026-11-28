# Tommy Tints HQ

Dedicated business operations application for Tommy Tints HQ and customer Bookings. Portfolio HQ is not involved.

## Current architecture

Existing Flask/Gunicorn deployment on Railway, with canonical native SQLite records on the existing persistent volume. HQ owner authentication and customer access controls are separate. The public customer entry point is /book; the owner dashboard is private. /health provides service readiness, not proof that every integration or workflow is complete.

## Development and completion

- [Current unfinished-work backlog](docs/hq-unfinished-work-backlog.md): prioritized acceptance and implementation gaps.
- [Migration completion ledger](docs/hq-migration-completion-ledger.md): verified commits, tests, deployment evidence, recovery instructions, field ownership and approval boundaries.

The original Google workbook remains read-only during staged migration. New native records and historical source evidence must be distinguished. Do not reset production storage, overwrite the workbook, activate bidirectional sync, run real imports or change real business records without explicit owner approval. No blanket cutover has occurred.

Develop on an isolated branch with synthetic fixtures and no production credentials. Follow the repository's HQ tests workflow; merge only green exact-head changes, then verify Railway deployment and original volume. Keep access controls, audit history, idempotency and unknown-value semantics intact. Never put private recovery keys or real customer/financial data into GitHub.

The existence of a route, passing tests, or a successful deployment does not establish authenticated owner/customer acceptance or historical reconciliation. Record those separately. Finish incomplete vertical workflows before cosmetic or redundant refinements.
