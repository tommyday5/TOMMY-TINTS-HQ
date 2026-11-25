"""Bounded, reversible historical customer provenance links.

One already-existing canonical customer; no profile, pricing, job or Sheets writes.
A link records the owner's reviewed historical identity, not ongoing source authority.
"""
from contextlib import closing
from datetime import datetime, timezone
import hmac
import json
import secrets
import sqlite3
import time

from flask import abort, current_app, render_template, request, session
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_core_records import _schema
from hq_historical_customer_field_review import (
    HEX, _helpers, _tables as field_tables, _candidate_context, _tier,
)
from hq_private_recovery import _paths, _existing, _restore_check, MAX_SECONDS
from hq_work_progress import _actor_fingerprint


def tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_approved_customer_source_links (
        candidate_id TEXT PRIMARY KEY REFERENCES hq_historical_customer_candidates(id),
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        workbook_id TEXT NOT NULL, sheet_id INTEGER NOT NULL, sheet_row INTEGER NOT NULL,
        manifest TEXT NOT NULL, manifest_stamp TEXT NOT NULL UNIQUE,
        approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
        reversed_by TEXT, reversed_at TEXT
    )""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_approved_customer_source_row
        ON hq_approved_customer_source_links(workbook_id,sheet_id,sheet_row)
        WHERE reversed_at IS NULL""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_approved_customer_source_native
        ON hq_approved_customer_source_links(customer_id) WHERE reversed_at IS NULL""")


def _init(db):
    db.execute("PRAGMA foreign_keys=ON")
    _schema(db)
    _helpers()._schema_candidates(db)
    field_tables(db)
    tables(db)


def _identity(db, candidate_id, source):
    original = _helpers()
    candidate, raw, native, tier = _candidate_context(db, candidate_id, *source)
    full = db.execute("SELECT * FROM hq_historical_customer_candidates WHERE id=?",
                      (candidate_id,)).fetchone()
    review = db.execute("SELECT * FROM hq_historical_customer_field_reviews WHERE candidate_id=?",
                        (candidate_id,)).fetchone()
    if (full["basis"] != "owner_document_review" or not review
            or review["source_stamp"] != candidate["source_stamp"]
            or review["native_stamp"] != candidate["native_stamp"]
            or review["tier_revision"] != tier["revision"]
            or review["name_decision"] != "candidate_agreement"
            or review["account_type_decision"] != "candidate_agreement"
            or review["postcode_decision"] != "not_mapped"):
        raise ValueError("Require document-reviewed identity, matching name and reviewed tier, and explicitly unmapped postcode.")
    raw, effective = original._source_row(*source, candidate["sheet_row"])
    alias = {"trader":"trade", "trade":"trade", "retail":"retail", "maintenance":"maintenance"}
    if (len(raw) < 2 or len(effective) < 2
            or any(not isinstance(v,str) or v.startswith("=") for v in raw[:2])
            or raw[:2] != effective[:2]
            or original._norm(raw[0]) != original._norm(native["name"])
            or not tier["reviewed"] or alias.get(original._norm(raw[1])) != tier["tier"]
            or sum(original._norm(r["name"]) == original._norm(raw[0])
                   for r in db.execute("SELECT name FROM hq_customers")) != 1):
        raise ValueError("Unique literal customer name and reviewed account tier must agree.")
    return dict(full), dict(review), dict(native), tier


def _snapshot_check(snapshot_id, identity):
    if not HEX.fullmatch(snapshot_id):
        raise ValueError("Select a verified private snapshot.")
    _, directory = _paths()
    if not any(x["id"] == snapshot_id for x in _existing(directory)):
        raise ValueError("Recovery snapshot missing.")
    path = directory / (snapshot_id + ".sqlite3")
    _restore_check(path, time.monotonic() + MAX_SECONDS)
    candidate, review, native, tier = identity
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=1)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        for table, key, before in (
            ("hq_historical_customer_candidates","id",candidate),
            ("hq_historical_customer_field_reviews","candidate_id",review),
        ):
            row = db.execute("SELECT * FROM "+table+" WHERE "+key+"=?", (candidate["id"],)).fetchone()
            if not row or dict(row) != before:
                raise ValueError("Snapshot predates the current candidate or field review.")
        saved = _helpers()._native_row(db, native["id"])
        if not saved or dict(saved) != native or _tier(db,native["id"]) != tier:
            raise ValueError("Snapshot does not contain the current canonical identity and tier.")


def _manifest(db, candidate_id, snapshot_id, source):
    identity = _identity(db,candidate_id,source)
    candidate, review, native, tier = identity
    if db.execute("""SELECT 1 FROM hq_approved_customer_source_links
        WHERE candidate_id=? OR (reversed_at IS NULL AND
        (customer_id=? OR (workbook_id=? AND sheet_id=? AND sheet_row=?)))""",
        (candidate_id,native["id"],candidate["workbook_id"],candidate["sheet_id"],
         candidate["sheet_row"])).fetchone():
        raise ValueError("Candidate already used or an active source/customer link exists.")
    _snapshot_check(snapshot_id,identity)
    return dict(version=1,candidate_id=candidate_id,customer_id=native["id"],
        workbook_id=candidate["workbook_id"],sheet_id=candidate["sheet_id"],
        sheet_row=candidate["sheet_row"],source_stamp=candidate["source_stamp"],
        native_stamp=candidate["native_stamp"],field_revision=review["revision"],
        tier_revision=tier["revision"],tier=tier["tier"],snapshot_id=snapshot_id,
        customers_linked=1,customers_created=0,vehicles_created=0,jobs_changed=0,
        fields_overwritten=0,unresolved_link_conflicts=0,
        field_ownership="unchanged; name and tier compared, postcode not mapped")


def review(candidate_id,snapshot_id,source,owner,stamp=None):
    if not storage_ready() or not HEX.fullmatch(candidate_id):
        raise ValueError("Persistent storage and candidate required.")
    actor = _actor_fingerprint(owner)
    with closing(connect()) as db, db:
        _init(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT * FROM hq_approved_customer_source_links WHERE candidate_id=?",
                           (candidate_id,)).fetchone()
        if prior and stamp and hmac.compare_digest(prior["manifest_stamp"],stamp):
            if prior["approved_by"] != actor or prior["reversed_at"]:
                raise ValueError("Link was reversed or belongs to another approval.")
            return None
        manifest = _manifest(db,candidate_id,snapshot_id,source)
        expected = _helpers()._stamp(["approved-customer-link-v1",manifest,actor])
        if stamp is None:
            return manifest,expected
        if not hmac.compare_digest(expected,stamp):
            raise ValueError("Exact approval changed; prepare a new review.")
        db.execute("""INSERT INTO hq_approved_customer_source_links
            (candidate_id,customer_id,workbook_id,sheet_id,sheet_row,manifest,
             manifest_stamp,approved_by,approved_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (candidate_id,manifest["customer_id"],manifest["workbook_id"],manifest["sheet_id"],
             manifest["sheet_row"],json.dumps(manifest,sort_keys=True),expected,actor,
             datetime.now(timezone.utc).isoformat()))
        return None


def reverse(candidate_id,stamp,owner):
    if not storage_ready() or not HEX.fullmatch(candidate_id):
        raise ValueError("Invalid link.")
    with closing(connect()) as db, db:
        _init(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM hq_approved_customer_source_links WHERE candidate_id=?",
                         (candidate_id,)).fetchone()
        if not row or not hmac.compare_digest(row["manifest_stamp"],stamp):
            raise ValueError("Link changed.")
        if not row["reversed_at"]:
            db.execute("""UPDATE hq_approved_customer_source_links
                SET reversed_at=?,reversed_by=? WHERE candidate_id=?""",
                (datetime.now(timezone.utc).isoformat(),_actor_fingerprint(owner),candidate_id))


def history():
    if not storage_ready():
        raise ValueError("Persistent storage unavailable.")
    with closing(connect()) as db, db:
        _init(db)
        return [dict(r) for r in db.execute("""SELECT candidate_id,customer_id,sheet_row,
            manifest_stamp,approved_at,reversed_at FROM hq_approved_customer_source_links
            ORDER BY approved_at DESC LIMIT 500""")]


def register_routes(bp):
    @bp.route("/hq/migration/customers/approved-links",methods=["GET"])
    @bp.route("/hq/migration/customers/<candidate_id>/approved-link",methods=["GET","POST"])
    @required
    def approved_customer_link(candidate_id=None):
        if candidate_id is not None and not HEX.fullmatch(candidate_id):
            abort(404)
        manifest = stamp = error = notice = None
        snapshots = []
        status = 200
        try:
            if request.method == "POST":
                if request.content_length is None or request.content_length > 4096:
                    abort(413)
                if not secrets.compare_digest(request.form.get("csrf",""),session.get("csrf","not-set")):
                    abort(403)
                action = request.form.get("action","")
                if action == "reverse":
                    if request.form.get("confirm") != "yes":
                        raise ValueError("Confirm reversal of the exact link.")
                    reverse(candidate_id,request.form.get("stamp",""),session.get("google_sub"))
                    notice = "Historical link reversed; the original approval remains in history."
                elif action in ("prepare","apply"):
                    if action == "apply" and (request.form.get("confirm") != "yes"
                            or not _helpers().STAMP.fullmatch(request.form.get("stamp",""))):
                        raise ValueError("Confirm the exact prepared link.")
                    provider = current_app.extensions["hq_sheets_connection"]()
                    if not provider or provider[1] <= time.time():
                        raise ValueError("Connect read-only Sheets access first.")
                    source = _helpers()._read_live(provider[0])
                    result = review(candidate_id,request.form.get("snapshot_id",""),source,
                                    session.get("google_sub"),
                                    request.form.get("stamp") if action == "apply" else None)
                    if result:
                        manifest,stamp = result
                    else:
                        notice = "Approved historical link saved. No customer fields or Sheets values changed."
                else:
                    abort(400)
            if candidate_id:
                _, directory = _paths()
                snapshots = _existing(directory)
            rows = history()
        except (ValueError,LookupError) as exc:
            error,status = str(exc),409
            rows = []
        except (sqlite3.Error,OSError,RuntimeError):
            error,status = "Storage or source unavailable; no approval was established.",503
            rows = []
        return render_template("hq_approved_customer_links.html",candidate_id=candidate_id,
            manifest=manifest,stamp=stamp,error=error,notice=notice,rows=rows,
            snapshots=snapshots,csrf=session.get("csrf","")),status
