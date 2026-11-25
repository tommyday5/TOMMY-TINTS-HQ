"""Bounded owner-approved creation of missing historical customer identities.

Only literal names are imported; contact, pricing, entitlements, vehicles and
jobs remain unmapped. Reversal archives untouched identities, never deletes.
"""
from contextlib import closing
from datetime import datetime, timezone
import hmac
import json
import re
import secrets
import sqlite3
import time
import uuid

from flask import abort, current_app, render_template, request, session
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_customer_archive import _tables as archive_tables
from hq_private_recovery import _paths, _existing, _restore_check, MAX_SECONDS
from hq_work_progress import _actor_fingerprint


def helpers():
    import hq_historical_customer_links as source
    return source


def _init(db):
    from hq_approved_customer_links import _init as link_init
    link_init(db)
    archive_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_customer_creation_batches(
        id TEXT PRIMARY KEY, stamp TEXT NOT NULL UNIQUE, manifest TEXT NOT NULL,
        actor TEXT NOT NULL, created_at TEXT NOT NULL, reversed_at TEXT, reversed_by TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_created_customers(
        workbook_id TEXT NOT NULL, sheet_id INTEGER NOT NULL, sheet_row INTEGER NOT NULL,
        customer_id TEXT NOT NULL UNIQUE REFERENCES hq_customers(id),
        batch_id TEXT NOT NULL REFERENCES hq_historical_customer_creation_batches(id),
        source_stamp TEXT NOT NULL, native_stamp TEXT NOT NULL,
        PRIMARY KEY(workbook_id,sheet_id,sheet_row))""")


def _directory_stamp(db):
    return helpers()._stamp([list(r) for r in db.execute(
        "SELECT id,name,email,phone,created_at FROM hq_customers ORDER BY id")])


def _snapshot(snapshot_id, db):
    if not helpers().HEX.fullmatch(snapshot_id):
        raise ValueError("Select a private recovery snapshot.")
    _, directory = _paths()
    if not any(s["id"] == snapshot_id for s in _existing(directory)):
        raise ValueError("Recovery snapshot missing.")
    path = directory / (snapshot_id + ".sqlite3")
    _restore_check(path, time.monotonic() + MAX_SECONDS)
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=1)) as saved:
        saved.execute("PRAGMA query_only=ON")
        if not saved.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_customers'").fetchone():
            raise ValueError("Snapshot predates the canonical customer directory.")
        if _directory_stamp(saved) != _directory_stamp(db):
            raise ValueError("Recovery snapshot does not contain the current customer directory.")


def _rows(value):
    if not re.fullmatch(r"[0-9]{1,4}(?: *, *[0-9]{1,4}){0,4}", value):
        raise ValueError("Enter one to five distinct Customers sheet row numbers.")
    rows = sorted(int(x.strip()) for x in value.split(","))
    if len(set(rows)) != len(rows) or any(not 5 <= x <= 1000 for x in rows):
        raise ValueError("Rows must be distinct and between 5 and 1000.")
    return rows


def _manifest(db, rows, snapshot_id, batch_id, source):
    original = helpers()
    _snapshot(snapshot_id, db)
    native_names = {original._norm(r[0]) for r in db.execute("SELECT name FROM hq_customers")}
    entries = []
    for row in rows:
        raw, effective = original._source_row(*source, row)
        name = raw[0]
        if (not effective or name != effective[0] or name.startswith("=")
                or name != name.strip() or any(ord(c)<32 or ord(c)==127 for c in name)):
            raise ValueError("Row %d needs a clean literal, unchanged name." % row)
        if original._norm(name) in native_names:
            raise ValueError("Row %d collides with a native or archived name; use identity review." % row)
        key = (original.WORKBOOK, original.CUSTOMERS_SHEET_ID, row)
        for table, active in (("hq_historical_customer_candidates", " AND withdrawn_at IS NULL"),
                              ("hq_approved_customer_source_links", " AND reversed_at IS NULL"),
                              ("hq_historical_created_customers", "")):
            if db.execute("SELECT 1 FROM "+table+
                          " WHERE workbook_id=? AND sheet_id=? AND sheet_row=?"+active,key).fetchone():
                raise ValueError("Row %d already has migration history or an active proposal." % row)
        customer_id = original._stamp(["historical-created-customer",batch_id,row])[:32]
        if db.execute("SELECT 1 FROM hq_customers WHERE id=?",(customer_id,)).fetchone():
            raise ValueError("Native ID collision.")
        entries.append(dict(row=row, name=name, customer_id=customer_id,
                            source_stamp=original.source_stamp(*source,row)))
    return dict(version=1,batch_id=batch_id,snapshot_id=snapshot_id,
                workbook_id=original.WORKBOOK,sheet_id=original.CUSTOMERS_SHEET_ID,
                directory_stamp=_directory_stamp(db),entries=entries,
                customers_created=len(entries),fields_overwritten=0,
                vehicles_created=0,jobs_changed=0,unresolved_conflicts=0)


def review(rows, snapshot_id, batch_id, source, owner, stamp=None):
    original = helpers()
    if not storage_ready() or not original.HEX.fullmatch(batch_id):
        raise ValueError("Persistent storage and a valid batch reference are required.")
    actor = _actor_fingerprint(owner)
    with closing(connect()) as db, db:
        _init(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT * FROM hq_historical_customer_creation_batches WHERE id=?",
                           (batch_id,)).fetchone()
        if prior:
            saved = json.loads(prior["manifest"])
            if (stamp and hmac.compare_digest(prior["stamp"],stamp) and prior["actor"]==actor
                    and not prior["reversed_at"] and rows==[e["row"] for e in saved["entries"]]
                    and snapshot_id==saved["snapshot_id"]):
                return None
            raise ValueError("Batch reference already used.")
        manifest = _manifest(db,rows,snapshot_id,batch_id,source)
        expected = original._stamp(["historical-customer-creation-v1",manifest,actor])
        if stamp is None:
            return manifest,expected
        if not hmac.compare_digest(expected,stamp):
            raise ValueError("Source or directory changed; prepare a fresh approval.")
        now = datetime.now(timezone.utc).isoformat()
        # PII remains only in the canonical native customer; store keyed provenance.
        audit = dict(manifest,entries=[{k:v for k,v in e.items() if k!="name"}
                                     for e in manifest["entries"]])
        db.execute("INSERT INTO hq_historical_customer_creation_batches VALUES(?,?,?,?,?,NULL,NULL)",
                   (batch_id,expected,json.dumps(audit,sort_keys=True),actor,now))
        for entry in manifest["entries"]:
            cid=entry["customer_id"]
            db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                       (cid,None,entry["name"],"","",now))
            native = original._native_row(db,cid)
            db.execute("INSERT INTO hq_historical_created_customers VALUES(?,?,?,?,?,?,?)",
                       (manifest["workbook_id"],manifest["sheet_id"],entry["row"],cid,batch_id,
                        entry["source_stamp"],original._native_digest(native)))
        return None


def _quoted(value):
    return '"' + value.replace('"','""') + '"'


def _untouched(db, row):
    original=helpers()
    cid=row["customer_id"]
    native=original._native_row(db,cid)
    if not native or original._native_digest(native)!=row["native_stamp"]:
        raise ValueError("Imported identity has changed or is archived; review manually.")
    # Conservative: any later native relationship/history blocks automatic archival.
    for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if table in ("hq_customers","hq_historical_created_customers"):
            continue
        for col in db.execute("PRAGMA table_info("+_quoted(table)+")"):
            if col["name"].lower().endswith("customer_id") and db.execute(
                    "SELECT 1 FROM "+_quoted(table)+" WHERE "+_quoted(col["name"])+"=? LIMIT 1",
                    (cid,)).fetchone():
                raise ValueError("Imported customer has later linked data; review manually.")
        for fk in db.execute("PRAGMA foreign_key_list("+_quoted(table)+")"):
            if fk["table"]=="hq_customers" and db.execute(
                    "SELECT 1 FROM "+_quoted(table)+" WHERE "+_quoted(fk["from"])+"=? LIMIT 1",
                    (cid,)).fetchone():
                raise ValueError("Imported customer has later linked data; review manually.")


def reverse(batch_id, stamp, owner):
    if not storage_ready() or not helpers().HEX.fullmatch(batch_id):
        raise ValueError("Invalid batch.")
    with closing(connect()) as db, db:
        _init(db)
        db.execute("BEGIN IMMEDIATE")
        batch=db.execute("SELECT * FROM hq_historical_customer_creation_batches WHERE id=?",
                         (batch_id,)).fetchone()
        if not batch or not hmac.compare_digest(batch["stamp"],stamp):
            raise ValueError("Batch approval changed.")
        if batch["reversed_at"]:
            return
        rows=db.execute("SELECT * FROM hq_historical_created_customers WHERE batch_id=?",
                        (batch_id,)).fetchall()
        if not rows:
            raise ValueError("Batch provenance missing.")
        for row in rows:
            _untouched(db,row)
        actor=_actor_fingerprint(owner)
        now=datetime.now(timezone.utc).isoformat()
        for row in rows:
            cid=row["customer_id"]
            db.execute("INSERT INTO hq_customer_archive_state VALUES(?,1,1,?)",(cid,now))
            db.execute("INSERT INTO hq_customer_archive_events VALUES(?,?,1,0,1,?,?)",
                       (uuid.uuid4().hex,cid,actor,now))
        db.execute("UPDATE hq_historical_customer_creation_batches SET reversed_at=?,reversed_by=? WHERE id=?",
                   (now,actor,batch_id))


def register_routes(bp):
    @bp.route("/hq/migration/customers/create-batch", methods=["GET","POST"])
    @required
    def historical_customer_creation():
        original=helpers()
        manifest=stamp=error=notice=None
        snapshots=[]
        history=[]
        status=200
        batch_id=uuid.uuid4().hex
        try:
            if not storage_ready():
                raise ValueError("Persistent storage unavailable.")
            if request.method=="POST":
                if request.content_length is None or request.content_length>4096:
                    abort(413)
                if not secrets.compare_digest(request.form.get("csrf",""),session.get("csrf","not-set")):
                    abort(403)
                action=request.form.get("action")
                batch_id=request.form.get("batch_id","")
                if action in ("apply","reverse") and (request.form.get("confirm")!="yes"
                        or not original.STAMP.fullmatch(request.form.get("stamp",""))):
                    raise ValueError("Explicitly confirm the exact reviewed action.")
                if action=="reverse":
                    reverse(batch_id,request.form["stamp"],session.get("google_sub"))
                    notice="Untouched imported customers archived; original provenance retained."
                elif action in ("prepare","apply"):
                    provider=current_app.extensions["hq_sheets_connection"]()
                    if not provider or provider[1]<=time.time():
                        raise ValueError("Connect read-only Sheets access first.")
                    result=review(_rows(request.form.get("rows","")),request.form.get("snapshot_id",""),
                                  batch_id,original._read_live(provider[0]),session.get("google_sub"),
                                  request.form.get("stamp") if action=="apply" else None)
                    if result:
                        manifest,stamp=result
                    else:
                        notice="Approved native customer identities saved."
                else:
                    abort(400)
            with closing(connect()) as db, db:
                _init(db)
                history=[dict(r) for r in db.execute("""SELECT b.id,b.stamp,b.created_at,b.reversed_at,
                    count(c.customer_id) AS count FROM hq_historical_customer_creation_batches b
                    JOIN hq_historical_created_customers c ON c.batch_id=b.id
                    GROUP BY b.id ORDER BY b.created_at DESC LIMIT 50""")]
            try:
                _,directory=_paths()
                snapshots=_existing(directory)
            except LookupError:
                pass
        except (ValueError,LookupError) as exc:
            error,status=str(exc),409
        except (sqlite3.Error,OSError,RuntimeError):
            error,status="Storage, recovery or source unavailable; no new approval established.",503
        return render_template("hq_historical_customer_creation.html",manifest=manifest,stamp=stamp,
            error=error,notice=notice,history=history,snapshots=snapshots,
            batch_id=batch_id,csrf=session.get("csrf","")),status
