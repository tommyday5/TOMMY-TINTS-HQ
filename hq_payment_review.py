"""Owner-only exact-reference review of Square evidence against one HQ job.

This is a comparison, NOT reconciliation. Matching amounts cannot prove that
a payment belongs to a particular job, reached the bank, or is non-refundable.
No links, receipts, paid flags, prices, or external records are written.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
import re
import secrets
import sqlite3

from flask import Blueprint, abort, jsonify, render_template_string, request, session

from customer_request_store import storage_ready
from hq_auth import required
from hq_core_records import read_work_record
from hq_square_payments import _path, _timestamp, configured, snapshot_metadata_consistent

payment_review = Blueprint("payment_review", __name__)
_WORK_ID = re.compile(r"[0-9a-f]{32}\Z")
_PAYMENT_ID = re.compile(r"[!-~]{1,192}\Z")

# These are not permission to mark a job paid.
_NOTICE = ("This is a read-only comparison of provider evidence with an internal "
           "job. Identical amounts do not establish customer identity, invoice "
           "allocation, bank settlement or verified cash received.")


def compare_references(work_id, payment_id, now=None):
    """Read one exact Square ID and one HQ ID; never search by approximate value."""
    if (not isinstance(work_id, str) or not _WORK_ID.fullmatch(work_id)
            or not isinstance(payment_id, str) or not _PAYMENT_ID.fullmatch(payment_id)):
        raise ValueError("Enter valid exact HQ job and Square payment references")
    job = read_work_record(work_id)  # Validation and lookup; NEVER return customer PII.
    result = {
        "work_id": work_id, "payment_id": payment_id,
        "job_status": job["status"], "snapshot_current": False,
        "payment_evidence_found": False, "provider_status": None,
        "provider_amount_pence": None, "reported_refund_pence": None,
        "provider_currency": None, "price_pence": None, "planned_deposit_pence": None,
        "matches_approved_price": False, "matches_planned_deposit": False,
        "candidate_evidence_complete": False,
        "payment_verified_for_job": False, "bank_settlement_verified": False,
        "job_marked_paid": False, "verified_job_receipts_pence": None,
        "verified_job_outstanding_pence": None, "notice": _NOTICE,
        "warnings": [],
    }
    warnings = result["warnings"]
    if (job.get("source_request_id") or "").startswith("TEST-"):
        warnings.append("Synthetic TEST jobs must not be matched to real payments.")
    if job["status"] not in ("approved", "in_progress", "completed") or job["kind"] != "job":
        warnings.append("HQ job must have an approved selling price and active job status.")
    price, deposit = job.get("price_pence"), job.get("deposit_pence")
    valid_price = (type(price) is int and type(deposit) is int
                   and 0 <= deposit <= price <= 100000000)
    if not valid_price:
        warnings.append("Approved HQ job price or planned deposit is missing or invalid.")
    else:
        result["price_pence"] = price
        result["planned_deposit_pence"] = deposit
    if not configured() or not storage_ready() or not _path().exists():
        warnings.append("Current Square payment evidence is unavailable; no payment can be inferred.")
        return result

    # Read snapshot metadata and payment in one SQLite transaction. A missing
    # reference in this bounded 30-day snapshot does NOT prove non-payment.
    with closing(sqlite3.connect(_path().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        meta = db.execute("SELECT location,refreshed_at,window_start,window_end,item_count FROM snapshot WHERE id=1").fetchone()
        if not meta:
            warnings.append("No complete Square snapshot available.")
            return result
        # The payment review must enforce the SAME scope isolation as the
        # Square evidence list. A warning alone is insufficient: returning
        # an old seller's status/amount could misattribute it to an HQ job.
        stored_location = meta["location"]
        active_location = os.environ.get("HQ_SQUARE_LOCATION_ID", "")
        if (not isinstance(stored_location, str)
                or not stored_location.isascii() or not active_location.isascii()
                or not secrets.compare_digest(stored_location, active_location)):
            warnings.append("Square location has changed since this snapshot; refresh before reviewing.")
            return result
        if not snapshot_metadata_consistent(db, meta):
            warnings.append("Square snapshot metadata or row coverage cannot be verified; refresh before reviewing.")
            return result
        evidence = db.execute(
            "SELECT id,status,total_minor,refunded_minor,currency FROM payments WHERE id=?",
            (payment_id,),
        ).fetchone()

    observed_at = datetime.fromisoformat(_timestamp(meta["refreshed_at"]))
    age = (now or datetime.now(timezone.utc)) - observed_at
    result["snapshot_current"] = timedelta(0) <= age <= timedelta(hours=24)
    if not result["snapshot_current"]:
        warnings.append("Square snapshot is stale; refresh before reviewing.")

    if evidence is None:
        warnings.append("This exact Square ID was not found in the bounded snapshot; this is not proof of non-payment.")
        return result

    result["payment_evidence_found"] = True
    result["provider_status"] = evidence["status"]
    result["provider_currency"] = evidence["currency"]
    result["provider_amount_pence"] = evidence["total_minor"]
    result["reported_refund_pence"] = evidence["refunded_minor"]
    if evidence["status"] != "COMPLETED":
        warnings.append("Square does not report this payment as completed.")
    if evidence["currency"] != "GBP":
        warnings.append("Payment currency is not GBP; amounts cannot be compared.")
    if (type(evidence["total_minor"]) is not int or
            not 0 <= evidence["total_minor"] <= 10**12):
        warnings.append("Provider amount is invalid.")
    if evidence["refunded_minor"] is not None:
        if (type(evidence["refunded_minor"]) is not int or
                not 0 <= evidence["refunded_minor"] <= evidence["total_minor"]):
            warnings.append("Reported refund amount is invalid.")
        elif evidence["refunded_minor"] > 0:
            warnings.append("Square reports a partial or full refund; total is not net receipts.")
    if valid_price and evidence["currency"] == "GBP" and type(evidence["total_minor"]) is int:
        result["matches_approved_price"] = evidence["total_minor"] == price
        # Zero planned deposits must never be offered as a payment match.
        result["matches_planned_deposit"] = deposit > 0 and evidence["total_minor"] == deposit
        if not (result["matches_approved_price"] or result["matches_planned_deposit"]):
            warnings.append("Amount does not equal the approved price or planned deposit; partial payments may exist.")

    # The internal job was read before the Square SQLite snapshot. A price,
    # planned deposit, approval or revision may change during that comparison.
    # Re-read the authoritative HQ record before offering even a *candidate*
    # amount match. Two reads are not an atomic cross-database snapshot; they
    # only screen revisions that changed during this request.
    latest_job = read_work_record(work_id)
    identity_fields = ("revision", "status", "kind", "price_pence",
                       "deposit_pence", "source_request_id")
    if any(latest_job.get(field) != job.get(field) for field in identity_fields):
        warnings.append("HQ work record changed during payment comparison; refresh the job and retry.")
        result.update(job_status=None, price_pence=None,
                      planned_deposit_pence=None, matches_approved_price=False,
                      matches_planned_deposit=False)
        return result
    result["candidate_evidence_complete"] = (
        not warnings and result["snapshot_current"] and valid_price
        and (result["matches_approved_price"] or result["matches_planned_deposit"])
    )
    # Never return a "verified" job receipt or modify any financial balance.
    return result


def _csrf_ok(value):
    return isinstance(value, str) and secrets.compare_digest(value, session.get("csrf", "not-set"))


def _unique_json_fields(pairs):
    """Reject ambiguous duplicate JSON member names, including nested ones."""
    parsed = {}
    for name, value in pairs:
        if name in parsed:
            raise ValueError("Duplicate review instruction")
        parsed[name] = value
    return parsed


def _review_input():
    if request.is_json:
        # Authorisation precedes any JSON parsing, including duplicate-key
        # checks. An absent Content-Length must not permit an unbounded body.
        if not _csrf_ok(request.headers.get("X-HQ-CSRF", "")):
            abort(403)
        if request.content_length is not None and request.content_length > 2048:
            abort(413)
        raw = request.stream.read(2049)
        if len(raw) > 2048:
            abort(413)
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_fields)
        except (ValueError, UnicodeDecodeError):
            abort(400)
        if (not isinstance(data, dict) or set(data) != {"work_id", "payment_id"}
                or type(data["work_id"]) is not str
                or type(data["payment_id"]) is not str):
            abort(400)
    else:
        if request.content_length is not None and request.content_length > 2048:
            abort(413)
        if not _csrf_ok(request.form.get("csrf", "")):
            abort(403)
        data = request.form
    return data


@payment_review.post("/api/hq/square-payment-review")
@required
def review_api():
    data = _review_input()
    try:
        result = compare_references(data.get("work_id"), data.get("payment_id"))
    except ValueError:
        return jsonify(error="Invalid exact references or stored evidence"), 400
    except LookupError:
        return jsonify(error="HQ job not found"), 404
    except (OSError, sqlite3.Error, TypeError):
        return jsonify(error="Payment evidence is temporarily unavailable"), 503
    return jsonify(result)


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Exact payment review | Tommy Tints HQ</title>
<style>
body{background:#0b0f16;color:#eef2f8;font:16px system-ui;margin:0}
main{max-width:750px;margin:26px auto;padding:16px}
section{background:#151e2b;border:1px solid #334153;border-radius:12px;padding:20px;margin:15px 0}
a{color:#ff8b92}label{display:block;margin-top:16px}
input{display:block;margin:5px 0;padding:11px;width:95%;background:#0c121b;color:white;border:1px solid #555;border-radius:6px}
button{background:#c82737;border:0;border-radius:8px;padding:12px;color:white;margin-top:14px;cursor:pointer}
p,li{line-height:1.55}small{color:#b1bdc9}dl{display:grid;grid-template-columns:1fr 1fr;gap:8px}
dt{color:#b1bdc9}dd{margin:0;word-break:break-all}
</style></head><body><main>
<a href="/hq/square-payment-evidence">Back to Square payment evidence</a>
<h1>Exact reference review</h1>
<p>{{ notice }}</p>
<section><form method="post">
<input type="hidden" name="csrf" value="{{ csrf }}">
<label for="work_id">HQ work record ID (32 characters)</label>
<input id="work_id" name="work_id" maxlength="32" autocomplete="off" required value="{{ work_id }}">
<label for="payment_id">Exact Square payment ID</label>
<input id="payment_id" name="payment_id" maxlength="192" autocomplete="off" required value="{{ payment_id }}">
<button type="submit">Compare evidence (read-only)</button>
</form></section>
{% if error %}<section role="alert">{{ error }}</section>{% endif %}
{% if result %}
<section><h2>Review result — not payment confirmation</h2>
<p><strong>{{ "Reference comparison complete" if result.candidate_evidence_complete else "Review incomplete / warnings" }}</strong></p>
<dl>
<dt>HQ job status</dt><dd>{{ result.job_status }}</dd>
<dt>Approved selling price</dt><dd>{{ result.price_pence if result.price_pence is not none else "Not valid" }} pence</dd>
<dt>Planned deposit (not paid)</dt><dd>{{ result.planned_deposit_pence if result.planned_deposit_pence is not none else "Not valid" }} pence</dd>
<dt>Square evidence found</dt><dd>{{ result.payment_evidence_found }}</dd>
<dt>Square status</dt><dd>{{ result.provider_status or "Unknown" }}</dd>
<dt>Square total</dt><dd>{{ result.provider_amount_pence if result.provider_amount_pence is not none else "Unknown" }} {{ result.provider_currency or "" }} minor units</dd>
<dt>Reported refund</dt><dd>{{ result.reported_refund_pence if result.reported_refund_pence is not none else "Not supplied" }}</dd>
<dt>Matches approved price</dt><dd>{{ result.matches_approved_price }}</dd>
<dt>Matches planned deposit</dt><dd>{{ result.matches_planned_deposit }}</dd>
</dl>
{% if result.warnings %}<h3>Warnings</h3><ul>{% for warning in result.warnings %}<li>{{ warning }}</li>{% endfor %}</ul>{% endif %}
<p><small>Even a complete comparison is only a candidate. This screen never links a payment, marks a job paid, reconciles an invoice, or changes outstanding balances.</small></p>
</section>{% endif %}
</main></body></html>"""


@payment_review.route("/hq/square-payment-review", methods=["GET", "POST"])
@required
def review_page():
    result, error, code = None, None, 200
    work_id, payment_id = "", ""
    if request.method == "POST":
        data = _review_input()
        work_id, payment_id = data.get("work_id", ""), data.get("payment_id", "")
        try:
            result = compare_references(work_id, payment_id)
        except ValueError:
            error, code = "Invalid exact references or stored evidence", 400
        except LookupError:
            error, code = "HQ job not found", 404
        except (OSError, sqlite3.Error, TypeError):
            error, code = "Payment evidence is temporarily unavailable", 503
    return render_template_string(_PAGE, csrf=session.get("csrf"),
                                  notice=_NOTICE, result=result, error=error,
                                  work_id=work_id, payment_id=payment_id), code
