"""Evidence-based, owner-only connection and reconciliation readiness.

Never infer a provider's permission, read result, bank cash or reconciliation
from the existence of adapter code or a configured secret. The status page
does not invoke provider APIs or create local databases.
"""
from hq_integration_health import valid_observation_time as _observation_time

# Keys match integration_health provider evidence, not marketing product names.
PROFILES = (
    {"key": "sheets", "name": "Google Sheets jobs and pricing", "implemented": True,
     "persisted_read": False,
     "setup": "Configure Google read-only OAuth or HQ_GOOGLE_SERVICE_ACCOUNT_JSON on Railway; grant workbook access.",
     "read": "Connect Google Sheets in HQ, then open Jobs and load the workbook read-only.",
     "reconcile": "Verify job and cost-library mappings against the actual workbook; do not assume its values are bank receipts."},
    {"key": "maintenance_sheets", "name": "Google Sheets maintenance data", "implemented": True,
     "persisted_read": False,
     "setup": "Configure owner Google Sheets read-only access and verify maintenance workbook permissions.",
     "read": "Open the private Maintenance page and manually read the maintenance workbook.",
     "reconcile": "Verify actual customer/visit IDs, service dates and package balances against the source; no route or booking is confirmed."},
    {"key": "finance", "name": "Google Sheets finance", "implemented": True,
     "persisted_read": False,
     "setup": "Configure Google OAuth or the read-only Sheets service account and access to the actual finance workbook.",
     "read": "Open the private Finance Command Centre and manually refresh the spreadsheet.",
     "reconcile": "Reconcile dated bank evidence, transfers, liabilities and actual receipts before presenting verified cashflow."},
    {"key": "gmail", "name": "Business Gmail enquiries", "implemented": True,
     "persisted_read": True,
     "setup": "Configure Google OAuth/Gmail API and consent for info@tommytintsltd.co.uk; isolate legal/debt mail.",
     "read": "Apply HQ-Enquiries only to genuine customer mail; import in the owner-only Gmail enquiry review.",
     "reconcile": "Review imported enquiries against original message IDs; never automatically send or create bookings."},
    {"key": "calendar", "name": "Google Calendar availability", "implemented": True,
     "persisted_read": False,
     "setup": "Configure Google OAuth and Calendar read-only consent.",
     "read": "Connect Calendar in HQ and run an owner-initiated conflict check in Maintenance.",
     "reconcile": "Create and test canonical HQ appointments with exact provider IDs, versions and owner approval; no external writes."},
    {"key": "square", "name": "Square bookings", "implemented": True,
     "persisted_read": True,
     "setup": "Configure HQ_SQUARE_READ_TOKEN with APPOINTMENTS_READ/APPOINTMENTS_ALL_READ and HQ_SQUARE_LOCATION_ID on Railway.",
     "read": "Manually refresh the Square booking snapshot in Integration Health.",
     "reconcile": "Verify booking versions against canonical HQ appointments before any owner-approved outbound action."},
    {"key": "square_payments", "name": "Square payment evidence", "implemented": True,
     "persisted_read": True,
     "setup": "Configure HQ_SQUARE_READ_TOKEN with PAYMENTS_READ and HQ_SQUARE_LOCATION_ID on Railway.",
     "read": "Manually refresh owner-only Square Payment Evidence and compare exact payment and job references.",
     "reconcile": "Implement audited owner-approved allocations plus refunds, fees and bank-settlement evidence; comparisons are not receipts."},
    {"key": "monzo", "name": "Monzo transaction reconciliation", "implemented": False,
     "persisted_read": False,
     "setup": "Offline fixture-tested canonical Monzo transaction contracts exist, but no verified real export mapping, ingestion or bank read exists; spreadsheet balances are not bank proof.",
     "read": "Obtain a sample schema under owner control, verify exact Monzo CSV/Sheets column mappings, then stage a read-only import preview.",
     "reconcile": "Reconcile opening/closing balances and separate transfers, director funding, taxes and liabilities from revenue."},
    {"key": "dna", "name": "DNA Payments reconciliation", "implemented": False,
     "persisted_read": False,
     "setup": "Offline fixture-tested canonical DNA event contracts exist, but no verified DNA export mapping, authenticated adapter or provider permission exists.",
     "read": "Verify the real DNA report format and map distinct sale, refund, chargeback, fee and batch IDs to the offline contract; do not infer bank settlement.",
     "reconcile": "Match payment IDs, chargebacks, refunds, processor fees and batched settlements to actual bank entries."},
    {"key": "maps", "name": "Google Maps and Routes", "implemented": True,
     "persisted_read": False,
     "setup": "Owner approval for billed Maps APIs is required; configure HQ_GOOGLE_MAPS_BROWSER_KEY and HQ_GOOGLE_MAPS_API_KEY with restrictions.",
     "read": "After approved billing/key setup, request routes for verified full addresses from the private maintenance planner.",
     "reconcile": "Verify addresses, mileage and service travel costs; route estimates never confirm a customer appointment."},
)


def _stage(value):
    if value is True:
        return "Observed / present"
    if value is False:
        return "Not yet"
    return "Unverified"


def build_readiness(health, signals):
    """Create a conservative, owner-scoped snapshot from already-read evidence.

    signals holds configuration/consent presence only, never secret values.
    A historical successful read is not proof that a connection still works.
    A successful persisted provider snapshot is not a job/bank reconciliation.
    """
    by_name = {entry["name"]: entry for entry in health.get("integrations", [])}
    old_names = {
        "sheets": "Google Sheets", "maintenance_sheets": "Maintenance Sheets",
        "finance": "Sheets finance",
        "gmail": "Gmail", "calendar": "Google Calendar",
        "square": "Square", "square_payments": "Square payments",
    }
    history_available = health.get("history_available") is True
    result = []
    for profile in PROFILES:
        key = profile["key"]
        previous = by_name.get(old_names.get(key), {})
        signal = signals.get(key, {})
        implemented = profile["implemented"]
        configured = bool(signal.get("configured")) if implemented else False
        # OAuth grant presence is separate from the result of an API call.
        # API key presence is NOT proof of provider authorisation.
        # A locally stored refresh grant only establishes that an encrypted
        # owner-scoped grant was found. It does not prove that Google currently
        # accepts the grant, or even that a provider read has ever succeeded.
        consent_present = signal.get("consent") if configured else False
        if consent_present not in (True, False, None):
            consent_present = None
        raw_success = previous.get("last_success") if history_available else None
        success = _observation_time(raw_success)
        read_evidence_invalid = (previous.get("observation_invalid") is True
                                 or raw_success is not None and success is None)
        last_error = previous.get("last_error") if history_available else None
        read_tested = bool(success) if history_available else None
        # Read-confirmed authorisation is historical and provider-specific.
        # Token-only providers cannot claim it from secret configuration.
        # A saved OAuth grant without a recorded successful read stays unknown.
        authorised = (True if consent_present is True and read_tested is True
                      else False if consent_present is False else None)
        # Historical upstream reads cannot establish that storage committed.
        # New explicit markers are recorded only after each import/snapshot
        # operation returns successfully; legacy records remain unverified.
        raw_sync = (health.get("sync_successes", {}).get(key)
                    if history_available and profile["persisted_read"] else None)
        sync_success = _observation_time(raw_sync)
        sync_evidence_invalid = raw_sync is not None and sync_success is None
        sync_tested = (bool(success) and bool(sync_success)
                       and profile["persisted_read"]) if history_available else None
        sync_observation = (health.get("sync_failures", {}).get(key, {})
                            if history_available and profile["persisted_read"] else {})
        sync_needs_attention = sync_observation.get("needs_attention") is True
        sync_error = (sync_observation.get("last_error")
                      if sync_needs_attention else None)
        # No provider in this register has an owner-reviewed, bank-reconciled
        # allocation chain; never manufacture this stage from amount matches.
        reconciled = False
        if not implemented or not configured:
            blocker = profile["setup"]
            next_action = profile["setup"]
        elif consent_present is False:
            blocker = "Owner OAuth consent is not available in this session or the saved grant could not be used."
            next_action = profile["read"]
        elif not history_available:
            blocker = "Integration observation storage is unavailable; previous reads and syncs cannot be verified."
            next_action = "Restore read-only access to the persistent integration health database without resetting history."
        elif read_evidence_invalid or sync_evidence_invalid:
            blocker = ("Stored integration read or sync success timestamp is invalid or future-dated; "
                       "this evidence cannot establish readiness.")
            next_action = ("Inspect the owner-only persistent integration health history and "
                           "restore trustworthy evidence before repeating a read or sync.")
        elif previous.get("needs_attention") is True:
            blocker = last_error or "Most recent upstream read failed; earlier success may be stale."
            next_action = profile["read"]
        elif sync_needs_attention:
            # Preserve historical sync-tested evidence, but never hide a more
            # recent failed persistence attempt behind that old success.
            blocker = sync_error or "Most recent persisted sync failed; historical success may be stale."
            next_action = profile["read"]
        elif not read_tested:
            blocker = "No successful upstream read has been recorded for this specific workstream."
            next_action = profile["read"]
        elif profile["persisted_read"] and not sync_tested:
            blocker = "Upstream read succeeded, but no completed persistent import/snapshot is evidenced."
            next_action = profile["read"]
        else:
            # Sheets, finance, Calendar and Maps are read-only checks: they
            # must not be instructed to perform a non-existent persisted sync.
            blocker = profile["reconcile"]
            next_action = profile["reconcile"]

        # For a configured API key, unknown authorisation is expected until a
        # real provider read has been observed; never convert it to 'true'.
        result.append({
            "key": key, "name": profile["name"], "implemented": implemented,
            "configured": configured, "authorised": authorised,
            "consent_present": consent_present,
            "authorisation_verified_at": success if authorised is True else None,
            "read_tested": read_tested, "sync_tested": sync_tested,
            "observation_integrity_issue": read_evidence_invalid or sync_evidence_invalid,
            "sync_applicable": profile["persisted_read"],
            "sync_needs_attention": sync_needs_attention,
            "sync_last_error": sync_error,
            "sync_last_error_at": (sync_observation.get("last_error_at")
                                   if sync_needs_attention else None),
            "reconciled": reconciled, "last_success": success,
            "sync_last_success": sync_success if sync_tested else None,
            "last_error": last_error, "last_error_at": previous.get("last_error_at"),
            "blocker": blocker, "next_safe_action": next_action, "owner_only": True,
            "stages": {
                **{stage: _stage(value) for stage, value in (
                    ("implemented", implemented), ("configured", configured),
                    ("read_tested", read_tested), ("reconciled", reconciled))},
                "authorised": (
                    "Historically read-confirmed (not current proof)"
                    if authorised is True else
                    "Grant present; permission unverified"
                    if consent_present is True else _stage(authorised)),
                "sync_tested": (_stage(sync_tested) if profile["persisted_read"]
                                else "Not applicable (read-only)"),
            },
        })
    return result
