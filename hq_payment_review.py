"""Owner-only exact-reference review of Square evidence against one HQ job.

This is a comparison, NOT reconciliation. Matching amounts cannot prove that
a payment belongs to a particular job, reached the bank, or is non-refundable.
An explicit owner draft is an inactive audit record only; no receipts, paid flags, prices, or external records are written.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
import os
import re
import secrets
import sqlite3

from flask import Blueprint, abort, jsonify, redirect, render_template_string, request, session, url_for

from customer_request_store import connect, storage_ready
from hq_auth import active_session, required
from hq_work_progress import _actor_fingerprint
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
        "warnings": [], "draft_evidence": None,
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
    # A digest binds the exact reviewed snapshot and native revision. It is
    # not proof of payer identity and is never a receipt or settlement.
    if result["candidate_evidence_complete"] and evidence["total_minor"] > 0:
        result["draft_work_revision"] = job["revision"]
        result["draft_evidence"] = hashlib.sha256(json.dumps([
            work_id, payment_id, job["revision"], job["status"], price, deposit,
            meta["location"], meta["refreshed_at"], meta["window_start"], meta["window_end"],
            evidence["status"], evidence["currency"], evidence["total_minor"],
            evidence["refunded_minor"],
        ], separators=(",", ":")).encode()).hexdigest()
    # Never return a "verified" job receipt or modify any financial balance.
    return result


def _draft_table_exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_square_review_drafts'").fetchone())


def _draft_history():
    # Opening the review page must not create a database or a draft table.
    root = os.environ.get("HQ_DATA_DIR", "")
    path = Path(root) / "customer_requests.sqlite3"
    if not root or not path.is_absolute() or not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        if not _draft_table_exists(db):
            return []
        return [dict(row) for row in db.execute("""SELECT e.work_id,e.payment_id,e.revision,
            e.decision,e.amount_pence,e.created_at,e.evidence_digest
            FROM hq_square_review_drafts e WHERE e.revision=(SELECT MAX(p.revision)
            FROM hq_square_review_drafts p WHERE p.work_id=e.work_id AND p.payment_id=e.payment_id)
            ORDER BY e.created_at DESC LIMIT 50""")]


def save_review_draft(work_id, payment_id, expected, decision, digest, actor):
    """Append an inactive candidate or withdrawal; never post a receipt."""
    if (not storage_ready() or not isinstance(work_id, str) or not _WORK_ID.fullmatch(work_id)
            or not isinstance(payment_id, str) or not _PAYMENT_ID.fullmatch(payment_id)
            or type(expected) is not int or not 0 <= expected < 2**63-1
            or decision not in ("save", "withdraw")
            or not isinstance(digest, str) or (decision == "save" and not re.fullmatch("[0-9a-f]{64}", digest))
            or (decision == "withdraw" and digest)):
        raise ValueError("Invalid draft instructions")
    actor = _actor_fingerprint(actor)
    # Evidence lives in another SQLite database. Recompare before writing and
    # bind the saved observation, never claim a cross-database atomic receipt.
    result = compare_references(work_id, payment_id) if decision == "save" else None
    if result and (not result["draft_evidence"] or not secrets.compare_digest(result["draft_evidence"], digest)):
        raise ValueError("Evidence changed; compare again")
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("""CREATE TABLE IF NOT EXISTS hq_square_review_drafts (
            work_id TEXT NOT NULL REFERENCES hq_work_records(id),payment_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision>0),
            decision TEXT NOT NULL CHECK(decision IN ('save','withdraw')),
            amount_pence INTEGER NOT NULL CHECK(amount_pence>0),evidence_digest TEXT NOT NULL,
            actor_fingerprint TEXT NOT NULL,created_at TEXT NOT NULL,
            PRIMARY KEY(work_id,payment_id,revision))""")
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("""SELECT * FROM hq_square_review_drafts WHERE work_id=? AND payment_id=?
            ORDER BY revision DESC LIMIT 1""", (work_id,payment_id)).fetchone()
        revision = prior["revision"] if prior else 0
        if prior and prior["decision"] == decision and (decision == "withdraw" or prior["evidence_digest"] == digest) and expected in (revision, revision-1):
            return
        if expected != revision or (decision == "withdraw" and (not prior or prior["decision"] != "save")):
            raise ValueError("Draft changed; reload")
        if result:
            # Recheck native state inside the canonical write transaction.
            job = db.execute("SELECT status,kind,price_pence,deposit_pence,revision FROM hq_work_records WHERE id=?", (work_id,)).fetchone()
            if (not job or job["kind"] != "job" or job["status"] != result["job_status"]
                    or job["revision"] != result["draft_work_revision"] or job["price_pence"] != result["price_pence"] or job["deposit_pence"] != result["planned_deposit_pence"]):
                raise ValueError("Native job changed")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_work_archive_state'").fetchone():
                if db.execute("SELECT 1 FROM hq_work_archive_state WHERE work_id=? AND is_archived=1", (work_id,)).fetchone():
                    raise ValueError("Archived job")
            conflict = db.execute("""SELECT 1 FROM hq_square_review_drafts e
                WHERE e.payment_id=? AND e.work_id<>? AND e.decision='save'
                AND e.revision=(SELECT MAX(p.revision) FROM hq_square_review_drafts p
                WHERE p.work_id=e.work_id AND p.payment_id=e.payment_id)""", (payment_id,work_id)).fetchone()
            if conflict:
                raise ValueError("Payment already has another active review draft")
        db.execute("INSERT INTO hq_square_review_drafts VALUES(?,?,?,?,?,?,?,?)",
                   (work_id,payment_id,revision+1,decision,
                    result["provider_amount_pence"] if result else prior["amount_pence"],
                    digest if result else prior["evidence_digest"],actor,datetime.now(timezone.utc).isoformat()))


@payment_review.post("/hq/square-payment-review/draft")
@required
def review_draft():
    if request.content_length is None or request.content_length > 2048:
        abort(413)
    data = request.form
    if not _csrf_ok(data.get("csrf", "")):
        abort(403)
    if set(data) != {"csrf","work_id","payment_id","revision","decision","digest"} or any(len(data.getlist(k)) != 1 for k in data):
        abort(400)
    revision = data.get("revision", "")
    if not revision.isascii() or not revision.isdecimal() or len(revision)>18:
        abort(400)
    owner = active_session()
    if owner is None:
        abort(403)
    try:
        save_review_draft(data["work_id"],data["payment_id"],int(revision),data["decision"],data["digest"],owner["subject"])
    except (ValueError, LookupError, OSError, sqlite3.Error, TypeError):
        return redirect(url_for(".review_page",draft_error="1"),code=303)
    return redirect(url_for(".review_page",draft_saved="1"),code=303)


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
<p><small>Even a complete comparison is only a candidate. A saved review draft is inactive evidence, not an allocation, receipt or balance change.</small></p>
{% if result.draft_evidence %}
<form method="post" action="{{ url_for('.review_draft') }}">
<input type="hidden" name="csrf" value="{{ csrf }}">
<input type="hidden" name="work_id" value="{{ work_id }}"><input type="hidden" name="payment_id" value="{{ payment_id }}">
<input type="hidden" name="revision" value="{{ draft_revision }}">
<input type="hidden" name="decision" value="save"><input type="hidden" name="digest" value="{{ result.draft_evidence }}">
<button type="submit">Save inactive payment review draft</button></form>{% endif %}
</section>{% endif %}
<section><h2>Saved inactive payment review drafts</h2>
<p>Latest 50 draft states. Historical observations may be stale, refunded or withdrawn. No amounts are allocated or counted as money received. Recompare before saving a new revision.</p>
{% if draft_saved %}<p role="status">Draft state saved. No receipt or balance changed.</p>{% endif %}
{% if draft_error %}<p role="alert">Draft not saved. Recompare evidence, reload or withdraw a conflicting draft.</p>{% endif %}
{% for draft in drafts %}
<p>HQ {{ draft.work_id }} · Square {{ draft.payment_id }} · {{ draft.amount_pence }} pence observed · revision {{ draft.revision }} · {{ 'Inactive candidate' if draft.decision == 'save' else 'Withdrawn' }} · {{ draft.created_at }}</p>
{% if draft.decision == 'save' %}<form method="post" action="{{ url_for('.review_draft') }}">
<input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="work_id" value="{{ draft.work_id }}">
<input type="hidden" name="payment_id" value="{{ draft.payment_id }}"><input type="hidden" name="revision" value="{{ draft.revision }}">
<input type="hidden" name="decision" value="withdraw"><input type="hidden" name="digest" value="">
<button type="submit">Withdraw draft</button></form>{% endif %}
{% else %}<p>No saved drafts.</p>{% endfor %}</section>
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
    try:
        drafts = _draft_history()
        # Read the exact pair independently of the bounded recent list.
        draft_revision = 0
        if result and result["draft_evidence"]:
            root = Path(os.environ.get("HQ_DATA_DIR", "")) / "customer_requests.sqlite3"
            if root.is_absolute() and root.is_file():
                with closing(sqlite3.connect(root.as_uri()+"?mode=ro", uri=True, timeout=3)) as db:
                    if _draft_table_exists(db):
                        draft_revision = db.execute("SELECT COALESCE(MAX(revision),0) FROM hq_square_review_drafts WHERE work_id=? AND payment_id=?", (work_id,payment_id)).fetchone()[0]
    except (OSError, sqlite3.Error):
        return "Payment draft storage temporarily unavailable", 503
    return render_template_string(_PAGE, csrf=session.get("csrf"),
                                  notice=_NOTICE, result=result, error=error,
                                  work_id=work_id, payment_id=payment_id,
                                  drafts=drafts,draft_revision=draft_revision,
                                  draft_saved=request.args.get("draft_saved")=="1",
                                  draft_error=request.args.get("draft_error")=="1"), code
