"""Explicit, owner-only local HQ work summary (no AI provider or PII export)."""
from customer_request_store import storage_ready
from hq_core_records import work_summary


class LocalRecordsUnavailable(Exception):
    pass


_COMMANDS = frozenset({
    "hq work summary",
    "show hq work summary",
    "show my hq work summary",
    "give me my hq work summary",
    "what is my hq work summary",
    "how many internal work records do i have",
})


def answer(message):
    """Only respond to precise local commands; all other requests use existing chat.

    Stored prices and deposits are owner-entered draft figures, not verified
    revenue, receivables or settled payments. This function makes no web calls.
    """
    if not isinstance(message, str) or not message.strip() or len(message) > 3000:
        raise ValueError("Message must contain 1 to 3000 characters")
    command = " ".join(message.casefold().split()).rstrip("?.!")
    if command not in _COMMANDS:
        return None
    if not storage_ready():
        raise LocalRecordsUnavailable("Persistent HQ records storage is unavailable; no summary was generated.")
    snapshot = work_summary()
    count = snapshot["record_count"]
    statuses = snapshot["by_status"]
    breakdown = ", ".join(
        f"{state.replace('_', ' ')}: {number}"
        for state, number in statuses.items() if number
    ) or "no records"
    draft = snapshot["priced_draft_value_pence"] / 100
    outstanding = snapshot["outstanding_recorded_pence"] / 100
    reply = (
        f"HQ internal work records: {count}. Statuses — {breakdown}.\n"
        f"Priced draft work: £{draft:,.2f}. Recorded expected balance: £{outstanding:,.2f}.\n"
        "These figures are owner-entered, not verified invoices, revenue or payments. "
        "They include any synthetic TEST jobs and exclude external Square, Sheets and bank data. "
        "No changes, messages or bookings were made."
    )
    return {
        "reply": reply,
        "mode": "hq_records_local",
        "data_source": snapshot["source"],
        "payment_verified": False,
        "actions_executed": 0,
    }
