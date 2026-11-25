"""Explicit historical J-row to existing canonical vehicle provenance.

The Jobs Vehicle/Reg column is mixed free text. Never infer make, model or
registration, create a vehicle, change work, or grant customer portal access.
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
from hq_historical_preview import (
    _check_source, _ranges, _cell, _norm, J_REF, _candidate_stamp, _verified_source_candidate,
)
from hq_historical_customer_creation import _init as creation_init
from hq_private_recovery import _paths, _existing, _restore_check, MAX_SECONDS
from hq_work_progress import _actor_fingerprint


def source_helpers():
    import hq_historical_customer_links as h
    return h


def _init(db):
    creation_init(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_vehicle_links(
        id TEXT PRIMARY KEY, job_ref TEXT NOT NULL, sheet_row INTEGER NOT NULL,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        vehicle_id TEXT NOT NULL REFERENCES hq_vehicles(id),
        manifest TEXT NOT NULL, stamp TEXT NOT NULL UNIQUE,
        actor TEXT NOT NULL, created_at TEXT NOT NULL,
        reversed_at TEXT, reversed_by TEXT)""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_historical_vehicle_active_ref
        ON hq_historical_vehicle_links(job_ref) WHERE reversed_at IS NULL""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_historical_vehicle_active_row
        ON hq_historical_vehicle_links(sheet_row) WHERE reversed_at IS NULL""")


def _customer_identity(db, customer_id, customer_row):
    h = source_helpers()
    native = h._native_row(db, customer_id)
    if not native:
        raise ValueError("Choose an active canonical customer.")
    links = []
    for table, query in (
        ("hq_approved_customer_source_links",
         "SELECT * FROM hq_approved_customer_source_links WHERE customer_id=? AND workbook_id=? AND sheet_id=? AND sheet_row=? AND reversed_at IS NULL"),
        ("hq_historical_created_customers",
         "SELECT c.* FROM hq_historical_created_customers c JOIN hq_historical_customer_creation_batches b ON b.id=c.batch_id WHERE c.customer_id=? AND c.workbook_id=? AND c.sheet_id=? AND c.sheet_row=? AND b.reversed_at IS NULL"),
    ):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            links.extend((table, dict(row)) for row in db.execute(
                query, (customer_id, h.WORKBOOK, h.CUSTOMERS_SHEET_ID, customer_row)))
    if len(links) != 1:
        raise ValueError("First establish exactly one active historical customer provenance link.")
    return dict(native), links[0]


def _identity(db, customer_id, vehicle_id, customer_row):
    native, parent = _customer_identity(db, customer_id, customer_row)
    vehicle = db.execute("SELECT * FROM hq_vehicles WHERE id=? AND customer_id=?",
                         (vehicle_id, customer_id)).fetchone()
    if not vehicle:
        raise ValueError("Choose a vehicle belonging to this canonical customer.")
    revision = 0
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_vehicle_edit_audit'").fetchone():
        revision = db.execute("SELECT COALESCE(MAX(id),0) FROM hq_vehicle_edit_audit WHERE vehicle_id=?",
                              (vehicle_id,)).fetchone()[0]
    return native, dict(vehicle), parent, revision


def _snapshot(snapshot_id, identity, customer_id, vehicle_id, customer_row):
    if not source_helpers().HEX.fullmatch(snapshot_id):
        raise ValueError("Select a private recovery snapshot.")
    _, directory = _paths()
    if not any(s["id"] == snapshot_id for s in _existing(directory)):
        raise ValueError("Recovery snapshot missing.")
    path = directory / (snapshot_id + ".sqlite3")
    _restore_check(path, time.monotonic() + MAX_SECONDS)
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=1)) as saved:
        saved.row_factory = sqlite3.Row
        saved.execute("PRAGMA query_only=ON")
        if _identity(saved, customer_id, vehicle_id, customer_row) != identity:
            raise ValueError("Snapshot predates current customer provenance or vehicle.")


def _manifest(db, source, row, ref, customer_row, customer_id, vehicle_id, snapshot_id, operation):
    h = source_helpers()
    stamp = _candidate_stamp(*source, row)
    _verified_source_candidate(*source, row, ref, stamp)
    jobs, _ = _check_source(*source)
    effective, _ = _ranges(source[1])
    raw = jobs[row-4]
    val = _cell(raw, 4)
    name = _cell(raw, 2)
    if (not isinstance(val, str) or not val.strip() or val.startswith("=")
            or len(val) > 160 or any(ord(c) < 32 for c in val)
            or val != _cell(effective[row-4], 4)
            or not isinstance(name, str) or not _norm(name) or name.startswith("=")
            or name != _cell(effective[row-4], 2)):
        raise ValueError("Vehicle/Reg and customer must be unchanged literal source fields.")
    customer_source, computed = h._source_row(*source, customer_row)
    if _norm(customer_source[0]) != _norm(name) or customer_source[0] != computed[0]:
        raise ValueError("Jobs customer and Customers source identity do not agree.")
    identity = _identity(db, customer_id, vehicle_id, customer_row)
    native, vehicle, parent, revision = identity
    if _norm(native["name"]) != _norm(name):
        raise ValueError("Native customer name differs; resolve identity first.")
    parent_source_stamp = (json.loads(parent[1]["manifest"])["source_stamp"]
                           if parent[0] == "hq_approved_customer_source_links" else parent[1]["source_stamp"])
    if parent_source_stamp != h.source_stamp(*source, customer_row):
        raise ValueError("Historical customer source changed; review provenance before linking.")
    # A literal plate-only cell can be checked exactly. Mixed descriptions
    # require explicit documentary identity review, never automated parsing.
    import re
    norm_plate = lambda value: re.sub(r"[ -]", "", value).upper()
    plate_only = bool(re.fullmatch(r"[A-Za-z]{2}[0-9]{2} ?[A-Za-z]{3}", val.strip()))
    if plate_only and norm_plate(val) != norm_plate(vehicle["registration"]):
        raise ValueError("Literal source registration does not match the selected vehicle.")
    if db.execute("SELECT 1 FROM hq_historical_vehicle_links WHERE reversed_at IS NULL AND (job_ref=? OR sheet_row=?)",
                  (ref,row)).fetchone():
        raise ValueError("This historical job already has an active vehicle link.")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_historical_created_vehicles'").fetchone():
        created=db.execute("SELECT vehicle_id,reversed_at FROM hq_historical_created_vehicles WHERE job_ref=? OR sheet_row=?",(ref,row)).fetchone()
        if created and (created["reversed_at"] or created["vehicle_id"]!=vehicle_id):
            raise ValueError("Historical creation provenance conflicts with this vehicle link.")
    _snapshot(snapshot_id, identity, customer_id, vehicle_id, customer_row)
    return dict(version=1, operation=operation, job_ref=ref, sheet_row=row,
                workbook_id=h.WORKBOOK, sheet_id=330634230,
                customer_row=customer_row, customer_id=customer_id, vehicle_id=vehicle_id,
                source_stamp=stamp, customer_source_stamp=parent_source_stamp,
                parent_stamp=h._stamp(parent), native_stamp=h._stamp(identity),
                vehicle_revision=revision, snapshot_id=snapshot_id, plate_only=plate_only,
                source_vehicle=val, native_vehicle=" ".join(
                    vehicle[k] for k in ("make","model","registration") if vehicle[k]),
                customer_name=native["name"])


def review(source, row, ref, customer_row, customer_id, vehicle_id, snapshot_id,
           operation, owner, approval=None):
    h = source_helpers()
    if (not storage_ready() or not all(h.HEX.fullmatch(x) for x in
            (customer_id, vehicle_id, snapshot_id, operation)) or not J_REF.fullmatch(ref)):
        raise ValueError("Valid references and persistent storage are required.")
    actor = _actor_fingerprint(owner)
    with closing(connect()) as db, db:
        _init(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT * FROM hq_historical_vehicle_links WHERE id=?", (operation,)).fetchone()
        if prior:
            saved = json.loads(prior["manifest"])
            if (approval and hmac.compare_digest(approval, prior["stamp"])
                    and prior["actor"] == actor and not prior["reversed_at"]
                    and (row,ref,customer_row,customer_id,vehicle_id,snapshot_id) ==
                    tuple(saved[k] for k in ("sheet_row","job_ref","customer_row","customer_id","vehicle_id","snapshot_id"))):
                return None
            raise ValueError("Submission already used.")
        manifest = _manifest(db,source,row,ref,customer_row,customer_id,vehicle_id,snapshot_id,operation)
        expected = h._stamp(["historical-vehicle-link-v1",manifest,actor])
        if approval is None:
            return manifest, expected
        if not hmac.compare_digest(approval,expected):
            raise ValueError("Source or native vehicle changed; prepare a fresh approval.")
        audit = {k:v for k,v in manifest.items() if k not in
                 ("source_vehicle","native_vehicle","customer_name")}
        db.execute("INSERT INTO hq_historical_vehicle_links VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)",
                   (operation,ref,row,customer_id,vehicle_id,json.dumps(audit,sort_keys=True),
                    expected,actor,datetime.now(timezone.utc).isoformat()))
        return None


def reverse(operation, stamp, owner):
    h = source_helpers()
    if not storage_ready() or not h.HEX.fullmatch(operation):
        raise ValueError("Invalid link.")
    with closing(connect()) as db, db:
        _init(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM hq_historical_vehicle_links WHERE id=?", (operation,)).fetchone()
        if not row or not hmac.compare_digest(stamp,row["stamp"]):
            raise ValueError("Link changed.")
        if not row["reversed_at"]:
            db.execute("UPDATE hq_historical_vehicle_links SET reversed_at=?,reversed_by=? WHERE id=?",
                       (datetime.now(timezone.utc).isoformat(),_actor_fingerprint(owner),operation))



def _creation_tables(db):
    _init(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_created_vehicles(
        id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL UNIQUE, customer_id TEXT NOT NULL,
        job_ref TEXT NOT NULL, sheet_row INTEGER NOT NULL, stamp TEXT NOT NULL UNIQUE,
        manifest TEXT NOT NULL, native_stamp TEXT NOT NULL, actor TEXT NOT NULL,
        created_at TEXT NOT NULL, reversed_at TEXT, reversed_by TEXT)""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_hist_created_vehicle_row
        ON hq_historical_created_vehicles(sheet_row)""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_hist_created_vehicle_ref
        ON hq_historical_created_vehicles(job_ref)""")


def _creation_state(db, customer_id, customer_row):
    return (_customer_identity(db, customer_id, customer_row),
            [dict(v) for v in db.execute("SELECT * FROM hq_vehicles ORDER BY id")])


def _creation_manifest(db, source, row, ref, customer_row, customer_id,
                       snapshot_id, operation, fields):
    h = source_helpers()
    stamp = _candidate_stamp(*source, row)
    _verified_source_candidate(*source, row, ref, stamp)
    jobs, _ = _check_source(*source)
    effective, _ = _ranges(source[1])
    raw = jobs[row-4]
    val, name = _cell(raw,4), _cell(raw,2)
    if (not isinstance(val,str) or not val.strip() or val.startswith("=")
            or len(val)>160 or any(ord(c)<32 for c in val)
            or val!=_cell(effective[row-4],4) or name!=_cell(effective[row-4],2)
            or not isinstance(name,str) or name.startswith("=")):
        raise ValueError("Historical vehicle and customer need unchanged literal source fields.")
    customer_source, computed = h._source_row(*source,customer_row)
    if _norm(customer_source[0])!=_norm(name) or customer_source[0]!=computed[0]:
        raise ValueError("Jobs customer and Customers source do not agree.")
    state = _creation_state(db,customer_id,customer_row)
    native,parent = state[0]
    parent_stamp = (json.loads(parent[1]["manifest"])["source_stamp"]
                    if parent[0]=="hq_approved_customer_source_links" else parent[1]["source_stamp"])
    if _norm(native["name"])!=_norm(name) or parent_stamp!=h.source_stamp(*source,customer_row):
        raise ValueError("Customer identity or provenance changed.")
    import re
    norm_plate = lambda v: re.sub(r"[ -]","",v).upper()
    plate_only = bool(re.fullmatch(r"[A-Za-z]{2}[0-9]{2} ?[A-Za-z]{3}",val.strip()))
    if plate_only and norm_plate(val)!=norm_plate(fields["registration"]):
        raise ValueError("Literal registration must match the reviewed registration.")
    for vehicle in state[1]:
        if fields["registration"] and norm_plate(vehicle["registration"])==norm_plate(fields["registration"]):
            raise ValueError("Registration already exists; review and link that vehicle instead.")
        if (vehicle["customer_id"]==customer_id and
                _norm(vehicle["make"])==_norm(fields["make"]) and
                _norm(vehicle["model"])==_norm(fields["model"]) and
                (not fields["registration"] or not vehicle["registration"])):
            raise ValueError("Possible existing vehicle; review before creating another.")
    if db.execute("SELECT 1 FROM hq_historical_created_vehicles WHERE job_ref=? OR sheet_row=?",(ref,row)).fetchone():
        raise ValueError("Source already has vehicle creation history.")
    if db.execute("SELECT 1 FROM hq_historical_vehicle_links WHERE reversed_at IS NULL AND (job_ref=? OR sheet_row=?)",(ref,row)).fetchone():
        raise ValueError("Source already has an active vehicle link.")
    _, directory = _paths()
    if not any(s["id"]==snapshot_id for s in _existing(directory)):
        raise ValueError("Recovery snapshot missing.")
    path = directory / (snapshot_id+".sqlite3")
    _restore_check(path,time.monotonic()+MAX_SECONDS)
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=1)) as saved:
        saved.row_factory=sqlite3.Row
        saved.execute("PRAGMA query_only=ON")
        if _creation_state(saved,customer_id,customer_row)!=state:
            raise ValueError("Snapshot predates the current customer or vehicle directory.")
    vehicle_id=h._stamp(["historical-vehicle-create",operation])[:32]
    if db.execute("SELECT 1 FROM hq_vehicles WHERE id=?",(vehicle_id,)).fetchone():
        raise ValueError("Vehicle ID collision.")
    return dict(version=1,operation=operation,job_ref=ref,sheet_row=row,customer_row=customer_row,
                workbook_id=h.WORKBOOK,sheet_id=330634230,customer_id=customer_id,vehicle_id=vehicle_id,
                snapshot_id=snapshot_id,source_stamp=stamp,directory_stamp=h._stamp(state),
                customer_name=native["name"],source_vehicle=val,fields=fields,plate_only=plate_only)


def create_review(source,row,ref,customer_row,customer_id,snapshot_id,operation,payload,owner,approval=None):
    from hq_native_work import _vehicle_fields
    h=source_helpers()
    if not storage_ready() or not all(h.HEX.fullmatch(x) for x in (customer_id,snapshot_id,operation)) or not J_REF.fullmatch(ref):
        raise ValueError("Valid references and persistent storage are required.")
    fields=_vehicle_fields(payload)
    actor=_actor_fingerprint(owner)
    field_stamp=h._stamp(fields)
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        _creation_tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior=db.execute("SELECT * FROM hq_historical_created_vehicles WHERE id=?",(operation,)).fetchone()
        if prior:
            m=json.loads(prior["manifest"])
            if (approval and hmac.compare_digest(approval,prior["stamp"]) and prior["actor"]==actor
                    and not prior["reversed_at"] and
                    (row,ref,customer_row,customer_id,snapshot_id,field_stamp)==
                    tuple(m[k] for k in ("sheet_row","job_ref","customer_row","customer_id","snapshot_id","field_stamp"))):
                return None
            raise ValueError("Submission already used.")
        manifest=_creation_manifest(db,source,row,ref,customer_row,customer_id,snapshot_id,operation,fields)
        expected=h._stamp(["historical-vehicle-create-v1",manifest,actor])
        if approval is None:
            return manifest,expected
        if not hmac.compare_digest(expected,approval):
            raise ValueError("Source, reviewed fields or directory changed; prepare fresh approval.")
        now=datetime.now(timezone.utc).isoformat()
        vid=manifest["vehicle_id"]
        db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                   (vid,customer_id,fields["make"],fields["model"],fields["model_year"],fields["registration"],now))
        native=db.execute("SELECT * FROM hq_vehicles WHERE id=?",(vid,)).fetchone()
        audit={k:v for k,v in manifest.items() if k not in ("customer_name","source_vehicle","fields")}
        audit["field_stamp"]=field_stamp
        db.execute("INSERT INTO hq_historical_created_vehicles VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                   (operation,vid,customer_id,ref,row,expected,json.dumps(audit,sort_keys=True),
                    h._stamp(dict(native)),actor,now))
        return None


def reverse_creation(operation,stamp,owner):
    """Rollback only a newly created untouched identity; retain immutable provenance."""
    from hq_historical_customer_creation import _quoted
    h=source_helpers()
    if not storage_ready() or not h.HEX.fullmatch(operation):
        raise ValueError("Invalid creation reference.")
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        _creation_tables(db)
        db.execute("BEGIN IMMEDIATE")
        record=db.execute("SELECT * FROM hq_historical_created_vehicles WHERE id=?",(operation,)).fetchone()
        if not record or not hmac.compare_digest(stamp,record["stamp"]):
            raise ValueError("Approval changed.")
        if record["reversed_at"]:
            return
        vid=record["vehicle_id"]
        native=db.execute("SELECT * FROM hq_vehicles WHERE id=?",(vid,)).fetchone()
        if not native or h._stamp(dict(native))!=record["native_stamp"]:
            raise ValueError("Imported vehicle changed; resolve manually.")
        for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if table in ("hq_vehicles","hq_historical_created_vehicles"):
                continue
            cols={c["name"] for c in db.execute("PRAGMA table_info("+_quoted(table)+")")
                  if c["name"].lower().endswith("vehicle_id")}
            cols.update(fk["from"] for fk in db.execute("PRAGMA foreign_key_list("+_quoted(table)+")")
                        if fk["table"]=="hq_vehicles")
            for col in cols:
                if db.execute("SELECT 1 FROM "+_quoted(table)+" WHERE "+_quoted(col)+"=? LIMIT 1",(vid,)).fetchone():
                    raise ValueError("Vehicle has dependent records or history; resolve manually.")
        db.execute("DELETE FROM hq_vehicles WHERE id=?",(vid,))
        db.execute("UPDATE hq_historical_created_vehicles SET reversed_at=?,reversed_by=? WHERE id=?",
                   (datetime.now(timezone.utc).isoformat(),_actor_fingerprint(owner),operation))


def register_creation_route(bp):
    @bp.route("/hq/migration/customers/create-vehicle",methods=["GET","POST"])
    @required
    def historical_vehicle_creation():
        import re
        import uuid
        h=source_helpers()
        manifest=stamp=notice=error=None
        snapshots=history=[]
        status=200
        try:
            if not storage_ready():
                raise ValueError("Persistent storage unavailable.")
            if request.method=="POST":
                if request.content_length is None or request.content_length>4096:
                    abort(413)
                if not secrets.compare_digest(request.form.get("csrf",""),session.get("csrf","not-set")):
                    abort(403)
                action=request.form.get("action")
                operation=request.form.get("operation","")
                stamp=request.form.get("stamp","")
                if action in ("apply","reverse") and (request.form.get("confirm")!="yes" or not h.STAMP.fullmatch(stamp)):
                    raise ValueError("Explicitly confirm the exact reviewed operation.")
                if action=="reverse":
                    reverse_creation(operation,stamp,session.get("google_sub"))
                    notice="Untouched imported vehicle rolled back; provenance retained."
                elif action in ("prepare","apply"):
                    provider=current_app.extensions["hq_sheets_connection"]()
                    if not provider or provider[1]<=time.time():
                        raise ValueError("Connect read-only Sheets access first.")
                    row,customer_row=request.form.get("row",""),request.form.get("customer_row","")
                    if not all(re.fullmatch(r"[0-9]{1,4}",x) for x in (row,customer_row)):
                        raise ValueError("Enter exact source row numbers.")
                    result=create_review(h._read_live(provider[0]),int(row),request.form.get("ref",""),
                        int(customer_row),request.form.get("customer_id",""),request.form.get("snapshot_id",""),
                        operation,request.form,session.get("google_sub"),stamp if action=="apply" else None)
                    if result:
                        manifest,stamp=result
                    else:
                        notice="Reviewed vehicle saved to native HQ. No job or workbook changed."
                else:
                    abort(400)
            with closing(connect()) as db, db:
                _creation_tables(db)
                history=[dict(r) for r in db.execute("SELECT * FROM hq_historical_created_vehicles ORDER BY created_at DESC LIMIT 250")]
            try:
                _,directory=_paths()
                snapshots=_existing(directory)
            except LookupError:
                pass
        except (ValueError,LookupError) as exc:
            error,status=str(exc),409
        except (sqlite3.Error,OSError,RuntimeError):
            error,status="Storage or source unavailable; no new approval established.",503
        return render_template("hq_historical_vehicle_creation.html",manifest=manifest,stamp=stamp,
            notice=notice,error=error,history=history,snapshots=snapshots,
            operation=uuid.uuid4().hex,csrf=session.get("csrf","")),status


def register_routes(bp):
    register_creation_route(bp)
    @bp.route("/hq/migration/customers/vehicle-links", methods=["GET","POST"])
    @required
    def historical_vehicle_links():
        h = source_helpers()
        manifest = stamp = notice = error = None
        rows = snapshots = vehicles = []
        status = 200
        try:
            if request.method == "POST":
                if request.content_length is None or request.content_length > 4096:
                    abort(413)
                if not secrets.compare_digest(request.form.get("csrf",""),session.get("csrf","not-set")):
                    abort(403)
                action = request.form.get("action")
                operation = request.form.get("operation","")
                if action == "reverse":
                    if request.form.get("confirm") != "yes":
                        raise ValueError("Confirm reversal.")
                    reverse(operation,request.form.get("stamp",""),session.get("google_sub"))
                    notice = "Historical vehicle link reversed; history retained."
                elif action in ("prepare","apply"):
                    if action == "apply" and (request.form.get("confirm") != "yes"
                            or not h.STAMP.fullmatch(request.form.get("stamp",""))):
                        raise ValueError("Confirm exact independent vehicle review.")
                    provider = current_app.extensions["hq_sheets_connection"]()
                    if not provider or provider[1] <= time.time():
                        raise ValueError("Connect read-only Sheets access first.")
                    import re
                    row = request.form.get("row","")
                    customer_row = request.form.get("customer_row","")
                    if not all(re.fullmatch(r"[0-9]{1,4}", x) for x in (row,customer_row)):
                        raise ValueError("Enter exact source row numbers.")
                    result = review(h._read_live(provider[0]),int(row),request.form.get("ref",""),
                        int(customer_row),request.form.get("customer_id",""),request.form.get("vehicle_id",""),
                        request.form.get("snapshot_id",""),operation,session.get("google_sub"),
                        request.form.get("stamp") if action=="apply" else None)
                    if result:
                        manifest,stamp = result
                    else:
                        notice = "Historical vehicle link saved. No job, vehicle or Sheets fields changed."
                else:
                    abort(400)
            if storage_ready():
                with closing(connect()) as db, db:
                    _init(db)
                    rows = [dict(r) for r in db.execute("SELECT * FROM hq_historical_vehicle_links ORDER BY created_at DESC LIMIT 250")]
                    vehicles = [dict(r) for r in db.execute("""SELECT v.id,v.customer_id,v.make,v.model,v.registration,c.name
                        FROM hq_vehicles v JOIN hq_customers c ON c.id=v.customer_id
                        WHERE NOT EXISTS(SELECT 1 FROM hq_customer_archive_state a WHERE a.customer_id=c.id AND a.is_archived=1)
                        ORDER BY c.name,v.make,v.model LIMIT 250""")]
                try:
                    _, directory = _paths()
                    snapshots = _existing(directory)
                except LookupError:
                    pass
        except (ValueError,LookupError) as exc:
            error,status = str(exc),409
        except (sqlite3.Error,OSError,RuntimeError):
            error,status = "Storage or source unavailable; no new approval established.",503
        import uuid
        return render_template("hq_historical_vehicle_links.html",manifest=manifest,stamp=stamp,
            notice=notice,error=error,rows=rows,snapshots=snapshots,vehicles=vehicles,
            operation=uuid.uuid4().hex,csrf=session.get("csrf","")),status
