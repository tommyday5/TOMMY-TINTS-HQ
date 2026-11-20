# Google Maps activation — 20 November 2026

Use only `tommy-tints-ltd`. No Maps variables were present on the production service at inspection. Cloud console returned Site Unavailable; billing, API enablement, quotas and key restrictions remain unverified. No Cloud billing or API settings were changed.

## Owner setup

1. In TOMMY TINTS LTD verify billing and enabled Maps JavaScript API, Geocoding API and Routes API. Do not use My First Project.
2. Create a browser key restricted to `https://tommy-tints-web-production.up.railway.app/*`, Maps JavaScript API and Geocoding API (the map uses the JavaScript Geocoder). Set Railway `HQ_GOOGLE_MAPS_BROWSER_KEY` securely.
3. Create a separate Routes API-only server key. Set Railway `HQ_GOOGLE_MAPS_API_KEY` securely. Use an IP application restriction only with a verified stable outbound IP; do not guess a Railway inbound IP or enable paid static networking without approval. Resolve supported outbound networking before applying an IP restriction. Never share keys in chat or commit them.
4. Owner must confirm full workshop address and postcode before setting `HQ_ROUTE_BASE_ADDRESS`. Preserve `HQ_ROUTE_MILEAGE_RATE`; code default remains £0.56/mile when unset, an estimate rather than verified operating cost.
5. Review provider quotas. Billing budget/alert amounts require owner approval; app request limits do not cap Google billing. Matrix calls contain up to 100 elements each; a route request is not one uniformly priced billing unit.
6. Open owner-only `/hq/maintenance/routes`, load map explicitly (up to 40 geocodes), select valid recorded customer addresses, calculate one workshop round trip. Verify pins, stop order, returned geometry, mileage/time and contribution. No external bookings are created. Browser map/geocode usage is separate from the server request ceiling.

## Safeguards

Routes requests across both endpoints share a persistent SQLite ceiling of 20/hour and 100/day, including failed provider attempts. The counter stores timestamps only and expires after a day. Both endpoints require owner sessions and CSRF; matrix input is bounded before reserving usage. Invalid customer selections do not consume a provider request. Storage failure prevents provider calls. The matrix provider exception is not logged with secrets/addresses. Existing route estimates exclude traffic, labour, materials and overhead. Existing Google integrations and customer data are preserved.

Map/geocoder requests remain explicit and are not automatically retried; no customer coordinates are persisted in browser storage. Cross-navigation geocoding reuse remains outstanding pending provider storage-policy review. App quotas are application-level controls, not verified Google Cloud quota or billing configuration.
