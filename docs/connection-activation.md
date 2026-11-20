# Connection activation register — 20 November 2026

Production inspection found Google OAuth client configuration, owner session configuration, persistent /data storage and the owner-confirmed HQ_ROUTE_BASE_ADDRESS. Maps and Square variables were absent. No provider credentials were printed, inserted as placeholders or rotated. Google OAuth grant contents were not inspected; user-reported connections still need source-specific successful-read evidence. No open PRs were found at the audit baseline.

## Existing paths to activation

- Sheets jobs/pricing, maintenance and finance: existing Google read-only consent or existing service account with actual workbook access. Verify each source separately through its normal owner workflow; one successful spreadsheet read is not proof for all three.
- Gmail: existing Gmail read-only consent and HQ-Enquiries label. Import only expressly selected genuine customer mail. No send permissions or automated communications.
- Calendar: existing Calendar read-only consent, then conflict/availability check. A key does not enable external booking writes.
- Square bookings and payment evidence: configure HQ_SQUARE_READ_TOKEN and the real HQ_SQUARE_LOCATION_ID, with production read scopes APPOINTMENTS_READ/APPOINTMENTS_ALL_READ as applicable and PAYMENTS_READ. Refresh each read-only snapshot separately. Outbound booking synchronisation, financial allocations and settlement reconciliation are separate unfinished work.
- Maps: restricted HQ_GOOGLE_MAPS_BROWSER_KEY and HQ_GOOGLE_MAPS_API_KEY from tommy-tints-ltd only. Workshop address is set; API enablement, billing, quotas and restrictions remain unverified because Cloud Console is inaccessible. See maps-activation.md.
- Monzo: no production ingestion adapter. Real export/Sheets schema, provenance, source identity, idempotent staging and balance reconciliation are required. The existing schema preview and offline fixtures do not become connected by adding a key.
- DNA: no production ingestion adapter. Authorised account/report/API access, real event/fee/refund/settlement schema and source-scoped ingestion remain required. No payment requests or charges should be enabled by attaching credentials.

## Setup page

/hq/integrations/setup is owner-only, no-store, local-only. It displays environment-variable names and presence, never values, with dated evidence remaining in Integration Health. Its links open existing workflows; GET does not refresh grants, call providers, import data or mutate business records. It avoids encouraging replacement Google OAuth clients or repeated authorisation when a persistent grant exists. Configure secrets directly through Railway, never through source control or chat. No Monzo/DNA placeholder secrets are created.

The Maps readiness configuration now also requires the workshop base address; two keys alone cannot make routing configured. Configuration presence never establishes live provider access, successful reads, persisted sync or reconciliation.
