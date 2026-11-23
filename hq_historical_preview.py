"""Read-only owner-triggered historical Jobs/Customers migration DRY RUN.

No source or HQ record writes, no auto-mapping, no import, no finance/payment
interpretation. Never return names, emails, registrations, prices or raw cells.
"""
from collections import Counter
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import secrets
import sqlite3
import time
import unicodedata

import requests
from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for
from hq_auth import required
from hq_job_sync import WORKBOOK
from hq_core_records import _schema
from customer_request_store import connect, storage_ready
from hq_work_progress import _actor_fingerprint

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


def build_preview(formula, computed, *, native=None, page_index=0, find_ref=""):
    """Pure, bounded advisory result with full-source, owner-selected page access.

    The full Jobs/Customers range is validated on every call; pagination only
    limits the PII-free review queue returned for rendering. No import occurs.
    """
    if type(page_index) is not int or not 0 <= page_index <= 16:
        raise ValueError("Invalid historical review page")
    if not isinstance(find_ref, str) or (find_ref and not J_REF.fullmatch(find_ref)):
        raise ValueError("Enter an exact uppercase J-series reference")
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
    display_rows = ([entry for entry in job_out if entry["ref"] == find_ref]
                    if find_ref else job_out)
    page_count = max(1, (len(display_rows) + 59) // 60)
    if page_index >= page_count:
        raise ValueError("Historical review page changed; start a fresh comparison")
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
        "sample": display_rows[page_index * 60:(page_index + 1) * 60],
        "sample_truncated": len(display_rows) > 60,
        "page_index": page_index, "page_count": page_count,
        "filtered_count": len(display_rows), "find_ref": find_ref,
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



# These are owner-curated, INACTIVE candidate mappings. Nothing below alters
# hq_work_records, source J references, Sheets, or source-of-truth ownership.
# Fresh provider reads prove only the momentary source snapshot, not identity.
_HEX = re.compile(r"[0-9a-f]{32}\Z")
_STAMP = re.compile(r"[0-9a-f]{64}\Z")


def _candidate_stamp(formula, computed, row_number):
    """Keyed fingerprint of both source renderings; never persist raw source cells."""
    if type(row_number) is not int or not 5 <= row_number <= 1000:
        raise ValueError("Invalid historical row")
    jobs_raw, _ = _check_source(formula, computed)
    jobs_values, _ = _ranges(computed)
    index = row_number - 4
    if index >= len(jobs_raw) or index >= len(jobs_values):
        raise ValueError("Historical row no longer exists")
    key = current_app.secret_key
    if not isinstance(key, str) or not key:
        raise RuntimeError("Persistent owner session key required")
    data = json.dumps([WORKBOOK, 330634230, row_number,
                       jobs_raw[index], jobs_values[index]],
                      ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()


def _verified_source_candidate(formula, computed, row_number, expected_ref, expected_stamp):
    """All Jobs rows must have a unique, literal J identifier at the chosen row."""
    if not isinstance(expected_ref, str) or not J_REF.fullmatch(expected_ref):
        raise ValueError("Invalid original J reference")
    if not isinstance(expected_stamp, str) or not _STAMP.fullmatch(expected_stamp):
        raise ValueError("Invalid source snapshot")
    jobs, _ = _check_source(formula, computed)
    index = row_number - 4
    if not 1 <= index < len(jobs):
        raise ValueError("Source row is outside verified bounds")
    identities = [_cell(row, 0) for row in jobs[1:]]
    if (identities[index - 1] != expected_ref
            or sum(str(item).casefold() == expected_ref.casefold() for item in identities) != 1
            or _cell(jobs[index], 0) != expected_ref):
        raise ValueError("J reference moved, duplicated, or changed; run a new comparison")
    actual_stamp = _candidate_stamp(formula, computed, row_number)
    if not hmac.compare_digest(actual_stamp, expected_stamp):
        raise ValueError("Source row changed; run a new comparison")
    return actual_stamp


def _review_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_j_candidates (
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE,
        work_id TEXT NOT NULL REFERENCES hq_work_records(id),
        work_revision INTEGER NOT NULL,
        workbook_id TEXT NOT NULL,
        sheet_id INTEGER NOT NULL,
        sheet_row INTEGER NOT NULL,
        job_ref TEXT NOT NULL,
        source_stamp TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        withdrawn_at TEXT,
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision IN (0,1)),
        CHECK(sheet_row BETWEEN 5 AND 1000)
    )""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_candidate_current_work
        ON hq_historical_j_candidates(work_id) WHERE withdrawn_at IS NULL""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_candidate_current_ref
        ON hq_historical_j_candidates(workbook_id,sheet_id,job_ref COLLATE NOCASE)
        WHERE withdrawn_at IS NULL""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_candidate_current_row
        ON hq_historical_j_candidates(workbook_id,sheet_id,sheet_row)
        WHERE withdrawn_at IS NULL""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_j_candidate_events (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL REFERENCES hq_historical_j_candidates(id),
        revision INTEGER NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('proposed','withdrawn')),
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(candidate_id,revision)
    )""")


def _current_candidates():
    """Read review queue without creating an empty database or modifying production."""
    root = os.environ.get("HQ_DATA_DIR", "")
    path = Path(root) / "customer_requests.sqlite3" if root and Path(root).is_absolute() else None
    if path is None or not path.is_file():
        return []
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=3) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        exists = db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='hq_historical_j_candidates'""").fetchone()
        if not exists:
            return []
        return [dict(row) for row in db.execute("""SELECT id,work_id,work_revision,
            job_ref,sheet_row,created_at,revision
            FROM hq_historical_j_candidates WHERE withdrawn_at IS NULL
            ORDER BY created_at DESC,id DESC LIMIT 100""")]


def _native_work_options():
    """Owner-only selector; IDs and revisions are genuine canonical HQ records."""
    root = os.environ.get("HQ_DATA_DIR", "")
    path = Path(root) / "customer_requests.sqlite3" if root and Path(root).is_absolute() else None
    if path is None or not path.is_file():
        return []
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=3) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return [dict(row) for row in db.execute("""SELECT w.id,w.revision,w.kind,w.status,
            w.service,c.name AS customer_name
            FROM hq_work_records w JOIN hq_customers c ON c.id=w.customer_id
            ORDER BY w.created_at DESC,w.id DESC LIMIT 80""")]


def stage_j_candidate(*, formula, computed, sheet_row, job_ref, source_stamp,
                      work_token, operation_id, owner_identity):
    """Persist only owner-selected IDs and keyed evidence. Does NOT link work."""
    if not storage_ready():
        raise ValueError("Persistent HQ storage is not configured")
    if not isinstance(operation_id, str) or not _HEX.fullmatch(operation_id):
        raise ValueError("Invalid submission identifier")
    if not isinstance(work_token, str) or not re.fullmatch(
            r"[0-9a-f]{32}:(0|[1-9][0-9]{0,15})", work_token):
        raise ValueError("Choose a canonical HQ work record")
    work_id, revision_text = work_token.split(":")
    work_revision = int(revision_text)
    stamp = _verified_source_candidate(formula, computed, sheet_row, job_ref, source_stamp)
    actor = _actor_fingerprint(owner_identity)
    now = datetime.now(timezone.utc).isoformat()
    try:
        with connect() as db:
            db.execute("PRAGMA foreign_keys=ON")
            _schema(db)
            _review_tables(db)
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("""SELECT id,work_id,work_revision,job_ref,
                sheet_row,source_stamp FROM hq_historical_j_candidates
                WHERE operation_id=?""", (operation_id,)).fetchone()
            if prior:
                if (prior["work_id"],prior["work_revision"],prior["job_ref"],
                        prior["sheet_row"],prior["source_stamp"]) != (
                        work_id,work_revision,job_ref,sheet_row,stamp):
                    raise ValueError("Submission ID already used for another candidate")
                return prior["id"]
            work = db.execute("""SELECT revision FROM hq_work_records WHERE id=?""",
                              (work_id,)).fetchone()
            if work is None or work["revision"] != work_revision:
                raise ValueError("HQ work record changed or is unavailable; reload")
            candidate_id = uuid.uuid4().hex
            db.execute("""INSERT INTO hq_historical_j_candidates
                (id,operation_id,work_id,work_revision,workbook_id,sheet_id,
                 sheet_row,job_ref,source_stamp,actor_fingerprint,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id,operation_id,work_id,work_revision,WORKBOOK,
                 330634230,sheet_row,job_ref,stamp,actor,now))
            db.execute("""INSERT INTO hq_historical_j_candidate_events
                VALUES (?,?,?,?,?,?)""", (uuid.uuid4().hex,candidate_id,0,
                                        "proposed",actor,now))
            return candidate_id
    except sqlite3.IntegrityError as exc:
        raise ValueError("Mapping collision: that J row, J reference or HQ work is already proposed") from exc


def withdraw_j_candidate(candidate_id, expected_revision, owner_identity):
    """Owner may withdraw a proposal; never delete evidence or change live work."""
    if not storage_ready() or not isinstance(candidate_id, str) or not _HEX.fullmatch(candidate_id):
        raise ValueError("Invalid proposal reference or storage")
    if type(expected_revision) is not int or expected_revision not in (0, 1):
        raise ValueError("Invalid proposal revision")
    actor = _actor_fingerprint(owner_identity)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _review_tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT revision,withdrawn_at FROM hq_historical_j_candidates
            WHERE id=?""", (candidate_id,)).fetchone()
        if row is None:
            raise LookupError("Proposal no longer exists")
        if row["withdrawn_at"] is not None:
            if expected_revision in (0, 1):
                return
        if row["revision"] != expected_revision or row["withdrawn_at"] is not None:
            raise ValueError("Proposal changed; reload before withdrawing")
        now = datetime.now(timezone.utc).isoformat()
        db.execute("""UPDATE hq_historical_j_candidates SET revision=1,withdrawn_at=?
            WHERE id=? AND revision=0 AND withdrawn_at IS NULL""", (now,candidate_id))
        db.execute("""INSERT INTO hq_historical_j_candidate_events
            VALUES (?,?,?,?,?,?)""", (uuid.uuid4().hex,candidate_id,1,
                                    "withdrawn",actor,now))



# Owner-only field assessments are advisory review evidence, never migration
# approval, a source-of-truth transfer or a write into canonical work records.
_REVIEW_FIELDS = (
    ("customer", "Customer identity"),
    ("customer_tier", "Retail / Trade / Maintenance"),
    ("vehicle", "Vehicle and registration"),
    ("services", "Service scope"),
    ("quoted_price", "Historical selling price"),
    ("lifecycle", "Job lifecycle"),
)
_REVIEW_DECISIONS = ("needs_evidence", "appears_consistent", "conflict")


def _field_review_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_candidate_field_reviews (
        candidate_id TEXT PRIMARY KEY REFERENCES hq_historical_j_candidates(id),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        source_stamp TEXT NOT NULL,
        work_revision INTEGER NOT NULL,
        assessments TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_candidate_field_events (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL REFERENCES hq_historical_j_candidates(id),
        revision INTEGER NOT NULL,
        source_stamp TEXT NOT NULL,
        work_revision INTEGER NOT NULL,
        assessments TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(candidate_id,revision)
    )""")


def _candidate_for_review(candidate_id):
    """Load a staged mapping without mutating/creating the production DB."""
    if not isinstance(candidate_id, str) or not _HEX.fullmatch(candidate_id):
        return None
    root = os.environ.get("HQ_DATA_DIR", "")
    path = Path(root) / "customer_requests.sqlite3" if root and Path(root).is_absolute() else None
    if path is None or not path.is_file():
        return None
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=3) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        if not db.execute("""SELECT 1 FROM sqlite_master WHERE
            type='table' AND name='hq_historical_j_candidates'""").fetchone():
            return None
        row = db.execute("""SELECT id,work_id,work_revision,job_ref,
            sheet_row,source_stamp,withdrawn_at FROM hq_historical_j_candidates
            WHERE id=?""", (candidate_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        if db.execute("""SELECT 1 FROM sqlite_master WHERE
            type='table' AND name='hq_historical_candidate_field_reviews'""").fetchone():
            review = db.execute("""SELECT revision,source_stamp,work_revision,
                assessments,updated_at FROM hq_historical_candidate_field_reviews
                WHERE candidate_id=?""", (candidate_id,)).fetchone()
            if review:
                item["field_review"] = dict(review)
                item["field_review"]["assessments"] = json.loads(review["assessments"])
        if db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='hq_historical_field_ownership'""").fetchone():
            ownership = db.execute("""SELECT revision,source_stamp,work_revision,
                assessment_revision,selections,updated_at
                FROM hq_historical_field_ownership WHERE candidate_id=?""",
                (candidate_id,)).fetchone()
            if ownership:
                item["field_ownership"] = dict(ownership)
                item["field_ownership"]["selections"] = json.loads(ownership["selections"])
        if db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='hq_historical_identity_reviews'""").fetchone():
            identity = db.execute("""SELECT revision,source_stamp,work_revision,
                customer_id,customer_stamp,assessment_revision,decision,basis,updated_at
                FROM hq_historical_identity_reviews WHERE candidate_id=?""",
                (candidate_id,)).fetchone()
            if identity:
                item["identity_review"] = dict(identity)
        return item


def _field_comparison(formula, computed, candidate):
    """Fresh source + current native display, shown only on owner POST."""
    _verified_source_candidate(formula, computed, candidate["sheet_row"],
                               candidate["job_ref"], candidate["source_stamp"])
    jobs, _ = _check_source(formula, computed)
    effective, _ = _ranges(computed)
    index = candidate["sheet_row"] - 4
    source, values = jobs[index], effective[index]
    root = os.environ.get("HQ_DATA_DIR", "")
    path = Path(root) / "customer_requests.sqlite3" if root and Path(root).is_absolute() else None
    if path is None or not path.is_file():
        raise ValueError("HQ records are unavailable")
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=3) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        has_tiers = db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='hq_native_work_details'""").fetchone()
        tier_sql = ("""(SELECT customer_tier FROM hq_native_work_details
                        WHERE work_id=w.id)""" if has_tiers else "NULL")
        native = db.execute("""SELECT w.revision,w.service,w.status,w.price_pence,
            c.name AS customer_name,v.make,v.model,v.registration, """ +
            tier_sql + """ AS customer_tier
            FROM hq_work_records w JOIN hq_customers c ON c.id=w.customer_id
            JOIN hq_vehicles v ON v.id=w.vehicle_id WHERE w.id=?""",
            (candidate["work_id"],)).fetchone()
        if native is None or native["revision"] != candidate["work_revision"]:
            raise ValueError("Native HQ work changed; withdraw and restage the candidate")
    # This is a side-by-side inspection, not a value comparison or automatic match.
    def display(value):
        if value is None or value == "":
            return "(unrecorded)"
        return str(value)[:180]
    services = [display(_cell(source, i)) for i in range(5, 10)
                if _cell(source, i) not in ("", None)]
    raw_price = _cell(source, 16)
    actual_price = _cell(values, 16)
    price = display(raw_price)
    if isinstance(raw_price, str) and raw_price.startswith("="):
        price = "Formula " + price + " (effective: " + display(actual_price) + ")"
    return [
        {"key": "customer", "label": "Customer identity",
         "source": display(_cell(source, 2)), "native": display(native["customer_name"])},
        {"key": "customer_tier", "label": "Retail / Trade / Maintenance",
         "source": display(_cell(source, 3)), "native": display(native["customer_tier"])},
        {"key": "vehicle", "label": "Vehicle and registration",
         "source": display(_cell(source, 4)),
         "native": display(" ".join(str(native[k]) for k in ("make", "model", "registration")))},
        {"key": "services", "label": "Service scope",
         "source": " / ".join(services) or "(unrecorded)",
         "native": display(native["service"])},
        {"key": "quoted_price", "label": "Historical selling price",
         "source": price,
         "native": "(unrecorded)" if native["price_pence"] is None
                   else "GBP %.2f" % (native["price_pence"] / 100)},
        {"key": "lifecycle", "label": "Job lifecycle (not payment)",
         "source": display(_cell(source, 17)), "native": display(native["status"])},
    ]


def save_candidate_field_review(*, candidate_id, expected_revision, assessments,
                                formula, computed, owner_identity):
    """Save owner assessment with source/native revision checks and audit events."""
    if not storage_ready() or not isinstance(candidate_id, str) or not _HEX.fullmatch(candidate_id):
        raise ValueError("Invalid candidate or persistent storage")
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**31:
        raise ValueError("Invalid review revision")
    if not isinstance(assessments, dict) or set(assessments) != {
            name for name, _ in _REVIEW_FIELDS} or any(
            v not in _REVIEW_DECISIONS for v in assessments.values()):
        raise ValueError("Choose a valid assessment for each field")
    if all(v == "needs_evidence" for v in assessments.values()):
        raise ValueError("Review at least one field before saving")
    actor = _actor_fingerprint(owner_identity)
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(assessments, separators=(",", ":"), sort_keys=True)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _review_tables(db)
        _field_review_tables(db)
        db.execute("BEGIN IMMEDIATE")
        candidate = db.execute("""SELECT work_id,work_revision,job_ref,sheet_row,
            source_stamp,withdrawn_at FROM hq_historical_j_candidates
            WHERE id=?""", (candidate_id,)).fetchone()
        if candidate is None or candidate["withdrawn_at"] is not None:
            raise ValueError("Candidate withdrawn or no longer available")
        _verified_source_candidate(formula, computed, candidate["sheet_row"],
                                   candidate["job_ref"], candidate["source_stamp"])
        work = db.execute("SELECT revision FROM hq_work_records WHERE id=?",
                          (candidate["work_id"],)).fetchone()
        if work is None or work["revision"] != candidate["work_revision"]:
            raise ValueError("Native work changed; restage this candidate")
        previous = db.execute("""SELECT revision,source_stamp,work_revision,assessments
            FROM hq_historical_candidate_field_reviews WHERE candidate_id=?""",
            (candidate_id,)).fetchone()
        if previous and (previous["source_stamp"],previous["work_revision"],
                         previous["assessments"]) == (
                         candidate["source_stamp"],candidate["work_revision"],payload):
            return previous["revision"]  # safe exact retry; no duplicate audit
        if (previous["revision"] if previous else 0) != expected_revision:
            raise ValueError("Assessment changed; reload before saving")
        revision = expected_revision + 1
        db.execute("""INSERT INTO hq_historical_candidate_field_reviews
            (candidate_id,revision,source_stamp,work_revision,assessments,
             actor_fingerprint,updated_at) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(candidate_id) DO UPDATE SET
              revision=excluded.revision,source_stamp=excluded.source_stamp,
              work_revision=excluded.work_revision,assessments=excluded.assessments,
              actor_fingerprint=excluded.actor_fingerprint,updated_at=excluded.updated_at""",
            (candidate_id,revision,candidate["source_stamp"],candidate["work_revision"],
             payload,actor,now))
        db.execute("""INSERT INTO hq_historical_candidate_field_events
            VALUES (?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex,candidate_id,revision,candidate["source_stamp"],
             candidate["work_revision"],payload,actor,now))
        return revision



# Inactive owner draft decisions, NOT verified matches or source-of-truth changes.
_OWNERSHIP_CHOICES = ("hold", "review_sheets", "review_hq", "manual_resolution")


def _field_ownership_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_field_ownership (
        candidate_id TEXT PRIMARY KEY REFERENCES hq_historical_j_candidates(id),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        source_stamp TEXT NOT NULL,
        work_revision INTEGER NOT NULL,
        assessment_revision INTEGER NOT NULL,
        selections TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_field_ownership_events (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL REFERENCES hq_historical_j_candidates(id),
        revision INTEGER NOT NULL,
        source_stamp TEXT NOT NULL,
        work_revision INTEGER NOT NULL,
        assessment_revision INTEGER NOT NULL,
        selections TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(candidate_id,revision)
    )""")


def save_candidate_field_ownership(*, candidate_id, expected_revision, selections,
                                   formula, computed, owner_identity):
    """Save review preferences only, never modify a source or canonical HQ record."""
    if not storage_ready() or not isinstance(candidate_id, str) or not _HEX.fullmatch(candidate_id):
        raise ValueError("Invalid candidate or persistent storage")
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**31:
        raise ValueError("Invalid ownership revision")
    if (not isinstance(selections, dict)
            or set(selections) != {field for field, _ in _REVIEW_FIELDS}
            or any(type(choice) is not str or choice not in _OWNERSHIP_CHOICES
                   for choice in selections.values())):
        raise ValueError("Choose a permitted handling decision for every field")
    if all(choice == "hold" for choice in selections.values()):
        raise ValueError("Select at least one field for review")
    actor, now = _actor_fingerprint(owner_identity), datetime.now(timezone.utc).isoformat()
    payload = json.dumps(selections, sort_keys=True, separators=(",", ":"))
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _review_tables(db)
        _field_review_tables(db)
        _field_ownership_tables(db)
        db.execute("BEGIN IMMEDIATE")
        candidate = db.execute("""SELECT work_id,work_revision,job_ref,sheet_row,
            source_stamp,withdrawn_at FROM hq_historical_j_candidates
            WHERE id=?""", (candidate_id,)).fetchone()
        if candidate is None or candidate["withdrawn_at"] is not None:
            raise ValueError("Historical candidate withdrawn or missing")
        _verified_source_candidate(formula, computed, candidate["sheet_row"],
                                   candidate["job_ref"], candidate["source_stamp"])
        work = db.execute("SELECT revision FROM hq_work_records WHERE id=?",
                          (candidate["work_id"],)).fetchone()
        if work is None or work["revision"] != candidate["work_revision"]:
            raise ValueError("Native work changed; withdraw and restage candidate")
        assessment = db.execute("""SELECT revision,assessments,source_stamp,work_revision
            FROM hq_historical_candidate_field_reviews WHERE candidate_id=?""",
            (candidate_id,)).fetchone()
        if (assessment is None or assessment["source_stamp"] != candidate["source_stamp"]
                or assessment["work_revision"] != candidate["work_revision"]):
            raise ValueError("Save a fresh field-by-field assessment first")
        decisions = json.loads(assessment["assessments"])
        for field, choice in selections.items():
            if choice in ("review_sheets", "review_hq") and decisions.get(field) != "appears_consistent":
                raise ValueError("Source preference needs a consistent field assessment")
            if choice == "manual_resolution" and decisions.get(field) == "needs_evidence":
                raise ValueError("Assess the field before recording manual resolution")
        previous = db.execute("""SELECT revision,selections,source_stamp,
            work_revision,assessment_revision FROM hq_historical_field_ownership
            WHERE candidate_id=?""", (candidate_id,)).fetchone()
        if previous and (previous["selections"],previous["source_stamp"],
                         previous["work_revision"],previous["assessment_revision"]) == (
                             payload,candidate["source_stamp"],candidate["work_revision"],
                             assessment["revision"]):
            return previous["revision"]  # exact replay, without a duplicate event
        if (previous["revision"] if previous else 0) != expected_revision:
            raise ValueError("Field ownership proposal changed; reload before saving")
        revision = expected_revision + 1
        db.execute("""INSERT INTO hq_historical_field_ownership
            (candidate_id,revision,source_stamp,work_revision,assessment_revision,
             selections,actor_fingerprint,updated_at) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(candidate_id) DO UPDATE SET
             revision=excluded.revision,source_stamp=excluded.source_stamp,
             work_revision=excluded.work_revision,
             assessment_revision=excluded.assessment_revision,
             selections=excluded.selections,actor_fingerprint=excluded.actor_fingerprint,
             updated_at=excluded.updated_at""",
            (candidate_id,revision,candidate["source_stamp"],candidate["work_revision"],
             assessment["revision"],payload,actor,now))
        db.execute("""INSERT INTO hq_historical_field_ownership_events
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex,candidate_id,revision,candidate["source_stamp"],
             candidate["work_revision"],assessment["revision"],payload,actor,now))
        return revision



# Identity review is an owner-authored, INACTIVE mapping proposal. It does not
# assert verified identity, copy Sheets cells, or update canonical customers.
_IDENTITY_DECISIONS = ("hold", "potential_match", "conflict")
_IDENTITY_BASIS = ("none", "name_crosscheck", "owner_document_review")


def _identity_review_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_identity_reviews (
        candidate_id TEXT PRIMARY KEY REFERENCES hq_historical_j_candidates(id),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        source_stamp TEXT NOT NULL,
        work_revision INTEGER NOT NULL,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        customer_stamp TEXT NOT NULL,
        assessment_revision INTEGER NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('hold','potential_match','conflict')),
        basis TEXT NOT NULL CHECK(basis IN ('none','name_crosscheck','owner_document_review')),
        actor_fingerprint TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_identity_review_events (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL REFERENCES hq_historical_j_candidates(id),
        revision INTEGER NOT NULL,
        source_stamp TEXT NOT NULL,
        work_revision INTEGER NOT NULL,
        customer_id TEXT NOT NULL,
        customer_stamp TEXT NOT NULL,
        assessment_revision INTEGER NOT NULL,
        decision TEXT NOT NULL,
        basis TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(candidate_id,revision)
    )""")


def _identity_evidence(db, candidate, formula, computed):
    """Recheck both source lists and current HQ customer without storing PII."""
    _verified_source_candidate(formula, computed, candidate["sheet_row"],
                               candidate["job_ref"], candidate["source_stamp"])
    jobs, customers = _check_source(formula, computed)
    name = _cell(jobs[candidate["sheet_row"] - 4], 2)
    source_name = _norm(name)
    work = db.execute("""SELECT w.revision,w.customer_id,c.name,c.email,c.phone
        FROM hq_work_records w JOIN hq_customers c ON c.id=w.customer_id
        WHERE w.id=?""", (candidate["work_id"],)).fetchone()
    if work is None or work["revision"] != candidate["work_revision"]:
        raise ValueError("Native work changed; withdraw and restage this candidate")
    current = [str(work[key]) for key in ("customer_id", "name", "email", "phone")]
    stamp = hashlib.sha256(json.dumps(current, ensure_ascii=True,
                                      separators=(",", ":")).encode()).hexdigest()
    source_count = sum(_norm(_cell(row, 0)) == source_name
                       for row in customers[1:]) if source_name else 0
    native_count = sum(_norm(row["name"]) == _norm(work["name"])
                       for row in db.execute("SELECT name FROM hq_customers"))
    return {"customer_id": work["customer_id"], "stamp": stamp,
            "same_name": bool(source_name and source_name == _norm(work["name"])),
            "source_count": source_count, "native_count": native_count}


def save_customer_identity_review(*, candidate_id, expected_revision, decision, basis,
                                  formula, computed, owner_identity):
    """Save a provisional customer match decision; NEVER import/link a customer."""
    if not storage_ready() or not isinstance(candidate_id, str) or not _HEX.fullmatch(candidate_id):
        raise ValueError("Invalid candidate or persistent storage")
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**31:
        raise ValueError("Invalid identity-review revision")
    if decision not in _IDENTITY_DECISIONS or basis not in _IDENTITY_BASIS:
        raise ValueError("Invalid identity review decision or basis")
    if decision == "potential_match" and basis == "none":
        raise ValueError("Potential match requires an explicit evidence basis")
    actor, now = _actor_fingerprint(owner_identity), datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _review_tables(db)
        _field_review_tables(db)
        _identity_review_tables(db)
        db.execute("BEGIN IMMEDIATE")
        candidate = db.execute("""SELECT work_id,work_revision,job_ref,sheet_row,
            source_stamp,withdrawn_at FROM hq_historical_j_candidates WHERE id=?""",
            (candidate_id,)).fetchone()
        if candidate is None or candidate["withdrawn_at"] is not None:
            raise ValueError("Historical candidate withdrawn or missing")
        evidence = _identity_evidence(db, candidate, formula, computed)
        assessment = db.execute("""SELECT revision,assessments,source_stamp,work_revision
            FROM hq_historical_candidate_field_reviews WHERE candidate_id=?""",
            (candidate_id,)).fetchone()
        assessment_revision = 0
        if assessment and (assessment["source_stamp"], assessment["work_revision"]) == (
                candidate["source_stamp"], candidate["work_revision"]):
            assessment_revision = assessment["revision"]
        customer_assessment = (json.loads(assessment["assessments"]).get("customer")
                               if assessment_revision else None)
        if decision == "potential_match":
            if customer_assessment != "appears_consistent":
                raise ValueError("Assess customer identity as consistent first")
            if not (evidence["same_name"] and evidence["source_count"] == 1
                    and evidence["native_count"] == 1):
                raise ValueError("Ambiguous or mismatched customer identity; hold for evidence")
        elif decision == "conflict" and customer_assessment != "conflict":
            raise ValueError("Record a conflicting customer field assessment first")
        prior = db.execute("""SELECT revision,source_stamp,work_revision,customer_id,
            customer_stamp,assessment_revision,decision,basis
            FROM hq_historical_identity_reviews WHERE candidate_id=?""",
            (candidate_id,)).fetchone()
        current = (candidate["source_stamp"], candidate["work_revision"],
                   evidence["customer_id"], evidence["stamp"], assessment_revision,
                   decision, basis)
        if prior and tuple(prior[key] for key in (
                "source_stamp", "work_revision", "customer_id", "customer_stamp",
                "assessment_revision", "decision", "basis")) == current:
            return prior["revision"]  # exact retry, no new audit event
        if (prior["revision"] if prior else 0) != expected_revision:
            raise ValueError("Identity review changed; reload before saving")
        revision = expected_revision + 1
        db.execute("""INSERT INTO hq_historical_identity_reviews
            (candidate_id,revision,source_stamp,work_revision,customer_id,
             customer_stamp,assessment_revision,decision,basis,actor_fingerprint,updated_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(candidate_id) DO UPDATE SET
             revision=excluded.revision,source_stamp=excluded.source_stamp,
             work_revision=excluded.work_revision,customer_id=excluded.customer_id,
             customer_stamp=excluded.customer_stamp,assessment_revision=excluded.assessment_revision,
             decision=excluded.decision,basis=excluded.basis,
             actor_fingerprint=excluded.actor_fingerprint,updated_at=excluded.updated_at""",
            (candidate_id,revision,*current,actor,now))
        db.execute("""INSERT INTO hq_historical_identity_review_events
            (id,candidate_id,revision,source_stamp,work_revision,customer_id,
             customer_stamp,assessment_revision,decision,basis,actor_fingerprint,occurred_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex,candidate_id,revision,*current,actor,now))
        return revision


@migration_preview.route("/hq/migration/candidates/<candidate_id>/review", methods=["GET", "POST"])
@required
def candidate_field_review_page(candidate_id):
    try:
        candidate = _candidate_for_review(candidate_id)
    except (sqlite3.Error, OSError, ValueError):
        candidate = None
    if candidate is None or candidate["withdrawn_at"] is not None:
        abort(404)
    comparison, error = None, None
    saved = request.args.get("saved", "") == "1"
    if request.method == "POST":
        if request.content_length is None or request.content_length > 4096:
            abort(413)
        if not secrets.compare_digest(
                request.form.get("csrf", ""), session.get("csrf", "not-set")):
            abort(403)
        action = request.form.get("action", "")
        if action not in ("inspect", "save", "save_ownership", "save_identity"):
            abort(400)
        provider = current_app.extensions["hq_sheets_connection"]()
        if not provider or provider[1] <= time.time():
            error = "Connect your owner Google Sheets read-only account first."
        else:
            try:
                formula, computed = _read_live(provider[0])
                comparison = _field_comparison(formula, computed, candidate)
                if action == "save":
                    revision = int(request.form.get("revision", "-1"))
                    assessments = {key: request.form.get(key, "") for key, _ in _REVIEW_FIELDS}
                    save_candidate_field_review(
                        candidate_id=candidate_id, expected_revision=revision,
                        assessments=assessments, formula=formula, computed=computed,
                        owner_identity=session.get("google_sub"))
                elif action == "save_identity":
                    revision = int(request.form.get("identity_revision", "-1"))
                    save_customer_identity_review(
                        candidate_id=candidate_id, expected_revision=revision,
                        decision=request.form.get("identity_decision", ""),
                        basis=request.form.get("identity_basis", ""),
                        formula=formula, computed=computed,
                        owner_identity=session.get("google_sub"))
                elif action == "save_ownership":
                    revision = int(request.form.get("ownership_revision", "-1"))
                    selections = {key: request.form.get("ownership_" + key, "")
                                  for key, _ in _REVIEW_FIELDS}
                    save_candidate_field_ownership(
                        candidate_id=candidate_id, expected_revision=revision,
                        selections=selections, formula=formula, computed=computed,
                        owner_identity=session.get("google_sub"))
                if action != "inspect":
                    return redirect(url_for(".candidate_field_review_page",
                                            candidate_id=candidate_id, saved="1"), code=303)
            except (ValueError, sqlite3.Error, OSError):
                error = "Source or HQ records changed, or the assessment was invalid. Nothing was saved."
                comparison = None
            except Exception:
                current_app.logger.warning("Historical field assessment unavailable")
                error = "Comparison unavailable. No changes made."
                comparison = None
    return render_template("hq_migration_candidate.html",
                           candidate=candidate, comparison=comparison,
                           fields=_REVIEW_FIELDS, decisions=_REVIEW_DECISIONS,
                           error=error, saved=saved, csrf=session.get("csrf", ""))


@migration_preview.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@migration_preview.route("/hq/migration/preview", methods=["GET", "POST"])
@required
def page():
    result = None
    error = None
    saved = request.args.get("saved", "") == "1"
    if request.method == "POST":
        if request.content_length is None or request.content_length > 4096:
            abort(413)
        if not secrets.compare_digest(
            request.form.get("csrf", ""), session.get("csrf", "not-set")
        ):
            return "Invalid CSRF token", 403
        action = request.form.get("action", "compare")
        if action == "withdraw":
            try:
                withdraw_j_candidate(request.form.get("candidate_id", ""),
                                     int(request.form.get("revision", "-1")),
                                     session.get("google_sub"))
            except (ValueError, LookupError, sqlite3.Error, OSError):
                error = "The proposal changed or could not be withdrawn. Reload to review."
            else:
                return redirect(url_for(".page", saved="1"), code=303)
        elif action in ("compare", "propose"):
            # Pagination and exact J lookup are always fresh, owner-initiated
            # provider reads; never cache historical source cells in sessions.
            try:
                page_index = int(request.form.get("page_index", "0"))
                find_ref = request.form.get("find_ref", "").strip()
                if type(page_index) is not int or not 0 <= page_index <= 16:
                    raise ValueError("Invalid historical review page")
                if find_ref and not J_REF.fullmatch(find_ref):
                    raise ValueError("Enter an exact uppercase J-series reference")
            except (ValueError, TypeError):
                error = "Invalid historical review page or J reference."
                page_index, find_ref = 0, ""
            if error:
                return render_template("hq_migration_preview.html",
                    result=None, error=error, csrf=session.get("csrf", ""),
                    candidates=_current_candidates(), work_options=[], saved=False), 400
            # Google Sheets is fetched only after a deliberate owner POST.
            provider = current_app.extensions["hq_sheets_connection"]()
            if not provider or provider[1] <= time.time():
                error = "Connect your owner Google Sheets read-only account first."
            else:
                try:
                    formula, computed = _read_live(provider[0])
                    result = build_preview(formula, computed, native=_native_inventory(),
                                           page_index=page_index, find_ref=find_ref)
                    for entry in result["sample"]:
                        if entry["status"] == "review":
                            entry["source_stamp"] = _candidate_stamp(formula, computed, entry["row"])
                            entry["operation_id"] = uuid.uuid4().hex
                    if action == "propose":
                        row = int(request.form.get("sheet_row", "0"))
                        stage_j_candidate(
                            formula=formula, computed=computed, sheet_row=row,
                            job_ref=request.form.get("job_ref", ""),
                            source_stamp=request.form.get("source_stamp", ""),
                            work_token=request.form.get("work_token", ""),
                            operation_id=request.form.get("operation_id", ""),
                            owner_identity=session.get("google_sub"))
                        return redirect(url_for(".page", saved="1"), code=303)
                except (ValueError, sqlite3.Error, OSError) as exc:
                    # Validation errors must not reveal raw source PII.
                    error = str(exc) if isinstance(exc, ValueError) else "HQ storage unavailable"
                except Exception:
                    current_app.logger.warning("Read-only Jobs/Customers preview unavailable")
                    error = "The source could not be fully verified. No changes were made."
        else:
            abort(400)
    try:
        candidates = _current_candidates()
        options = _native_work_options() if result else []
    except (sqlite3.Error, OSError):
        candidates, options = [], []
        error = error or "Saved review queue is unavailable."
    return render_template("hq_migration_preview.html", result=result,
                           error=error, csrf=session.get("csrf", ""),
                           candidates=candidates, work_options=options, saved=saved)
