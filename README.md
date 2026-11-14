# Tommy Tints HQ

Dedicated, isolated business operations application. **Portfolio HQ is not involved.**

## Initial deployment
Flask/Gunicorn on Railway. `/health` provides readiness. The landing dashboard is an initial UI shell with placeholder data only.

## Security
**Do not enter customer, financial, creditor or legal information yet.** This first version has no authentication or production database. Set the GitHub repository to PRIVATE before integrating real data. Add authenticated roles, audit logs, CSRF protection, encrypted secrets and PostgreSQL before real integrations.

## Next stages
1. PostgreSQL and authentication.
2. Import/mapping from existing Tommy Tints HQ Google Sheets (read-only verification first).
3. Quotes/jobs/customer CRM with price engine.
4. Calendar/Square and DNA/Monzo integrations after permission and reconciliation tests.
5. Financial, creditor and legal dashboard with access controls.
