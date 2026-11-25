"""One explicitly approved, reversible historical postcode import.

Only fills a never-edited native postcode on a previously linked customer.
No workbook, portal, tariff, routing, job or integration changes.
"""
from contextlib import closing
from datetime import datetime, timezone
import hmac
import json
import secrets
import sqlite3
import time
import uuid

from flask import abort, current_app, render_template, request, session
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_customer_postcodes import normalize_postcode, postcode_status, _tables as postcode_tables
from hq_work_progress import _actor_fingerprint


def _helpers():
    import hq_approved_customer_links as links
    return links


def _tables(db):
    postcode_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_postcode_imports (
        candidate_id TEXT PRIMARY KEY REFERENCES hq_approved_customer_source_links(candidate_id),
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        manifest TEXT NOT NULL, stamp TEXT NOT NULL UNIQUE,
        actor TEXT NOT NULL, applied_at TEXT NOT NULL,
        reversed_at TEXT, reversed_by TEXT
    )""")


def _init(db):
    _helpers()._init(db)
    _tables(db)


def _active_link(db, candidate_id):
    link = db.execute("""SELECT * FROM hq_approved_customer_source_links
        WHERE candidate_id=? AND reversed_at IS NULL""", (candidate_id,)).fetchone()
    if not link:
        raise ValueError("An active approved historical customer link is required.")
    return dict(link)


def _manifest(db, candidate_id, snapshot_id, source):
    links = _helpers()
    link = _active_link(db, candidate_id)
    candidate, review, native, tier = links._identity(db, candidate_id, source)
    original = json.loads(link["manifest"])
    if any(original[key] != value for key, value in (
        ("source_stamp", candidate["source_stamp"]), ("native_stamp", candidate["native_stamp"]),
        ("field_revision", review["revision"]), ("tier_revision", tier["revision"]),
        ("customer_id", native["id"]), ("tier", tier["tier"]),
    )):
        raise ValueError("Approved link evidence changed; review the identity again.")
    raw, effective = links._helpers()._source_row(*source, candidate["sheet_row"])
    if (len(raw) < 3 or len(effective) < 3 or not isinstance(raw[2], str)
            or raw[2] != effective[2] or raw[2].startswith("=")):
        raise ValueError("Require a literal, unchanged source postcode.")
    postcode = normalize_postcode(raw[2])
    if not postcode:
        raise ValueError("Historical postcode is blank.")
    if postcode_status(db, native["id"])["revision"] != 0:
        raise ValueError("Native postcode has already been edited; never overwrite it.")
    links._snapshot_check(snapshot_id, (candidate, review, native, tier))
    _, directory = links._paths()
    path = directory / (snapshot_id + ".sqlite3")
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=1)) as saved:
        saved.row_factory = sqlite3.Row
        saved.execute("PRAGMA query_only=ON")
        if not saved.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_approved_customer_source_links'").fetchone():
            raise ValueError("Snapshot predates the approved historical link.")
        if _active_link(saved, candidate_id) != link or postcode_status(saved, native["id"])["revision"] != 0:
            raise ValueError("Snapshot must include the approved link and untouched native postcode.")
    return dict(version=1, candidate_id=candidate_id, customer_id=native["id"],
                source_row=candidate["sheet_row"], source_stamp=candidate["source_stamp"],
                link_stamp=link["manifest_stamp"], snapshot_id=snapshot_id,
                postcode=postcode, previous_revision=0, result_revision=1,
                fields_filled=1, fields_overwritten=0, jobs_changed=0)


def _write_postcode(db, customer_id, revision, previous, value, actor, now):
    db.execute("""INSERT INTO hq_customer_postcodes(customer_id,postcode,revision,updated_at)
        VALUES(?,?,?,?) ON CONFLICT(customer_id) DO UPDATE SET
        postcode=excluded.postcode,revision=excluded.revision,updated_at=excluded.updated_at""",
        (customer_id,value,revision,now))
    db.execute("""INSERT INTO hq_customer_postcode_events VALUES(?,?,?,?,?,?,?)""",
        (uuid.uuid4().hex,customer_id,revision,previous,value,actor,now))


def review(candidate_id, snapshot_id, source, owner, stamp=None):
    if not storage_ready() or not _helpers().HEX.fullmatch(candidate_id):
        raise ValueError("Persistent storage and valid candidate required.")
    actor = _actor_fingerprint(owner)
    with closing(connect()) as db, db:
        _init(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT * FROM hq_historical_postcode_imports WHERE candidate_id=?",
                           (candidate_id,)).fetchone()
        if prior:
            if (stamp and hmac.compare_digest(prior["stamp"],stamp)
                    and prior["actor"] == actor and not prior["reversed_at"]):
                return None
            raise ValueError("This candidate already has an import history.")
        manifest = _manifest(db,candidate_id,snapshot_id,source)
        expected = _helpers()._helpers()._stamp(["historical-postcode-v1",manifest,actor])
        if stamp is None:
            return manifest,expected
        if not hmac.compare_digest(expected,stamp):
            raise ValueError("Approval changed; prepare a new review.")
        now = datetime.now(timezone.utc).isoformat()
        _write_postcode(db,manifest["customer_id"],1,"",manifest["postcode"],actor,now)
        db.execute("""INSERT INTO hq_historical_postcode_imports
            (candidate_id,customer_id,manifest,stamp,actor,applied_at) VALUES(?,?,?,?,?,?)""",
            (candidate_id,manifest["customer_id"],json.dumps(manifest,sort_keys=True),expected,actor,now))
        return None


def reverse(candidate_id, stamp, owner):
    if not storage_ready():
        raise ValueError("Persistent storage required.")
    with closing(connect()) as db, db:
        _init(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM hq_historical_postcode_imports WHERE candidate_id=?",
                         (candidate_id,)).fetchone()
        if not row or not hmac.compare_digest(row["stamp"],stamp):
            raise ValueError("Import changed.")
        if row["reversed_at"]:
            return
        _active_link(db,candidate_id)
        manifest = json.loads(row["manifest"])
        current = postcode_status(db,row["customer_id"])
        if (_helpers()._helpers()._native_row(db,row["customer_id"]) is None
                or current["revision"] != 1 or current["postcode"] != manifest["postcode"]):
            raise ValueError("Native postcode or customer changed; automatic rollback is unsafe.")
        now = datetime.now(timezone.utc).isoformat()
        actor = _actor_fingerprint(owner)
        _write_postcode(db,row["customer_id"],2,current["postcode"],"",actor,now)
        db.execute("""UPDATE hq_historical_postcode_imports SET reversed_at=?,reversed_by=?
            WHERE candidate_id=?""",(now,actor,candidate_id))


def register_routes(bp):
    @bp.route("/hq/migration/customers/<candidate_id>/postcode-import",methods=["GET","POST"])
    @required
    def historical_postcode_import(candidate_id):
        links = _helpers()
        if not links.HEX.fullmatch(candidate_id):
            abort(404)
        error = notice = manifest = stamp = record = None
        snapshots = []
        status = 200
        try:
            if not storage_ready():
                raise ValueError("Persistent storage unavailable.")
            if request.method == "POST":
                if request.content_length is None or request.content_length > 4096:
                    abort(413)
                if not secrets.compare_digest(request.form.get("csrf",""),session.get("csrf","not-set")):
                    abort(403)
                action = request.form.get("action")
                if action in ("apply","reverse") and (request.form.get("confirm") != "yes"
                        or not links._helpers().STAMP.fullmatch(request.form.get("stamp",""))):
                    raise ValueError("Confirm the exact prepared action.")
                if action == "reverse":
                    reverse(candidate_id,request.form["stamp"],session.get("google_sub"))
                    notice = "Postcode import reversed; history retained."
                elif action in ("prepare","apply"):
                    provider = current_app.extensions["hq_sheets_connection"]()
                    if not provider or provider[1] <= time.time():
                        raise ValueError("Connect read-only Sheets access first.")
                    result = review(candidate_id,request.form.get("snapshot_id",""),
                                    links._helpers()._read_live(provider[0]),session.get("google_sub"),
                                    request.form.get("stamp") if action == "apply" else None)
                    if result:
                        manifest,stamp = result
                    else:
                        notice = "Historical postcode saved to native customer record."
                else:
                    abort(400)
            with closing(connect()) as db, db:
                _init(db)
                row = db.execute("""SELECT candidate_id,customer_id,stamp,applied_at,reversed_at
                    FROM hq_historical_postcode_imports WHERE candidate_id=?""",(candidate_id,)).fetchone()
                record = dict(row) if row else None
            try:
                _, directory = links._paths()
                snapshots = links._existing(directory)
            except LookupError:
                pass
        except (ValueError,LookupError) as exc:
            error,status = str(exc),409
        except (sqlite3.Error,OSError,RuntimeError):
            error,status = "Storage, source or recovery unavailable; no new approval established.",503
        return render_template("hq_historical_postcode_import.html",candidate_id=candidate_id,
            record=record,manifest=manifest,stamp=stamp,error=error,notice=notice,
            snapshots=snapshots,csrf=session.get("csrf","")),status
