"""Owner-triggered, read-only Sheets Jobs-link preflight.

No spreadsheet writes, mapping persistence, automatic sync, receipt evidence
or customer-data responses. Never invoke a provider merely to render a page.
"""
import re
import secrets
import time

from flask import Blueprint, current_app, jsonify, render_template, request, session
from hq_auth import required
from hq_integration_health import record_check
from hq_sheet_link_preflight import MAX_ROWS, preflight_job_link
from hq_sheets_live import read_job_link_snapshot

sheet_link_inspection = Blueprint("sheet_link_inspection", __name__)


@sheet_link_inspection.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@sheet_link_inspection.get("/hq/sheets/job-link-preflight")
@required
def page():
    return render_template("sheets_link_preflight.html", csrf=session.get("csrf", ""))


@sheet_link_inspection.post("/api/hq/sheets/job-link-preflight")
@required
def inspect():
    # An explicit owner POST is required even though the upstream operation is
    # read-only; no background read, login-independent call or CORS endpoint.
    if not secrets.compare_digest(
        request.headers.get("X-HQ-CSRF", ""), session.get("csrf", "not-set")
    ):
        return jsonify(error="Invalid CSRF token"), 403
    if request.content_length is not None and request.content_length > 512:
        return jsonify(error="Inspection request too large"), 413
    if not request.is_json:
        return jsonify(error="JSON object required"), 415
    values = request.get_json(silent=True)
    if not isinstance(values, dict) or set(values) != {"job_ref", "target_row", "mode"}:
        return jsonify(error="Exact job_ref, target_row and mode are required"), 400
    job_ref, row, mode = values["job_ref"], values["target_row"], values["mode"]
    if (type(job_ref) is not str or not re.fullmatch(r"J[0-9]{4,8}", job_ref)
            or type(row) is not int or not 5 <= row <= MAX_ROWS
            or type(mode) is not str or mode not in ("new", "existing")):
        return jsonify(error="Invalid manually reviewed job reference, row or mode"), 400

    provider = current_app.extensions["hq_sheets_connection"]()
    if not provider or provider[1] <= time.time():
        return jsonify(
            error="Owner Google Sheets read-only consent is required",
            connect_url="/auth/sheets/connect",
        ), 401
    try:
        # The adapter returns Sheets GridData to the server only. Never include
        # snapshots, row text, customer names or tokens in a response/log.
        snapshot = read_job_link_snapshot(provider[0])
    except Exception:
        record_check("sheets_job_preflight", "Jobs link inspection provider read failed")
        current_app.logger.warning("Jobs link preflight read failed")
        return jsonify(error="Read-only Jobs workbook inspection failed; verify owner consent and source access"), 502

    try:
        safe = preflight_job_link(snapshot, job_ref=job_ref, target_row=row, mode=mode)
    except ValueError as exc:
        # The pure preflight supplies fixed, non-customer exception messages.
        record_check("sheets_job_preflight", "Jobs link inspection source or candidate conflict")
        return jsonify(error=str(exc), preflight="blocked", live_sync=False), 409
    if not record_check("sheets_job_preflight"):
        return jsonify(error=(
            "Preflight evaluated but observation storage failed; "
            "do not treat this as recorded read evidence"
        )), 503
    return jsonify(safe)
