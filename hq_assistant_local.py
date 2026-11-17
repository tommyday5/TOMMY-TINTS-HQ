"""Owner-only local HQ work and quote lookups, with no AI provider call."""
import re

from customer_request_store import storage_ready
from hq_core_records import work_summary, recent_work_records, read_work_record


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
_RECENT_COMMANDS = {
    "hq recent jobs": "job",
    "show my hq jobs": "job",
    "hq recent quotes": "quote",
    "show my hq quotes": "quote",
}
_RECORD_PATTERN = re.compile(r"(?:show )?hq (?:work )?record ([0-9a-f]{32})")


def _money(pence):
    return "Not priced" if pence is None else f"£{pence / 100:,.2f}"


def _result(reply):
    return {
        "reply": reply,
        "mode": "hq_records_local",
        "data_source": "internal_work_records_only",
        "payment_verified": False,
        "actions_executed": 0,
    }


def answer(message):
    """Handle explicit local commands only, without exporting stored records."""
    if not isinstance(message, str) or not message.strip() or len(message) > 3000:
        raise ValueError("Message must contain 1 to 3000 characters")
    command = " ".join(message.casefold().split()).rstrip("?.!")
    record = _RECORD_PATTERN.fullmatch(command)
    if (command not in _COMMANDS and command not in _RECENT_COMMANDS
            and not record):
        # Do not forward malformed record lookups to the external AI provider.
        if command.startswith(("hq record ", "show hq record ",
                               "hq work record ", "show hq work record ")):
            raise ValueError("Use 'HQ record' followed by its 32-character HQ reference")
        return None
    if not storage_ready():
        raise LocalRecordsUnavailable("Persistent HQ records storage is unavailable; no records were read.")

    if command in _COMMANDS:
        snapshot = work_summary()
        count = snapshot["record_count"]
        statuses = snapshot["by_status"]
        breakdown = ", ".join(
            f"{state.replace('_', ' ')}: {number}"
            for state, number in statuses.items() if number
        ) or "no records"
        draft = _money(snapshot["priced_draft_value_pence"])
        outstanding = _money(snapshot["outstanding_recorded_pence"])
        return _result(
            f"HQ internal work records: {count}. Statuses — {breakdown}.\n"
            f"Priced draft work: {draft}. Recorded expected balance: {outstanding}.\n"
            "These figures are owner-entered, not verified invoices, revenue or payments. "
            "They include any synthetic TEST jobs and exclude external Square, Sheets and bank data. "
            "No changes, messages or bookings were made."
        )

    if command in _RECENT_COMMANDS:
        kind = _RECENT_COMMANDS[command]
        rows = recent_work_records(kind, limit=5)
        if not rows:
            return _result(f"No internal HQ {kind} records found. This is not an external bookings or invoices search.")
        lines = [f"Latest {len(rows)} internal HQ {kind} records (up to 5):"]
        for item in rows:
            prefix = "TEST · " if item["source_request_id"].startswith("TEST-") else ""
            lines.append(
                f"- {prefix}{item['service']} · {item['status'].replace('_', ' ')} "
                f"· {_money(item['price_pence'])} · HQ record {item['id']}"
            )
        lines.append(
            "To inspect a record privately, type HQ record followed by its reference. "
            "Prices are owner-entered, not verified income or payments. "
            "No contacts, bookings or payments were changed."
        )
        return _result("\n".join(lines))

    item = read_work_record(record.group(1))
    test = "SYNTHETIC TEST RECORD — " if item["source_request_id"].startswith("TEST-") else ""
    price = item["price_pence"]
    balance = _money(price - item["deposit_pence"]) if price is not None else "Not priced"
    return _result(
        f"{test}HQ {item['kind']} record {item['id']}\n"
        f"Customer: {item['name']}\n"
        f"Vehicle: {item['make']} {item['model']} {item['model_year']} · {item['registration'] or 'Registration not supplied'}\n"
        f"Service: {item['service']}\n"
        f"Status: {item['status'].replace('_', ' ')}\n"
        f"Preferred date/time (unconfirmed): {item['preferred_date'] or 'Not supplied'} "
        f"{item['preferred_time'] or ''}\n"
        f"Notes: {item['notes'] or 'None'}\n"
        f"Owner-entered price: {_money(price)}; recorded deposit (unverified): {_money(item['deposit_pence'])}; "
        f"expected balance (unverified): {balance}.\n"
        "Private local HQ lookup only. This result is not included in external AI chat context. "
        "No customer contacted, booking made or payment processed."
    )
