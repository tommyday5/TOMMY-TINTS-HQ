# Public Quick Quote + AI Sales Assistant — implementation specification

Status: PLANNED; no customer-facing AI service is active yet.

## Public experience
- Customer selects service (tints, dechrome, wrapping, bodywork, wheels, detailing, leather, PDR) and enters vehicle year/make/model, optional registration, contact details and mobile-job location.
- Upload photos with size limits, MIME validation, retention controls and consent notice.
- Guided conversational assistant asks only relevant missing questions; supports service explanations and clarifies scope.
- Quote displays estimate/range, inclusions/exclusions, confidence, assumptions, estimated duration and inspection caveat.
- Customer can request a formal quote; public estimate is not an accepted contract or guaranteed fixed price.
- Send the request into private HQ Quick Quotes; internal owner reviews/approves, then converts to Jobs. Prevent duplicate jobs.
- Once approved, future Square/Calendar booking and payment links may be offered.

## Price engine
- Deterministic source of truth: HQ Cost Library, rate settings, verified historical job actuals, minimum recovery threshold, material costs and service-specific assumptions.
- AI only proposes structured scope and labour complexity; never independently sets final price or alters minimums.
- Separate retail/trade pricing, per-service labour overrides (e.g. front tinting £70/h as owner confirmed), minimum labour, film metres, wrap vinyl, paint consumables, mobile travel, VAT status and discounts.
- Quote confidence falls when photos, measurements, vehicle variant or condition are unknown.
- Capture estimate vs actual labour and materials after completion to calibrate pricing over time; don't blindly train on outliers.
- Human approval required for atypical bodywork, lighting film and any estimate below policy minimum.

## Security and privacy prerequisites
- Owner/staff role-based authentication for internal HQ; public quote routes only expose customer-safe data.
- Server-side OpenAI API key via Railway secret; never put keys in browser or repository.
- Rate limiting, abuse protection, bounded AI costs, moderation, prompt-injection resistance and structured-output validation.
- UK GDPR privacy notice, lawful basis, consent where required, secure photo storage, deletion policy, and explicit statement if images/messages are sent to an AI provider.
- Protect contact information, registration numbers and images. No customer details in logs.
- Database: PostgreSQL with migrations, encrypted connections, backups, audit trail and clear tenancy/access controls.
- UK road-legality checks: front side window minimum 70% VLT, windscreen 75% VLT, and lighting films subject to lawful light output/colour. Don't automatically offer non-compliant treatments.
- Public GitHub repository must contain code only; no secrets or business/customer data.

## Integration sequence
1. Authenticate staff and provision database.
2. Read-only mapping of existing Google Sheets HQ, with reconciliation and explicit cutover plan.
3. Implement deterministic quote engine and internal Quick Quotes workflow.
4. Launch public quote intake with safe photo handling.
5. Add OpenAI conversational assistant using structured tool outputs.
6. Add booking/deposit integrations, notifications, analytics and actual-vs-estimated feedback.
