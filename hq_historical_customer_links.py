"""Owner-curated historical Customers-sheet matching proposals (NEVER an import).

Both Sheets reads are fresh, read-only and owner initiated. A proposal stores
only source coordinates and keyed source/native fingerprints, not sheet PII.
It never changes canonical customers, Jobs, external providers or authority.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for

from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_core_records import _schema
from hq_historical_preview import _cell, _check_source, _norm, _ranges, _read_live
from hq_job_sync import WORKBOOK
from hq_work_progress import _actor_fingerprint

customer_migration = Blueprint("customer_migration", __name__)
CUSTOMERS_SHEET_ID = 378990021
HEX = re.compile(r"[0-9a-f]{32}\Z")
STAMP = re.compile(r"[0-9a-f]{64}\Z")
BASIS = ("name_crosscheck", "owner_document_review")
PAGE_SIZE = 40


def _secret():
    key = current_app.secret_key
    if not isinstance(key, str) or not key:
        raise RuntimeError("Persistent HQ session secret required")
    return key.encode()


def _stamp(payload):
    message = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode()
    return hmac.new(_secret(), message, hashlib.sha256).hexdigest()


def _source_rows(formula, computed):
    _, customer_rows = _check_source(formula, computed)
    _, compared = _ranges(computed)
    return customer_rows, compared


def _source_row(formula, computed, sheet_row):
    if type(sheet_row) is not int or not 5 <= sheet_row <= 1000:
        raise ValueError("Invalid Customers sheet row")
    originals, compared = _source_rows(formula, computed)
    idx = sheet_row - 4
    if not 1 <= idx < len(originals) or idx >= len(compared):
        raise ValueError("Source row missing; run a fresh comparison")
    raw = originals[idx]
    name = _cell(raw, 0)
    if not isinstance(name, str) or not _norm(name) or len(name) > 160:
        raise ValueError("Customer name must be a literal and nonblank")
    normalized = _norm(name)
    names = Counter(_norm(_cell(r, 0)) for r in originals[1:])
    if names[normalized] != 1:
        raise ValueError("Duplicate Customers-sheet name; resolve identity first")
    return raw, compared[idx]


def source_stamp(formula, computed, sheet_row):
    raw, effective = _source_row(formula, computed, sheet_row)
    return _stamp([WORKBOOK, CUSTOMERS_SHEET_ID, sheet_row, raw, effective])


def review_rows(formula, computed, page_index=0):
    """Owner-only, bounded page of live Customers rows; never stored in HQ."""
    if type(page_index) is not int or not 0 <= page_index <= 24:
        raise ValueError("Invalid review page")
    originals, compared = _source_rows(formula, computed)
    names = Counter(_norm(_cell(row, 0)) for row in originals[1:])
    rows = []
    for idx, row in enumerate(originals[1:], start=5):
        name = _cell(row, 0)
        if not isinstance(name, str) or not _norm(name):
            continue
        label = "duplicate_name" if names[_norm(name)] > 1 else "review"
        rows.append({"row": idx, "name": name[:160],
                     "type": str(_cell(row, 1))[:50],
                     "postcode": str(_cell(row, 2))[:30],
                     "status": label,
                     "source_stamp": _stamp([WORKBOOK, CUSTOMERS_SHEET_ID, idx,
                                              row, compared[idx]])
                     if label == "review" else None,
                     "operation_id": uuid.uuid4().hex})
    page_count = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    if page_index >= page_count:
        raise ValueError("Historical review page changed; start again")
    return {"rows": rows[page_index * PAGE_SIZE:(page_index + 1) * PAGE_SIZE],
            "total": len(rows), "duplicate_rows": sum(
                x["status"] == "duplicate_name" for x in rows),
            "page": page_index, "pages": page_count}


def _native_digest(row):
    # Detect changes in customer name/contact and source identity without
    # storing names, email or phone in the historical proposal tables.
    return _stamp([row["id"], row["name"], row["email"],
                   row["phone"], row["created_at"]])


def _schema_candidates(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_customer_candidates (
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE,
        workbook_id TEXT NOT NULL,
        sheet_id INTEGER NOT NULL,
        sheet_row INTEGER NOT NULL CHECK(sheet_row BETWEEN 5 AND 1000),
        source_stamp TEXT NOT NULL,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        native_stamp TEXT NOT NULL,
        basis TEXT NOT NULL CHECK(basis IN ('name_crosscheck','owner_document_review')),
        actor_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        withdrawn_at TEXT,
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision IN (0,1))
    )""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_customer_candidate_current_row
        ON hq_historical_customer_candidates(workbook_id,sheet_id,sheet_row)
        WHERE withdrawn_at IS NULL""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_customer_candidate_current_native
        ON hq_historical_customer_candidates(customer_id)
        WHERE withdrawn_at IS NULL""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_customer_candidate_events (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL REFERENCES hq_historical_customer_candidates(id),
        revision INTEGER NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('proposed','withdrawn')),
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(candidate_id,revision)
    )""")


def _native_row(db, customer_id):
    archived = db.execute("""SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='hq_customer_archive_state'""").fetchone()
    clause = (""" AND NOT EXISTS(SELECT 1 FROM hq_customer_archive_state a
        WHERE a.customer_id=c.id AND a.is_archived=1)""" if archived else "")
    return db.execute("""SELECT c.id,c.name,c.email,c.phone,c.created_at
        FROM hq_customers c WHERE c.id=?""" + clause, (customer_id,)).fetchone()


def stage_customer_candidate(*, formula, computed, sheet_row, expected_source_stamp,
                             customer_id, basis, operation_id, owner_identity):
    """Create an inactive, versioned proposal only after both identities recheck."""
    if not storage_ready():
        raise ValueError("Persistent storage not configured")
    if (not isinstance(operation_id, str) or not HEX.fullmatch(operation_id)
            or not isinstance(customer_id, str) or not HEX.fullmatch(customer_id)):
        raise ValueError("Invalid customer or submission reference")
    if basis not in BASIS:
        raise ValueError("Choose the review basis")
    if not isinstance(expected_source_stamp, str) or not STAMP.fullmatch(expected_source_stamp):
        raise ValueError("Invalid source snapshot")
    raw, _ = _source_row(formula, computed, sheet_row)
    digest = source_stamp(formula, computed, sheet_row)
    if not hmac.compare_digest(digest, expected_source_stamp):
        raise ValueError("Source changed; run a fresh comparison")
    actor = _actor_fingerprint(owner_identity)
    now = datetime.now(timezone.utc).isoformat()
    try:
        with connect() as db:
            db.execute("PRAGMA foreign_keys=ON")
            _schema(db)
            _schema_candidates(db)
            db.execute("BEGIN IMMEDIATE")
            native = _native_row(db, customer_id)
            if native is None:
                raise ValueError("Native customer is unavailable or archived")
            native_digest = _native_digest(native)
            if basis == "name_crosscheck":
                normalized = _norm(raw[0])
                count = sum(_norm(r["name"]) == normalized for r in db.execute(
                    "SELECT name FROM hq_customers"))
                if _norm(native["name"]) != normalized or count != 1:
                    raise ValueError("Name-only match is ambiguous; review independent evidence")
            prior = db.execute("""SELECT id,sheet_row,source_stamp,customer_id,native_stamp,basis
                FROM hq_historical_customer_candidates WHERE operation_id=?""",
                (operation_id,)).fetchone()
            if prior:
                if (prior["sheet_row"], prior["source_stamp"], prior["customer_id"],
                    prior["native_stamp"], prior["basis"]) != (
                        sheet_row, digest, customer_id, native_digest, basis):
                    raise ValueError("Submission ID already used for different evidence")
                return prior["id"]
            candidate_id = uuid.uuid4().hex
            db.execute("""INSERT INTO hq_historical_customer_candidates
                (id,operation_id,workbook_id,sheet_id,sheet_row,source_stamp,
                 customer_id,native_stamp,basis,actor_fingerprint,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id,operation_id,WORKBOOK,CUSTOMERS_SHEET_ID,sheet_row,
                 digest,customer_id,native_digest,basis,actor,now))
            db.execute("""INSERT INTO hq_historical_customer_candidate_events
                VALUES(?,?,?,?,?,?)""", (uuid.uuid4().hex,candidate_id,0,
                                         "proposed",actor,now))
            return candidate_id
    except sqlite3.IntegrityError as exc:
        raise ValueError("Collision: this sheet row or HQ customer already has an active proposal") from exc


def withdraw_customer_candidate(candidate_id, expected_revision, owner_identity):
    if (not storage_ready() or not isinstance(candidate_id, str)
            or not HEX.fullmatch(candidate_id) or expected_revision != 0
            or type(expected_revision) is not int):
        raise ValueError("Invalid proposal or revision")
    actor = _actor_fingerprint(owner_identity)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _schema_candidates(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT revision,withdrawn_at FROM
            hq_historical_customer_candidates WHERE id=?""", (candidate_id,)).fetchone()
        if row is None:
            raise LookupError("Candidate not found")
        if row["withdrawn_at"] is not None:
            return  # exact replay is harmless and creates no duplicate event
        if row["revision"] != expected_revision:
            raise ValueError("Candidate changed; reload")
        now = datetime.now(timezone.utc).isoformat()
        db.execute("""UPDATE hq_historical_customer_candidates
            SET withdrawn_at=?,revision=1 WHERE id=? AND revision=0
            AND withdrawn_at IS NULL""", (now,candidate_id))
        db.execute("""INSERT INTO hq_historical_customer_candidate_events
            VALUES(?,?,?,?,?,?)""", (uuid.uuid4().hex,candidate_id,1,
                                     "withdrawn",actor,now))


def current_candidates():
    """Read existing proposals without constructing or modifying storage."""
    root = os.environ.get("HQ_DATA_DIR", "")
    path = Path(root) / "customer_requests.sqlite3" if root and Path(root).is_absolute() else None
    if path is None or not path.is_file():
        return []
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=3) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        if not db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='hq_historical_customer_candidates'""").fetchone():
            return []
        return [dict(r) for r in db.execute("""SELECT id,sheet_row,customer_id,basis,
            created_at,revision FROM hq_historical_customer_candidates
            WHERE withdrawn_at IS NULL ORDER BY created_at DESC,id DESC LIMIT 100""")]


def native_options():
    """Display native record names for a bounded owner-only selection control."""
    root = os.environ.get("HQ_DATA_DIR", "")
    path = Path(root) / "customer_requests.sqlite3" if root and Path(root).is_absolute() else None
    if path is None or not path.is_file():
        return []
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=3) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        if not db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='hq_customers'""").fetchone():
            return []
        return [dict(r) for r in db.execute("""SELECT id,name
            FROM hq_customers ORDER BY name COLLATE NOCASE,id LIMIT 250""")]


@customer_migration.after_request
def private_headers(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@customer_migration.route("/hq/migration/customers", methods=["GET", "POST"])
@required
def page():
    result, error = None, None
    saved = request.args.get("saved") == "1"
    if request.method == "POST":
        if request.content_length is None or request.content_length > 4096:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""),
                                      session.get("csrf", "not-set")):
            abort(403)
        action = request.form.get("action", "")
        if action == "withdraw":
            try:
                value = request.form.get("revision", "")
                if not re.fullmatch(r"(0|[1-9][0-9]{0,9})", value):
                    raise ValueError("Invalid revision")
                withdraw_customer_candidate(
                    request.form.get("candidate_id", ""), int(value),
                    session.get("google_sub"))
                return redirect(url_for(".page", saved="1"), code=303)
            except (ValueError, LookupError, sqlite3.Error, OSError):
                error = "Proposal changed or storage is unavailable. Nothing was changed."
        elif action in ("compare", "stage"):
            try:
                raw_page = request.form.get("page", "0")
                if not re.fullmatch(r"(0|[1-9][0-9]{0,2})", raw_page):
                    raise ValueError("Invalid page")
                index = int(raw_page)
                provider = current_app.extensions["hq_sheets_connection"]()
                if not provider or provider[1] <= time.time():
                    raise ValueError("Connect owner Google Sheets read-only access first")
                formula, computed = _read_live(provider[0])
                result = review_rows(formula, computed, index)
                if action == "stage":
                    raw_row = request.form.get("sheet_row", "")
                    if not re.fullmatch(r"[0-9]{1,4}", raw_row):
                        raise ValueError("Invalid source row")
                    stage_customer_candidate(
                        formula=formula, computed=computed, sheet_row=int(raw_row),
                        expected_source_stamp=request.form.get("source_stamp", ""),
                        customer_id=request.form.get("customer_id", ""),
                        basis=request.form.get("basis", ""),
                        operation_id=request.form.get("operation_id", ""),
                        owner_identity=session.get("google_sub"))
                    return redirect(url_for(".page", saved="1"), code=303)
            except (ValueError, sqlite3.Error, OSError):
                error = "Could not validate current source/customer identity. Nothing saved."
            except Exception:
                current_app.logger.warning("Owner Customers migration preview unavailable")
                error = "Read-only comparison unavailable; nothing was changed."
        else:
            abort(400)
    try:
        candidates = current_candidates()
        customers = native_options() if result else []
    except (OSError, sqlite3.Error):
        candidates, customers = [], []
        error = error or "Native HQ customer records unavailable"
    return render_template("hq_historical_customers.html", csrf=session.get("csrf", ""),
                           result=result, error=error, saved=saved,
                           candidates=candidates, customers=customers)
