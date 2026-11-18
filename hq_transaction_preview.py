"""Owner-only, memory-only inspection of explicit Monzo/DNA export mappings.

This is a PREVIEW, not a provider adapter or import. No CSV rows, references,
customer information, classifications, or payment evidence are persisted.
Column mappings must be supplied explicitly after inspection of the REAL
provider export; this module never guesses pounds vs pence or provider formats.
"""
import csv
import io
import json
import re
import secrets

from flask import Blueprint, abort, jsonify, render_template_string, request, session

from hq_auth import required
from hq_reconciliation_contracts import (
    ContractError, normalise_monzo_rows, normalise_dna_rows,
)

transaction_preview = Blueprint("transaction_preview", __name__)
MAX_TEXT = 120_000
MAX_ROWS = 500
MAX_COLUMNS = 32
_REQUIRED = {
    "monzo": ("transaction_id", "amount_pence", "currency", "booked_at"),
    "dna": ("event_id", "payment_id", "kind", "gross_pence", "fee_pence",
            "net_pence", "currency", "occurred_at"),
}
_MONEY = {
    "monzo": ("amount_pence",),
    "dna": ("gross_pence", "fee_pence", "net_pence"),
}
_MONEY_INT = re.compile(r"-?(?:0|[1-9][0-9]{0,12})\Z")


class PreviewError(ValueError):
    """An export is malformed or its claimed mapping cannot be validated."""


def preview_export(source, csv_text, *, mapping=None, scope_ref=None):
    """Examine headers, then optionally validate explicitly mapped rows in memory.

    Never return source row values, account IDs, financial totals or raw events.
    A syntactically valid preview cannot verify a source mapping, account
    ownership, payment allocation or bank settlement.
    """
    if not isinstance(source, str) or source not in _REQUIRED:
        raise PreviewError("Choose Monzo or DNA Payments")
    if not isinstance(csv_text, str) or not csv_text:
        raise PreviewError("CSV must be nonempty UTF-8 text under 120 KB")
    try:
        byte_count = len(csv_text.encode("utf-8"))
    except UnicodeEncodeError:
        raise PreviewError("CSV must be valid UTF-8 text") from None
    if (byte_count > MAX_TEXT or "\x00" in csv_text
            or "\ufffd" in csv_text):
        raise PreviewError("CSV must be nonempty UTF-8 text under 120 KB")
    if csv_text.startswith("\ufeff"):
        csv_text = csv_text[1:]
    try:
        reader = csv.reader(io.StringIO(csv_text, newline=""), strict=True)
        headers = next(reader, None)
        if (not headers or len(headers) > MAX_COLUMNS
                or len(set(headers)) != len(headers)
                or any(not h or len(h) > 80 or h.strip() != h
                       or "\n" in h or "\r" in h for h in headers)):
            raise PreviewError("CSV headers must be unique, nonempty and at most 32 columns")
        output = {
            "source": source, "headers": headers,
            "required_canonical_fields": list(_REQUIRED[source]),
            "mapping_complete": False, "canonical_preview_valid": False,
            "source_mapping_verified": False, "authorised": False,
            "provider_read_tested": False, "sync_tested": False,
            "job_payment_verified": False, "bank_settlement_verified": False,
            "reconciled": False, "records_imported": 0,
            "notice": "Schema preview only. No provider data was saved or reconciled.",
        }
        if mapping is None or mapping == {}:
            return output

        if not isinstance(mapping, dict):
            raise PreviewError("Column mapping must be an object of canonical field to CSV header")
        allowed = set(_REQUIRED[source]) | ({"batch_id"} if source == "dna" else set())
        if set(mapping) - allowed:
            raise PreviewError("Mapping contains unsupported canonical fields")
        if not set(_REQUIRED[source]).issubset(mapping):
            raise PreviewError("Mapping is missing required canonical fields")
        chosen = {key: value for key, value in mapping.items()
                  if value is not None and value != ""}
        if set(_REQUIRED[source]) - set(chosen):
            raise PreviewError("Required columns cannot be omitted")
        if (any(not isinstance(value, str) or value not in headers for value in chosen.values())
                or len(set(chosen.values())) != len(chosen)):
            raise PreviewError("Each mapped field needs a distinct exact CSV header")

        indexes = {key: headers.index(header) for key, header in chosen.items()}
        records = []
        for line_number, columns in enumerate(reader, start=2):
            if len(records) >= MAX_ROWS:
                raise PreviewError("Maximum 500 records per validation preview")
            if len(columns) != len(headers):
                raise PreviewError("CSV row column count does not match header")
            row = {key: columns[index] for key, index in indexes.items()}
            for money_field in _MONEY[source]:
                raw = row[money_field]
                if not _MONEY_INT.fullmatch(raw):
                    raise PreviewError(
                        "Mapped monetary columns must contain exact signed integer pence, not pounds")
                row[money_field] = int(raw)
            if source == "dna" and row.get("batch_id") == "":
                row["batch_id"] = None
            records.append(row)
        if not records:
            raise PreviewError("No rows to validate")
        try:
            if source == "monzo":
                result = normalise_monzo_rows(records, account_ref=scope_ref)
            else:
                result = normalise_dna_rows(records, merchant_ref=scope_ref)
        except ContractError as exc:
            # Do not reflect sensitive input rows or raw IDs into an error.
            raise PreviewError("Canonical integrity check failed: " + str(exc)) from None
        output.update(
            mapping_complete=True, canonical_preview_valid=True,
            mapped_rows=len(records), unique_events=len(result["events"]),
            exact_duplicate_rows=result["duplicate_rows"],
            notice=("Mapped records satisfy offline canonical checks only; "
                    "source mapping, payments and settlement remain unverified."),
        )
        return output
    except (csv.Error, UnicodeError):
        raise PreviewError("Malformed CSV text") from None


def _submitted_data():
    request.max_content_length = 128_000
    if request.content_length is not None and request.content_length > 128_000:
        abort(413)
    if request.is_json:
        data = request.get_json(silent=True)
        token = request.headers.get("X-HQ-CSRF", "")
    else:
        data = request.form
        token = data.get("csrf", "")
    if not secrets.compare_digest(token, session.get("csrf", "not-set")):
        abort(403)
    if not data or not hasattr(data, "get"):
        abort(400)
    mapping = data.get("mapping")
    if isinstance(mapping, str):
        if len(mapping) > 3000:
            raise PreviewError("Column mapping is too large")
        if mapping.strip():
            try:
                mapping = json.loads(mapping)
            except (ValueError, TypeError):
                raise PreviewError("Mapping must be valid JSON") from None
        else:
            mapping = None
    return data, mapping


@transaction_preview.post("/api/hq/transaction-import-preview")
@required
def api_preview():
    try:
        data, mapping = _submitted_data()
        result = preview_export(data.get("source"), data.get("csv_text"),
                                mapping=mapping, scope_ref=data.get("scope_ref"))
    except PreviewError as exc:
        return jsonify(error=str(exc), imported=False, reconciled=False), 400
    return jsonify(result), 200


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bank &amp; processor schema preview | Tommy Tints HQ</title>
<style>body{background:#0b0f16;color:#eef2f8;font:16px system-ui;margin:0}
main{max-width:820px;margin:25px auto;padding:16px}a{color:#ff8088}
section{background:#151e2b;border:1px solid #324154;padding:20px;border-radius:12px;margin:16px 0}
label{display:block;margin:14px 0 4px}textarea,input,select{box-sizing:border-box;width:100%;background:#0c121b;color:#eef2f8;
border:1px solid #546273;border-radius:7px;padding:12px}button{background:#c82737;color:white;border:0;
padding:12px 18px;border-radius:7px;margin-top:16px}code{word-break:break-word}li{margin:5px 0}
</style></head><body><main><a href="/hq/integrations">Integration health</a>
<h1>Transaction export schema preview</h1>
<p>Owner-only and memory-only. Inspect the CSV header without a mapping, then explicitly
map each canonical field to an exact source header. Never assume a money column is in
pence: a column containing pounds must not be mapped as integer pence.</p>
<section><form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label for="source">Provider</label><select id="source" name="source"><option value="monzo">Monzo bank</option><option value="dna">DNA Payments</option></select>
<label for="csv_text">CSV content, maximum 120 KB / 500 rows when mapped</label>
<textarea id="csv_text" name="csv_text" rows="7" maxlength="120000" required autocomplete="off" spellcheck="false"
placeholder="Paste an export header or a bounded CSV; contents are never redisplayed"></textarea>
<label for="scope_ref">Exact account / merchant reference (required to validate records)</label>
<input id="scope_ref" name="scope_ref" maxlength="128" autocomplete="off" placeholder="Exact reference, no spaces">
<label for="mapping">Explicit column mapping (JSON, optional for header inspection)</label>
<textarea id="mapping" name="mapping" rows="5" maxlength="3000" autocomplete="off"
spellcheck="false" placeholder='{"transaction_id":"EXACT SOURCE COLUMN","amount_pence":"EXACT SOURCE COLUMN","currency":"EXACT SOURCE COLUMN","booked_at":"EXACT SOURCE COLUMN"}'></textarea>
<button type="submit">Inspect without importing</button></form></section>
{% if error %}<section role="alert">{{ error }}</section>{% endif %}
{% if result %}<section><h2>Preview only — not reconciled</h2>
<p>{{ result.notice }}</p><p>CSV columns: {{ result.headers|join(", ") }}</p>
<p>Required canonical fields: {{ result.required_canonical_fields|join(", ") }}</p>
{% if result.canonical_preview_valid %}
<p>Canonical validation passed: {{ result.mapped_rows }} mapped rows,
{{ result.unique_events }} distinct events, {{ result.exact_duplicate_rows }} identical repeats.</p>
{% else %}<p>Mapping incomplete; no row-level validation or import was performed.</p>{% endif %}
<p>Authorisation, source mapping, provider read, sync, job receipts and bank settlement:
<strong>unverified</strong>. No balance or revenue totals have been changed.</p>
</section>{% endif %}</main></body></html>"""


@transaction_preview.route("/hq/transaction-import-preview", methods=["GET", "POST"])
@required
def preview_page():
    result, error, code = None, None, 200
    if request.method == "POST":
        try:
            data, mapping = _submitted_data()
            result = preview_export(data.get("source"), data.get("csv_text"),
                                    mapping=mapping, scope_ref=data.get("scope_ref"))
        except PreviewError as exc:
            error, code = str(exc), 400
    return (render_template_string(_PAGE, csrf=session.get("csrf"),
                                   result=result, error=error),
            code, {"Cache-Control": "no-store"})
