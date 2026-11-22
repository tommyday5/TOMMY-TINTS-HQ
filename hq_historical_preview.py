"""Read-only owner-triggered historical Jobs/Customers migration DRY RUN.

No source or HQ record writes, no auto-mapping, no import, no finance/payment
interpretation. Never return names, emails, registrations, prices or raw cells.
"""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import secrets
import sqlite3
import time
import unicodedata

import requests
from flask import Blueprint, current_app, render_template, request, session
from hq_auth import required
from hq_job_sync import WORKBOOK

migration_preview = Blueprint("migration_preview", __name__)
SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets/" + WORKBOOK
RANGES = ("'Jobs'!A4:AZ1000", "'Customers'!A4:AF1000")
HEADERS = {
    "Jobs": {0: "Job ID", 1: "Date", 2: "Customer",
             3: "Customer Type", 4: "Vehicle/Reg",
             5: "Service 1", 16: "Quoted Price", 17: "Status"},
    "Customers": {0: "Customer", 1: "Type", 2: "Postcode"},
}
J_REF = re.compile(r"J[0-9]{4,8}\Z")


def _norm(value):
    if not isinstance(value, str) or not value.strip() or value.startswith("="):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _cell(row, index):
    return row[index] if isinstance(row, list) and index < len(row) else ""


def _ranges(response):
    values = response.get("valueRanges") if isinstance(response, dict) else None
    if not isinstance(values, list) or len(values) != 2:
        raise ValueError("Source ranges are missing")
    result = []
    for i, name in enumerate(("Jobs", "Customers")):
        item = values[i]
        source_range = item.get("range", "") if isinstance(item, dict) else ""
        if not (source_range.startswith(name + "!") or
                source_range.startswith("'" + name + "'!")):
            raise ValueError("Source tab identity differs")
        rows = item.get("values", [])
        if not isinstance(rows, list) or len(rows) > 997 or any(
                not isinstance(row, list) or len(row) > (52 if i == 0 else 32)
                for row in rows):
            raise ValueError("Invalid or oversized source rows")
        result.append(rows)
    return result


def _check_source(formula, computed):
    raw, actual = _ranges(formula), _ranges(computed)
    for tab, head, other in zip(("Jobs", "Customers"), raw, actual):
        if not head or not other:
            raise ValueError("The source header is missing")
        for idx, expected in HEADERS[tab].items():
            if _cell(head[0], idx) != expected or _cell(other[0], idx) != expected:
                raise ValueError("Source header changed; review the mapping")
        # Name/reference columns that are literal must agree across independent
        # FORMULA and UNFORMATTED_VALUE reads. Formulas are never trusted IDs.
        identity_cols = (0, 2) if tab == "Jobs" else (0,)
        for offset in range(1, max(len(head), len(other))):
            first = head[offset] if offset < len(head) else []
            second = other[offset] if offset < len(other) else []
            for col in identity_cols:
                original = _cell(first, col)
                if isinstance(original, (str, int, float)) and not (
                    isinstance(original, str) and original.startswith("=")
                ) and original != _cell(second, col):
                    raise ValueError("Source identities changed between reads")
    return raw


def _native_inventory():
    """Snapshot existing native names and row counts without schema creation."""
    root = os.environ.get("HQ_DATA_DIR", "")
    if not root or not Path(root).is_absolute():
        return None
    path = Path(root) / "customer_requests.sqlite3"
    if not path.is_file():
        return None
    try:
        with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=3) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            names = [r[0] for r in db.execute("SELECT name FROM hq_customers")]
            count = db.execute("SELECT count(*) FROM hq_work_records").fetchone()[0]
            db.rollback()
        return {"names": {_norm(n) for n in names if _norm(n)},
                "customers": len(names), "works": count}
    except (OSError, sqlite3.Error):
        return None


def build_preview(formula, computed, *, native=None):
    """Pure, bounded advisory result. All comparisons are unapproved candidates."""
    jobs, customers = _check_source(formula, computed)
    known = native["names"] if native is not None else set()
    if not isinstance(known, set):
        raise ValueError("Invalid native comparison snapshot")
    ref_rows = {}
    job_out = []
    jobs_hq_candidates = 0
    for i, row in enumerate(jobs[1:], start=5):
        entered = _cell(row, 0)
        person = _cell(row, 2)
        meaningful = any(_cell(row, j) not in ("", None) for j in
                         (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 16))
        if not meaningful:
            continue
        ref = entered if isinstance(entered, str) and J_REF.fullmatch(entered) else None
        if ref:
            ref_rows.setdefault(ref.casefold(), []).append(i)
        if _norm(person) and _norm(person) in known:
            jobs_hq_candidates += 1
        job_out.append({"row": i, "ref": ref, "status": "review",
                        "price_kind": "formula" if isinstance(_cell(row, 16), str)
                        and _cell(row, 16).startswith("=") else (
                            "missing" if _cell(row, 16) in ("", None) else "entered")})
    duplicates = {key for key, rows in ref_rows.items() if len(rows) > 1}
    for entry in job_out:
        if entry["ref"] is None:
            entry["status"] = "missing_or_invalid_id"
        elif entry["ref"].casefold() in duplicates:
            entry["status"] = "duplicate_reference"
    names = []
    customer_formula = 0
    customers_hq_candidates = 0
    for row in customers[1:]:
        raw_name = _cell(row, 0)
        # Exclude blank/formula-only dashboard rows from identities.
        if isinstance(raw_name, str) and raw_name.startswith("="):
            customer_formula += 1
            continue
        name = _norm(raw_name)
        if name:
            names.append(name)
            if name in known:
                customers_hq_candidates += 1
    counts = Counter(names)
    return {
        "workbook_id": WORKBOOK, "jobs_sheet_id": 330634230,
        "customers_sheet_id": 378990021, "first_data_row": 5,
        "last_scanned_row": 1000, "generated_at": datetime.now(timezone.utc).isoformat(),
        "jobs_rows": len(job_out), "valid_unique_j_refs": sum(
            1 for e in job_out if e["status"] == "review"),
        "duplicate_j_rows": sum(e["status"] == "duplicate_reference" for e in job_out),
        "missing_or_invalid_j_rows": sum(
            e["status"] == "missing_or_invalid_id" for e in job_out),
        "customers_named_rows": len(names),
        "duplicate_customer_name_rows": sum(n for n in counts.values() if n > 1),
        "formula_customer_name_rows": customer_formula,
        "native_available": native is not None,
        "native_customer_count": native["customers"] if native else None,
        "native_work_count": native["works"] if native else None,
        "possible_native_customer_rows": customers_hq_candidates if native else None,
        "possible_native_job_rows": jobs_hq_candidates if native else None,
        "sample": job_out[:60], "sample_truncated": len(job_out) > 60,
        "import_authorised": False, "identity_verified": False,
        "source_of_truth_changed": False, "reconciled": False,
    }


def _read_live(token):
    headers = {"Authorization": "Bearer " + token}
    dimensions = requests.get(SHEETS_BASE, params={
        "fields": "spreadsheetId,sheets(properties(sheetId,title,gridProperties(rowCount)))"
    }, headers=headers, timeout=(3, 15))
    dimensions.raise_for_status()
    meta = dimensions.json()
    tabs = {s.get("properties", {}).get("title"): s.get("properties", {})
            for s in meta.get("sheets", []) if isinstance(s, dict)}
    if meta.get("spreadsheetId") != WORKBOOK or any(
        tab not in tabs or tabs[tab].get("sheetId") != sid
        or type(tabs[tab].get("gridProperties", {}).get("rowCount")) is not int
        or not 5 <= tabs[tab]["gridProperties"]["rowCount"] <= 1000
        for tab, sid in (("Jobs", 330634230), ("Customers", 378990021))
    ):
        raise ValueError("Workbook identity or complete row bounds not verified")
    snapshots = []
    for mode in ("FORMULA", "UNFORMATTED_VALUE"):
        params = [("ranges", r) for r in RANGES] + [("valueRenderOption", mode)]
        response = requests.get(SHEETS_BASE + "/values:batchGet",
                                params=params, headers=headers, timeout=(3, 15))
        response.raise_for_status()
        snapshots.append(response.json())
    return snapshots


@migration_preview.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@migration_preview.route("/hq/migration/preview", methods=["GET", "POST"])
@required
def page():
    result = None
    error = None
    if request.method == "POST":
        if not secrets.compare_digest(
            request.form.get("csrf", ""), session.get("csrf", "not-set")
        ):
            return "Invalid CSRF token", 403
        # Click-to-read: never access the provider automatically on page load.
        provider = current_app.extensions["hq_sheets_connection"]()
        if not provider or provider[1] <= time.time():
            error = "Connect your owner Google Sheets read-only account first."
        else:
            try:
                formula, computed = _read_live(provider[0])
                result = build_preview(formula, computed, native=_native_inventory())
            except Exception:
                current_app.logger.warning("Read-only Jobs/Customers preview unavailable")
                error = "The source could not be fully verified. No preview or changes were made."
    return render_template("hq_migration_preview.html", result=result,
                           error=error, csrf=session.get("csrf", ""))
