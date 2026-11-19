"""Conservative owner-only readiness for internal HQ/customer workflows.

These are product-development observations, not live end-to-end tests.
No reads, writes, provider traffic or database creation occur in this module.
Provider evidence belongs in hq_connection_readiness.py instead.
"""

WORKFLOWS = (
    dict(key="maintenance_packages", name="Maintenance customers and visit packages",
         implemented=True, needs_storage=True,
         code_state="Package and append-only visit ledger code present; real entitlements unverified",
         blocker="Customer entitlement ownership, prepaid balances, genuine visit history and restore recovery have not been independently reconciled.",
         next_action="Test a synthetic package purchase, visit debit, replay and correction under owner-only access; then verify real customer mappings before importing."),
    dict(key="maintenance_routes", name="Maintenance visits and route data",
         implemented=True, needs_storage=False,
         code_state="Read-only Sheets planner and optional Maps route calculations present",
         blocker="Actual maintenance sheet values, full addresses and availability have not been read-tested here; Maps billing and routing are not assumed enabled.",
         next_action="Owner to verify maintenance sheet consent and exact customer/visit rows, then optionally approve restricted billable Maps APIs."),
    dict(key="customer_accounts", name="Retail / Trade / Maintenance customer accounts",
         implemented=False, needs_storage=True,
         code_state="Retail/trade catalogue prices exist; account entitlement and private pricing isolation are incomplete",
         blocker="Verified customer account identity, tier entitlement, trade-price privacy and maintenance account access are not end-to-end proven.",
         next_action="Build synthetic account/tier access tests and owner-controlled onboarding; no public trade-price disclosure."),
    dict(key="public_requests", name="Public enquiries and booking requests",
         implemented=True, needs_storage=True,
         code_state="Bounded public request intake, owner review and internal draft conversion present",
         blocker="Request storage readiness is not proof of an actual customer submission; requested slots are not canonical confirmed appointments.",
         next_action="Test authorised fixture submission, duplicate/rate limits, privacy notice and owner review; keep external booking disabled."),
    dict(key="basket_checkout", name="Product basket and checkout",
         implemented=False, needs_storage=True,
         code_state="Catalogue and basket quote exist; payment checkout and fulfilment are not enabled",
         blocker="Customer identity, order inventory reservation, checkout authorisation, payment confirmation, fees and refunds are not reconciled.",
         next_action="Prepare synthetic order/stock/idempotency tests and an owner approval gate; do not activate charging."),
    dict(key="finance_centre", name="Finance command centre",
         implemented=True, needs_storage=False,
         code_state="Read-only Google Sheets financial mappings and planning dashboard present",
         blocker="Bank balances, calendar-month turnover, liabilities, expected deposits and verified receipts require distinct independent evidence.",
         next_action="Owner to read-test finance workbook; validate each source/formula and independently reconcile real Monzo/DNA settlements."),
    dict(key="job_approval_costing", name="Approved quote-to-job and costing",
         implemented=True, needs_storage=True,
         code_state="Internal work records, approved prices and owner-only costing snapshots present",
         blocker="Real job identifiers, complete material/labour cost and confirmed payment attribution have not been reconciled; planned deposits are not receipts.",
         next_action="Test synthetic quote approval, conversion idempotency, pricing revision and customer-safe breakdown before linking actual jobs."),
    dict(key="notifications", name="Email and SMS notifications",
         implemented=False, needs_storage=False,
         code_state="Owner draft/review pathways exist; outbound notification delivery is not enabled by this register",
         blocker="Approved message templates, consent, provider configuration, retry/audit and explicit send approval are outstanding.",
         next_action="Prepare isolated template and no-send tests; request owner authorisation before sending anything."),
    dict(key="device_security", name="Passkeys and application updates",
         implemented=True, needs_storage=True,
         code_state="Owner passkey routes and PWA update controls present",
         blocker="Real iPhone biometric/passkey lifecycle, session lock behaviour, account recovery and app update installation are not independently device-tested.",
         next_action="Run owner-controlled iPhone sign-in/lock/recovery and version update checks without changing customer data."),
)


def build_workflow_readiness(*, persistent_storage_configured):
    """Report what code supports, not guessed live activity or customer data.

    Configured here means a *declared non-temporary data path* for workflows
    requiring storage, not that the database exists, is writable or backed up.
    Non-storage workflows show N/A and must use provider readiness separately.
    """
    if persistent_storage_configured not in (True, False, None):
        raise ValueError("Persistent-storage signal must be true, false or unknown")
    items = []
    for profile in WORKFLOWS:
        needs_storage = profile["needs_storage"]
        configured = persistent_storage_configured if needs_storage else None
        blocker = profile["blocker"]
        action = profile["next_action"]
        if needs_storage and configured is False:
            blocker = "Persistent non-temporary HQ_DATA_DIR is not configured; " + blocker
            action = "Configure a persistent HQ_DATA_DIR volume safely; then " + action
        elif needs_storage and configured is None:
            blocker = "Persistent storage configuration is unverified; " + blocker
        items.append({
            "key": profile["key"], "name": profile["name"],
            "implemented": profile["implemented"], "code_status": profile["code_state"],
            "configured": configured, "authorised": None,
            "read_tested": None, "sync_tested": None, "reconciled": False,
            "last_success": None, "blocker": blocker,
            "next_safe_action": action, "owner_only": True,
            "storage_configured_only": bool(needs_storage),
            "stages": {
                "implemented": "Code present (partial)" if profile["implemented"] else "End-to-end incomplete",
                "configured": ("Path declared, not verified" if configured is True
                               else "Missing data path" if configured is False else "Not applicable / unknown"),
                "authorised": "Unverified / workflow-specific",
                "read_tested": "No live evidence tracked",
                "sync_tested": "No live evidence tracked",
                "reconciled": "Not evidenced",
            },
        })
    return items
