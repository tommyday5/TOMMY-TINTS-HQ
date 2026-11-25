"""Bounded owner-approved historical rows -> native editable review drafts.

Non-quote status and price are provenance only until independently reconciled.
No external calls except existing read-only source.
"""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hmac
import json
import secrets
import sqlite3
import time
import uuid

from flask import abort, current_app, render_template, request, session
from customer_request_store import connect, storage_ready, SERVICES
from hq_auth import required
from hq_core_records import _schema, _audit_work
from hq_native_work import _tables as native_tables
from hq_historical_preview import _review_tables, _candidate_stamp, _verified_source_candidate, _check_source, _ranges, _cell
from hq_historical_vehicle_links import _init as vehicle_tables, _identity, source_helpers
from hq_private_recovery import _paths, _existing, _restore_check, MAX_SECONDS
from hq_work_progress import _actor_fingerprint
from hq_job_sync import schema as sync_tables
from hq_historical_customer_creation import _quoted


def _tables(db):
    _schema(db)
    native_tables(db)
    vehicle_tables(db)
    _review_tables(db)
    sync_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_created_work(
        id TEXT PRIMARY KEY, work_id TEXT NOT NULL UNIQUE REFERENCES hq_work_records(id),
        candidate_id TEXT NOT NULL UNIQUE, vehicle_link_id TEXT NOT NULL,
        job_ref TEXT NOT NULL UNIQUE COLLATE NOCASE, sheet_row INTEGER NOT NULL UNIQUE,
        stamp TEXT NOT NULL UNIQUE, manifest TEXT NOT NULL, native_stamp TEXT NOT NULL,
        actor TEXT NOT NULL, created_at TEXT NOT NULL, reversed_at TEXT, reversed_by TEXT)""")


def _link(db, link_id):
    link = db.execute("SELECT * FROM hq_historical_vehicle_links WHERE id=? AND reversed_at IS NULL",
                      (link_id,)).fetchone()
    if not link:
        raise ValueError("Choose an active verified historical vehicle link.")
    m = json.loads(link["manifest"])
    identity = _identity(db, link["customer_id"], link["vehicle_id"], m["customer_row"])
    if source_helpers()._stamp(identity) != m["native_stamp"]:
        raise ValueError("Customer or vehicle changed since the reviewed link.")
    return dict(link), identity


def _directory(db):
    # Bind current canonical work, existing proposals and sync references.
    return [[list(r) for r in db.execute(q)] for q in (
        "SELECT * FROM hq_work_records ORDER BY id",
        "SELECT * FROM hq_historical_j_candidates ORDER BY id",
        "SELECT * FROM hq_job_sheet_links ORDER BY work_id",
        "SELECT * FROM hq_historical_created_work ORDER BY id",
        "SELECT * FROM hq_historical_vehicle_links ORDER BY id",
        "SELECT * FROM hq_work_archive_state ORDER BY work_id")]


def _price(value, computed):
    if value in ("", None) and computed in ("", None):
        return None
    if type(value) not in (int, float) or value != computed or type(computed) not in (int, float):
        raise ValueError("Quoted price must be literal numeric or genuinely missing.")
    try:
        p = Decimal(str(value))*100
        if not p.is_finite() or p != p.to_integral_value() or not 0 <= p <= 100000000:
            raise ValueError("Quoted price must be an exact supported GBP amount.")
        return int(p)
    except InvalidOperation as exc:
        raise ValueError("Invalid quoted price.") from exc


def _manifest(db, source, link_id, snapshot_id, operation, service):
    h = source_helpers()
    link, identity = _link(db, link_id)
    m = json.loads(link["manifest"])
    row, ref = link["sheet_row"], link["job_ref"]
    stamp = _candidate_stamp(*source, row)
    _verified_source_candidate(*source, row, ref, stamp)
    if stamp != m["source_stamp"] or h.source_stamp(*source, m["customer_row"]) != m["customer_source_stamp"]:
        raise ValueError("Source changed since vehicle review; re-establish provenance.")
    jobs, _ = _check_source(*source)
    effective, _ = _ranges(source[1])
    raw, actual = jobs[row-4], effective[row-4]
    source_status = _cell(raw,17)
    if (source_status not in ("Quoted", "Booked", "Completed", "Paid", "Awaiting Payment")
            or source_status != _cell(actual,17)):
        raise ValueError("Choose an unchanged literal Quoted, Booked, Completed, Paid or Awaiting Payment row.")
    tier = {"Retail":"retail","Trader":"trade","Trade":"trade","Maintenance":"maintenance"}.get(_cell(raw,3))
    if not tier or _cell(raw,3) != _cell(actual,3):
        raise ValueError("Historical customer type is unknown or changed.")
    descriptions = []
    for i in range(5,10):
        value = _cell(raw,i)
        if value != _cell(actual,i) or not isinstance(value,str) or value.startswith("=") or len(value)>200 or any(ord(c)<32 for c in value):
            raise ValueError("Service descriptions need unchanged literal values.")
        if value.strip():
            descriptions.append(value.strip())
    if not descriptions or service not in SERVICES:
        raise ValueError("Review source services and select a native category.")
    price = _price(_cell(raw,16), _cell(actual,16))
    if db.execute("SELECT 1 FROM hq_historical_created_work WHERE job_ref=? COLLATE NOCASE OR sheet_row=?",
                  (ref,row)).fetchone() or db.execute("SELECT 1 FROM hq_historical_j_candidates WHERE job_ref=? COLLATE NOCASE OR sheet_row=?",
                  (ref,row)).fetchone() or db.execute("SELECT 1 FROM hq_job_sheet_links WHERE job_ref=? COLLATE NOCASE",
                  (ref,)).fetchone():
        raise ValueError("Original reference already has mapping history; review existing work.")
    # Repeat work on a vehicle is normal. Permit only separately identified,
    # unchanged imported drafts, not unmapped native work or ambiguous history.
    # Bind their references to this exact manifest and recheck fresh source.
    prior_refs = []
    for native in db.execute("SELECT * FROM hq_work_records WHERE vehicle_id=? ORDER BY id",
                             (link["vehicle_id"],)):
        prior = db.execute("SELECT * FROM hq_historical_created_work WHERE work_id=? AND reversed_at IS NULL",
                           (native["id"],)).fetchone()
        if (not prior or native["customer_id"] != link["customer_id"]
                or h._stamp(dict(native)) != prior["native_stamp"]
                or db.execute("SELECT 1 FROM hq_work_archive_state WHERE work_id=? AND is_archived=1",
                              (native["id"],)).fetchone()):
            raise ValueError("This vehicle has unmapped or changed native work; independently review possible duplicates.")
        candidate = db.execute("SELECT * FROM hq_historical_j_candidates WHERE id=? AND withdrawn_at IS NULL",
                               (prior["candidate_id"],)).fetchone()
        previous_link, _ = _link(db, prior["vehicle_link_id"])
        previous_manifest = json.loads(prior["manifest"])
        if (not candidate or candidate["work_id"] != native["id"]
                or candidate["job_ref"] != prior["job_ref"] or candidate["sheet_row"] != prior["sheet_row"]
                or previous_link["vehicle_id"] != link["vehicle_id"]
                or previous_link["customer_id"] != link["customer_id"]
                or previous_link["job_ref"] != prior["job_ref"]
                or previous_link["sheet_row"] != prior["sheet_row"]
                or candidate["source_stamp"] != previous_manifest["source_stamp"]):
            raise ValueError("Existing historical work provenance is inconsistent; review possible duplicates.")
        _verified_source_candidate(*source, prior["sheet_row"], prior["job_ref"], previous_manifest["source_stamp"])
        prior_refs.append(prior["job_ref"])
    prior_refs.sort()
    directory = h._stamp(_directory(db))
    _, folder = _paths()
    if not any(s["id"] == snapshot_id for s in _existing(folder)):
        raise ValueError("Recovery snapshot missing.")
    path = folder/(snapshot_id+".sqlite3")
    _restore_check(path,time.monotonic()+MAX_SECONDS)
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=1)) as saved:
        saved.row_factory=sqlite3.Row
        saved.execute("PRAGMA query_only=ON")
        # Older snapshots without optional empty review/sync tables cannot
        # establish directory parity. The prepare page initializes these first.
        if _link(saved,link_id) != (link,identity) or h._stamp(_directory(saved)) != directory:
            raise ValueError("Snapshot predates the reviewed vehicle link or work directory.")
    wid = h._stamp(["historical-native-work",operation])[:32]
    cid = h._stamp(["historical-native-candidate",operation])[:32]
    if db.execute("SELECT 1 FROM hq_work_records WHERE id=? OR source_request_id=?",(wid,"HQ-"+wid)).fetchone():
        raise ValueError("Native ID collision.")
    return dict(version=1,operation=operation,link_id=link_id,snapshot_id=snapshot_id,
                sheet_row=row,job_ref=ref,source_stamp=stamp,directory_stamp=directory,
                link_stamp=h._stamp(link),customer_id=link["customer_id"],vehicle_id=link["vehicle_id"],
                work_id=wid,candidate_id=cid,service=service,tier=tier,price_pence=price,
                source_services=descriptions,source_status=source_status,prior_vehicle_refs=prior_refs)


def review(source, link_id, snapshot_id, operation, service, owner, approval=None):
    h=source_helpers()
    if not storage_ready() or not all(h.HEX.fullmatch(x) for x in (link_id,snapshot_id,operation)):
        raise ValueError("Valid references and persistent storage are required.")
    actor=_actor_fingerprint(owner)
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior=db.execute("SELECT * FROM hq_historical_created_work WHERE id=?",(operation,)).fetchone()
        if prior:
            m=json.loads(prior["manifest"])
            if (approval and hmac.compare_digest(approval,prior["stamp"]) and prior["actor"]==actor
                    and not prior["reversed_at"] and (link_id,snapshot_id,service)==
                    tuple(m[k] for k in ("link_id","snapshot_id","service"))):
                return None
            raise ValueError("Submission already used.")
        m=_manifest(db,source,link_id,snapshot_id,operation,service)
        expected=h._stamp(["historical-native-quote-v1",m,actor])
        if approval is None:
            return m,expected
        if not hmac.compare_digest(expected,approval):
            raise ValueError("Source, mapping or work directory changed; prepare a fresh approval.")
        now=datetime.now(timezone.utc).isoformat()
        wid,cid=m["work_id"],m["candidate_id"]
        quote = m["source_status"] == "Quoted"
        notes="Historical "+m["job_ref"]+" — original service descriptions: "+"; ".join(m["source_services"])
        db.execute("""INSERT INTO hq_work_records
            (id,source_request_id,customer_id,vehicle_id,kind,service,notes,preferred_date,preferred_time,status,price_pence,created_at)
            VALUES(?,?,?,?,?,?,?,'','','draft',?,?)""",
            (wid,"HQ-"+wid,m["customer_id"],m["vehicle_id"],"quote" if quote else "job",
             service,notes,m["price_pence"] if quote else None,now))
        db.execute("INSERT INTO hq_native_work_details VALUES(?,?,?)",(wid,m["tier"],now))
        db.execute("INSERT INTO hq_native_work_operations VALUES(?,?,?,?)",(operation,wid,expected,now))
        db.execute("INSERT INTO hq_native_work_events VALUES(?,?,?,?,?)",(uuid.uuid4().hex,wid,"created",0,now))
        db.execute("""INSERT INTO hq_historical_j_candidates
            (id,operation_id,work_id,work_revision,workbook_id,sheet_id,sheet_row,job_ref,source_stamp,actor_fingerprint,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (cid,cid,wid,0,h.WORKBOOK,330634230,m["sheet_row"],m["job_ref"],m["source_stamp"],actor,now))
        db.execute("INSERT INTO hq_historical_j_candidate_events VALUES(?,?,?,?,?,?)",
                   (uuid.uuid4().hex,cid,0,"proposed",actor,now))
        native=dict(db.execute("SELECT * FROM hq_work_records WHERE id=?",(wid,)).fetchone())
        audit={k:v for k,v in m.items() if k!="source_services"}
        db.execute("INSERT INTO hq_historical_created_work VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                   (operation,wid,cid,link_id,m["job_ref"],m["sheet_row"],expected,
                    json.dumps(audit,sort_keys=True),h._stamp(native),actor,now))
        return None


def reverse(operation, stamp, owner):
    h=source_helpers()
    if not storage_ready() or not h.HEX.fullmatch(operation):
        raise ValueError("Invalid creation reference.")
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        record=db.execute("SELECT * FROM hq_historical_created_work WHERE id=?",(operation,)).fetchone()
        if not record or not hmac.compare_digest(stamp,record["stamp"]):
            raise ValueError("Approval changed.")
        if record["reversed_at"]:
            return
        wid,cid=record["work_id"],record["candidate_id"]
        native=db.execute("SELECT * FROM hq_work_records WHERE id=?",(wid,)).fetchone()
        candidate=db.execute("SELECT * FROM hq_historical_j_candidates WHERE id=?",(cid,)).fetchone()
        if (not native or h._stamp(dict(native))!=record["native_stamp"] or not candidate
                or candidate["revision"]!=0 or candidate["withdrawn_at"]):
            raise ValueError("Draft or proposal changed; resolve manually.")
        # Fail closed for any other work/candidate dependency, including audits.
        ignored={"hq_work_records","hq_native_work_details","hq_native_work_operations",
                 "hq_native_work_events","hq_historical_created_work","hq_historical_j_candidates",
                 "hq_historical_j_candidate_events"}
        for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if table in ignored:
                continue
            cols={c["name"] for c in db.execute("PRAGMA table_info("+_quoted(table)+")")}
            for col,value in (("work_id",wid),("candidate_id",cid)):
                refs={c for c in cols if c.lower().endswith(col)}
                refs.update(fk["from"] for fk in db.execute("PRAGMA foreign_key_list("+_quoted(table)+")")
                            if fk["table"]==("hq_work_records" if col=="work_id" else "hq_historical_j_candidates"))
                for key in refs:
                    if db.execute("SELECT 1 FROM "+_quoted(table)+" WHERE "+_quoted(key)+"=? LIMIT 1",(value,)).fetchone():
                        raise ValueError("Draft has dependent history; resolve manually.")
        now=datetime.now(timezone.utc).isoformat()
        actor=_actor_fingerprint(owner)
        db.execute("UPDATE hq_work_records SET revision=1 WHERE id=?",(wid,))
        db.execute("INSERT INTO hq_work_archive_state VALUES(?,1,?,NULL)",(wid,now))
        _audit_work(db,wid,1,"archive",{"archived":False},{"archived":True})
        db.execute("UPDATE hq_historical_j_candidates SET revision=1,withdrawn_at=? WHERE id=?",(now,cid))
        db.execute("INSERT INTO hq_historical_j_candidate_events VALUES(?,?,?,?,?,?)",
                   (uuid.uuid4().hex,cid,1,"withdrawn",actor,now))
        db.execute("UPDATE hq_historical_created_work SET reversed_at=?,reversed_by=? WHERE id=?",(now,actor,operation))


def register_routes(bp):
    @bp.route("/hq/migration/customers/create-work",methods=["GET","POST"])
    @required
    def historical_work_creation():
        h=source_helpers()
        manifest=stamp=notice=error=None
        links=history=snapshots=[]
        status=200
        try:
            if not storage_ready():
                raise ValueError("Persistent storage unavailable.")
            with closing(connect()) as db, db:
                _tables(db)
                links=[dict(r) for r in db.execute("SELECT * FROM hq_historical_vehicle_links WHERE reversed_at IS NULL ORDER BY job_ref LIMIT 250")]
            if request.method=="POST":
                if request.content_length is None or request.content_length>4096:
                    abort(413)
                if not secrets.compare_digest(request.form.get("csrf",""),session.get("csrf","not-set")):
                    abort(403)
                action=request.form.get("action")
                operation=request.form.get("operation","")
                stamp=request.form.get("stamp","")
                if action in ("apply","reverse") and (request.form.get("confirm")!="yes" or not h.STAMP.fullmatch(stamp)):
                    raise ValueError("Explicitly approve the exact operation.")
                if action=="reverse":
                    reverse(operation,stamp,session.get("google_sub"))
                    notice="Untouched draft archived; proposal withdrawn and provenance retained."
                elif action in ("prepare","apply"):
                    provider=current_app.extensions["hq_sheets_connection"]()
                    if not provider or provider[1]<=time.time():
                        raise ValueError("Connect read-only Sheets access first.")
                    result=review(h._read_live(provider[0]),request.form.get("link_id",""),
                                  request.form.get("snapshot_id",""),operation,request.form.get("service",""),
                                  session.get("google_sub"),stamp if action=="apply" else None)
                    if result:
                        manifest,stamp=result
                    else:
                        notice="Historical review draft saved; no booking, invoice or payment created."
                else:
                    abort(400)
            with closing(connect()) as db:
                history=[dict(r) for r in db.execute("SELECT * FROM hq_historical_created_work ORDER BY created_at DESC LIMIT 250")]
            _,folder=_paths()
            snapshots=_existing(folder)
        except (ValueError,LookupError) as exc:
            error,status=str(exc),409
        except (sqlite3.Error,OSError,RuntimeError):
            error,status="Storage or source unavailable; open this page before creating a fresh recovery snapshot.",503
        return render_template("hq_historical_work_creation.html",manifest=manifest,stamp=stamp,
            notice=notice,error=error,history=history,links=links,snapshots=snapshots,
            services=sorted(SERVICES),operation=uuid.uuid4().hex,csrf=session.get("csrf","")),status
