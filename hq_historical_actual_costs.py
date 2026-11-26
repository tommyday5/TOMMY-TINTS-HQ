"""Owner-approved historical cost observations, separate from live cost ledgers."""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hmac
import json
import re
import secrets
import sqlite3
import time
import uuid

from flask import abort, current_app, make_response, render_template, request, session
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_historical_preview import _check_source, _ranges, _cell, _verified_source_candidate
from hq_work_progress import _actor_fingerprint
from hq_historical_vehicle_links import source_helpers

FIELDS = (("hours", "Actual Hours"), ("materials", "Actual Materials £"),
          ("consumables", "Actual Consumables £"), ("extra", "Actual Extra Costs £"))
HEX = re.compile(r"[0-9a-f]{32}\Z")
STAMP = re.compile(r"[0-9a-f]{64}\Z")


def extract_costs(source, row, ref, stamp):
    """Use exact literal inputs only; blank is unknown, explicit zero is zero."""
    _verified_source_candidate(*source, row, ref, stamp)
    raw, _ = _check_source(*source)
    effective, _ = _ranges(source[1])
    result = {}
    for idx, (key, header) in enumerate(FIELDS, start=32):
        if _cell(raw[0], idx) != header or _cell(effective[0], idx) != header:
            raise ValueError("Actual-cost headings changed; review mapping.")
        a, b = _cell(raw[row-4], idx), _cell(effective[row-4], idx)
        if a in ("", None) and b in ("", None):
            result[key] = None
            continue
        if type(a) not in (int, float) or type(b) not in (int, float) or a != b:
            raise ValueError("Actual-cost inputs must be unchanged literal numbers or blank.")
        try:
            d = Decimal(str(a))
            # Hours are preserved as decimal hours, not rounded to whole minutes.
            # GBP fields must be exact integer pennies.
            if not d.is_finite() or not 0 <= d <= (10000 if key == "hours" else 1000000):
                raise ValueError("Actual-cost input outside supported range.")
            if key == "hours":
                if d != d.quantize(Decimal("0.000001")):
                    raise ValueError("Actual hours exceed supported precision.")
                result[key] = format(d.normalize(), "f")
            else:
                p = d * 100
                if p != p.to_integral_value():
                    raise ValueError("Actual costs must be exact pennies.")
                result[key] = int(p)
        except InvalidOperation as exc:
            raise ValueError("Invalid actual-cost number.") from exc
    if all(v is None for v in result.values()):
        raise ValueError("All historical actual-cost inputs are missing.")
    return result


def tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_cost_observations(
        id INTEGER PRIMARY KEY, work_id TEXT NOT NULL REFERENCES hq_work_records(id),
        operation TEXT NOT NULL UNIQUE, stamp TEXT NOT NULL, state TEXT NOT NULL
        CHECK(state IN ('observed','withdrawn')), manifest TEXT NOT NULL,
        actor TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_historical_cost_work
        ON hq_historical_cost_observations(work_id,id)""")


def linked(db, work_id):
    if not HEX.fullmatch(work_id):
        raise LookupError("Invalid work reference.")
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_historical_created_work'").fetchone():
        raise LookupError("No active imported historical work.")
    row = db.execute("""SELECT h.*,w.revision AS current_revision
        FROM hq_historical_created_work h JOIN hq_work_records w ON w.id=h.work_id
        JOIN hq_historical_j_candidates c ON c.id=h.candidate_id
        WHERE h.work_id=? AND h.reversed_at IS NULL AND c.withdrawn_at IS NULL
        AND c.work_id=h.work_id AND c.job_ref=h.job_ref AND c.sheet_row=h.sheet_row
        AND NOT EXISTS (SELECT 1 FROM hq_work_archive_state a
        WHERE a.work_id=w.id AND a.is_archived=1)""", (work_id,)).fetchone()
    if not row:
        raise LookupError("No active imported historical work.")
    return dict(row)


def latest(db, work_id):
    return db.execute("""SELECT * FROM hq_historical_cost_observations
        WHERE work_id=? ORDER BY id DESC LIMIT 1""", (work_id,)).fetchone()


def process(db, work_id, source, operation, owner, approval=None):
    if not HEX.fullmatch(operation):
        raise ValueError("Invalid operation.")
    link = linked(db, work_id)
    actor = _actor_fingerprint(owner)
    prior = db.execute("SELECT * FROM hq_historical_cost_observations WHERE operation=?", (operation,)).fetchone()
    if prior:
        if (approval and prior["work_id"] == work_id and prior["actor"] == actor
                and prior["state"] == "observed" and hmac.compare_digest(approval, prior["stamp"])):
            return None
        raise ValueError("Operation already used.")
    last = latest(db, work_id)
    if last and last["state"] == "observed":
        raise ValueError("Withdraw the existing observation before recording a replacement.")
    imported = json.loads(link["manifest"])
    costs = extract_costs(source, link["sheet_row"], link["job_ref"], imported["source_stamp"])
    manifest = dict(work_id=work_id, job_ref=link["job_ref"], sheet_row=link["sheet_row"],
                    source_stamp=imported["source_stamp"], source_work_id=link["id"],
                    work_revision=link["current_revision"], prior_event=last["id"] if last else None,
                    costs=costs, operation=operation)
    stamp = source_helpers()._stamp(["historical-cost-v1", manifest, actor])
    if approval is None:
        return manifest, stamp
    if not hmac.compare_digest(stamp, approval):
        raise ValueError("Source or HQ record changed; prepare a fresh approval.")
    db.execute("""INSERT INTO hq_historical_cost_observations
        (work_id,operation,stamp,state,manifest,actor,created_at)
        VALUES(?,?,?,'observed',?,?,?)""",
        (work_id,operation,stamp,json.dumps(manifest,sort_keys=True),actor,
         datetime.now(timezone.utc).isoformat()))
    return None


def withdraw(db, work_id, operation, stamp, owner):
    if not HEX.fullmatch(operation) or not STAMP.fullmatch(stamp):
        raise ValueError("Invalid withdrawal.")
    actor = _actor_fingerprint(owner)
    prior = db.execute("SELECT * FROM hq_historical_cost_observations WHERE operation=?", (operation,)).fetchone()
    if prior:
        if (prior["state"] == "withdrawn" and prior["work_id"] == work_id and prior["actor"] == actor
                and hmac.compare_digest(prior["stamp"], stamp)):
            return
        raise ValueError("Operation already used.")
    baseline = baseline_latest(db, work_id)
    if baseline and baseline["state"] == "adopted":
        raise ValueError("Withdraw the adopted cost baseline first.")
    last = latest(db, work_id)
    if not last or last["state"] != "observed" or not hmac.compare_digest(last["stamp"],stamp):
        raise ValueError("Observation changed; reload before withdrawal.")
    db.execute("""INSERT INTO hq_historical_cost_observations
        (work_id,operation,stamp,state,manifest,actor,created_at)
        VALUES(?,?,?,'withdrawn',?,?,?)""",
        (work_id,operation,stamp,last["manifest"],actor,datetime.now(timezone.utc).isoformat()))



# An aggregate baseline is a native cost record, not fictitious detailed
# material quantities or timed stages. It never enters the detailed subtotal.
def baseline_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_cost_baselines(
        id INTEGER PRIMARY KEY, work_id TEXT NOT NULL REFERENCES hq_work_records(id),
        operation TEXT NOT NULL UNIQUE, stamp TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('adopted','withdrawn')),
        manifest TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_cost_baseline_work
        ON hq_historical_cost_baselines(work_id,id)""")


def _exists(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (table,)).fetchone() is not None


def baseline_latest(db, work_id):
    if not _exists(db, "hq_historical_cost_baselines"):
        return None
    return db.execute("""SELECT * FROM hq_historical_cost_baselines
        WHERE work_id=? ORDER BY id DESC LIMIT 1""", (work_id,)).fetchone()


def _detail_overlap(db, work_id):
    # Any active detail (even unknown or zero) requires manual reconciliation.
    for table in ("hq_work_time_entries", "hq_work_material_entries", "hq_work_expense_entries"):
        if _exists(db, table) and db.execute(
                "SELECT 1 FROM " + table + " WHERE work_id=? AND voided_at IS NULL LIMIT 1",
                (work_id,)).fetchone():
            return True
    return False


def baseline_snapshot(db, work_id):
    """Read in the caller's transaction; no creation or modification here."""
    row = baseline_latest(db, work_id)
    if not row or row["state"] != "adopted":
        return None
    manifest = json.loads(row["manifest"])
    try:
        link = linked(db, work_id)
        observation = latest(db, work_id)
        valid = (observation and observation["state"] == "observed"
                 and observation["id"] == manifest["observation_id"]
                 and link["current_revision"] == manifest["work_revision"]
                 and not _detail_overlap(db, work_id))
    except LookupError:
        valid = False
    costs = manifest["costs"]
    money = [costs[k] for k in ("materials", "consumables", "extra")]
    return dict(manifest, current=bool(valid),
                nonlabour_pence=sum(money) if all(v is not None for v in money) else None,
                known_nonlabour_pence=sum(v for v in money if v is not None)
                if any(v is not None for v in money) else None)


def baseline_process(db, work_id, operation, owner, approval=None, revoke=False):
    if not HEX.fullmatch(operation):
        raise ValueError("Invalid baseline operation.")
    actor = _actor_fingerprint(owner)
    link = linked(db, work_id)
    last = baseline_latest(db, work_id)
    prior = db.execute("SELECT * FROM hq_historical_cost_baselines WHERE operation=?",
                       (operation,)).fetchone()
    if prior:
        if (approval and last and last["id"] == prior["id"]
                and prior["work_id"] == work_id and prior["actor"] == actor
                and prior["state"] == ("withdrawn" if revoke else "adopted")
                and hmac.compare_digest(approval, prior["stamp"])):
            return None
        raise ValueError("Baseline operation already used.")
    if revoke:
        if (not last or last["state"] != "adopted" or not approval
                or not hmac.compare_digest(approval, last["stamp"])):
            raise ValueError("Baseline changed; reload before withdrawal.")
        manifest, stamp, state = json.loads(last["manifest"]), last["stamp"], "withdrawn"
    else:
        if last and last["state"] == "adopted":
            raise ValueError("Withdraw the existing baseline before replacing it.")
        observation = latest(db, work_id)
        if not observation or observation["state"] != "observed":
            raise ValueError("Save historical cost evidence first.")
        evidence = json.loads(observation["manifest"])
        if link["current_revision"] != evidence["work_revision"]:
            raise ValueError("HQ work changed since observation; review fresh evidence.")
        if _detail_overlap(db, work_id):
            raise ValueError("Existing active cost details overlap; reconcile manually.")
        manifest = dict(work_id=work_id, job_ref=link["job_ref"],
                        sheet_row=link["sheet_row"], work_revision=link["current_revision"],
                        observation_id=observation["id"], observation_stamp=observation["stamp"],
                        source_stamp=evidence["source_stamp"], costs=evidence["costs"],
                        prior_event=last["id"] if last else None, operation=operation)
        stamp = source_helpers()._stamp(["historical-cost-baseline-v1", manifest, actor])
        if approval is None:
            return manifest, stamp
        if not hmac.compare_digest(stamp, approval):
            raise ValueError("Baseline evidence or HQ work changed; prepare a fresh approval.")
        state = "adopted"
    db.execute("""INSERT INTO hq_historical_cost_baselines
        (work_id,operation,stamp,state,manifest,actor,created_at)
        VALUES(?,?,?,?,?,?,?)""", (work_id,operation,stamp,state,
        json.dumps(manifest,sort_keys=True),actor,datetime.now(timezone.utc).isoformat()))
    return None


def register_routes(bp):
    @bp.route("/hq/migration/work/<work_id>/actual-costs", methods=["GET","POST"])
    @required
    def historical_actual_costs(work_id):
        preview=stamp=error=notice=None
        history=[]
        baselines=[]
        baseline_preview=baseline_stamp=baseline_view=None
        status=200
        operation=uuid.uuid4().hex
        try:
            if not storage_ready():
                raise RuntimeError("Persistent storage required.")
            source=None
            action=None
            if request.method=="POST":
                if request.content_length is None or request.content_length>4096:
                    abort(413)
                if not secrets.compare_digest(request.form.get("csrf",""),session.get("csrf","not-set")):
                    abort(403)
                action=request.form.get("action")
                operation=request.form.get("operation","")
                stamp=request.form.get("stamp","")
                if action in ("save","withdraw","baseline_save","baseline_withdraw") and (request.form.get("confirm")!="yes" or not STAMP.fullmatch(stamp)):
                    raise ValueError("Explicit confirmation required.")
                if action in ("prepare","save"):
                    provider=current_app.extensions["hq_sheets_connection"]()
                    if not provider or provider[1]<=time.time():
                        raise ValueError("Connect read-only Sheets access first.")
                    source=source_helpers()._read_live(provider[0])
                elif action not in ("withdraw","baseline_prepare","baseline_save","baseline_withdraw"):
                    abort(400)
            with closing(connect()) as db, db:
                linked(db,work_id)
                tables(db)
                baseline_tables(db)
                db.execute("BEGIN IMMEDIATE")
                if action in ("baseline_prepare","baseline_save","baseline_withdraw"):
                    result=baseline_process(db,work_id,operation,session.get("google_sub"),
                        stamp if action!="baseline_prepare" else None,action=="baseline_withdraw")
                    if result:
                        baseline_preview,baseline_stamp=result
                    else:
                        notice="Historical cost baseline withdrawn; history retained." if action=="baseline_withdraw" else "Historical aggregate cost baseline saved; no detailed costs or payments posted."
                elif action=="withdraw":
                    withdraw(db,work_id,operation,stamp,session.get("google_sub"))
                    notice="Observation withdrawn; history retained."
                elif action in ("prepare","save"):
                    result=process(db,work_id,source,operation,session.get("google_sub"),stamp if action=="save" else None)
                    if result:
                        preview,stamp=result
                    else:
                        notice="Historical observation saved; no live cost or payment posted."
                history=[dict(r) for r in db.execute("""SELECT * FROM hq_historical_cost_observations
                    WHERE work_id=? ORDER BY id DESC LIMIT 100""",(work_id,))]
                baselines=[dict(r) for r in db.execute("""SELECT * FROM hq_historical_cost_baselines
                    WHERE work_id=? ORDER BY id DESC LIMIT 100""",(work_id,))]
                baseline_view=baseline_snapshot(db,work_id)
            if action in ("save","withdraw","baseline_save","baseline_withdraw"):
                operation=uuid.uuid4().hex
        except LookupError:
            abort(404)
        except ValueError as exc:
            error,status=str(exc),409
        except (sqlite3.Error,OSError,RuntimeError):
            error,status="Storage or source unavailable.",503
        for item in history+baselines:
            item["details"]=json.loads(item["manifest"])
        response=make_response(render_template("hq_historical_actual_costs.html",work_id=work_id,
            preview=preview,stamp=stamp,history=history,error=error,notice=notice,
            operation=operation,fields=FIELDS,csrf=session.get("csrf",""),
            baselines=baselines,baseline_preview=baseline_preview,baseline_stamp=baseline_stamp,
            baseline_view=baseline_view),status)
        response.headers["Cache-Control"]="no-store"
        return response
