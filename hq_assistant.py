"""Private assistant action planner: safe read-only foundation, no autonomous writes."""
import re

INTENTS = (
    ("email", ("email", "emails", "inbox", "reply", "message"), "Review correspondence", "Gmail access and action permissions are not yet wired into the HQ assistant."),
    ("calendar", ("book", "appointment", "calendar", "schedule"), "Review appointments", "Google Calendar has a read-only check; creating appointments is not enabled."),
    ("quote", ("quote", "estimate", "price"), "Prepare a quote", "Use HQ pricing calculations and obtain owner approval before sending."),
    ("payments", ("payment", "invoice", "overdue", "owed", "debt"), "Review receivables", "Payment and invoice records must be connected and verified."),
    ("customer", ("customer", "client", "follow up"), "Review customer records", "Customer record edits require a connected verified data source."),
    ("daily", ("today", "daily", "morning", "sort out my business"), "Daily operations review", "Live inbox, booking and finance sources are not all connected."),
)
SENSITIVE = ("send", "pay", "refund", "delete", "cancel", "transfer", "legal", "creditor", "court", "hmrc", "publish")
def plan_request(data):
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    prompt = data.get("message", "")
    if not isinstance(prompt, str) or not 3 <= len(prompt.strip()) <= 2000:
        raise ValueError("Message must contain 3 to 2000 characters")
    lower = prompt.lower()
    def contains(word):
        return bool(re.search(r"(?<![a-z])"+re.escape(word)+r"(?![a-z])", lower))
    matched = [x for x in INTENTS if any(contains(w) for w in x[1])]
    if not matched:
        matched = [("general", (), "Clarify your request", "No connected action is available for this request yet.")]
    sensitive = any(contains(w) for w in SENSITIVE)
    return {
        "mode": "safe_planning_preview", "assistant_model_connected": False,
        "message": "I can prepare a plan, but I have not performed any external action.",
        "tasks": [{"category": key, "title": title, "status": "not_executed",
                   "next_step": note, "approval_required": sensitive or key in ("quote", "payments")}
                  for key, _, title, note in matched],
        "approval_required": sensitive,
        "actions_executed": 0,
        "audit_note": "No email sent, booking changed, payment made or customer record modified.",
    }
