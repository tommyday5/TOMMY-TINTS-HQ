"""Owner-only per-row mapping draft. Never updates canonical work or receipts."""
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from flask import abort, redirect, render_template, request, session, url_for
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_historical_job_full_evidence import HEADERS, WIDTH, _digest
from hq_historical_job_field_review import read_plan
from hq_historical_job_dry_run import read_comparison, _pence
from hq_work_progress import _actor_fingerprint


def _context(db, batch_id, candidate_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_historical_job_source_rows'").fetchone():
        raise LookupError("No stage")
    row = db.execute("""SELECT s.*,b.archived_at,t.plan_revision,t.dry_revision,t.headers,
        i.work_id,i.work_revision,w.revision AS live_revision,w.price_pence,w.status,
        c.withdrawn_at,c.id AS live_candidate,c.source_stamp AS live_stamp,i.source_stamp,
        EXISTS(SELECT 1 FROM hq_work_archive_state a WHERE a.work_id=i.work_id AND a.is_archived=1) AS work_archived
        FROM hq_historical_job_source_rows s
        JOIN hq_historical_job_source_stages t ON t.batch_id=s.batch_id
        JOIN hq_historical_job_batches b ON b.id=s.batch_id
        JOIN hq_historical_job_batch_items i ON i.batch_id=s.batch_id AND i.candidate_id=s.candidate_id
        LEFT JOIN hq_work_records w ON w.id=i.work_id
        LEFT JOIN hq_historical_j_candidates c ON c.id=i.candidate_id
        WHERE s.batch_id=? AND s.candidate_id=?""", (batch_id,candidate_id)).fetchone()
    if not row:
        raise LookupError("No staged row")
    raw, effective = json.loads(row["raw_cells"]), json.loads(row["effective_cells"])
    if (tuple(json.loads(row["headers"])) != HEADERS or len(raw) != WIDTH or len(effective) != WIDTH
            or not hmac.compare_digest(_digest([row["sheet_row"],row["job_ref"],HEADERS,raw,effective]),row["source_digest"])):
        raise ValueError("Staged evidence inconsistent")
    plan, dry = read_plan(db,batch_id), read_comparison(db,batch_id)
    current = bool(not row["archived_at"] and row["live_candidate"] and not row["withdrawn_at"]
        and not row["work_archived"] and row["live_stamp"] == row["source_stamp"]
        and row["live_revision"] == row["work_revision"] and plan and dry
        and plan["revision"] == row["plan_revision"] and dry["revision"] == row["dry_revision"]
        and dry["plan_revision"] == plan["revision"])
    price = _pence(effective[16])
    literal = price is not None and _pence(raw[16]) == price
    eligible = bool(current and literal and row["status"] == "draft" and plan["fields"][16]["decision"] == "n")
    history = []
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_historical_job_quote_mapping'").fetchone():
        history = [dict(r) for r in db.execute("""SELECT revision,decision,source_pence,native_pence,created_at
            FROM hq_historical_job_quote_mapping WHERE batch_id=? AND candidate_id=?
            ORDER BY revision DESC LIMIT 50""",(batch_id,candidate_id))]
    return {"row":dict(row),"cells":list(zip(HEADERS,raw,effective)),"current":current,
            "eligible":eligible,"source_pence":price,"history":history}


def save_mapping(batch_id,candidate_id,expected,decision,owner):
    if not storage_ready() or type(expected) is not int or not 0 <= expected < 2**63-1 or decision not in ("preserve","propose"):
        raise ValueError("Invalid mapping")
    actor = _actor_fingerprint(owner)
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_job_quote_mapping (
            batch_id TEXT NOT NULL,candidate_id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),
            decision TEXT NOT NULL CHECK(decision IN ('preserve','propose')),
            source_pence INTEGER,native_pence INTEGER,source_digest TEXT NOT NULL,work_revision INTEGER NOT NULL,
            actor_fingerprint TEXT NOT NULL,created_at TEXT NOT NULL,
            PRIMARY KEY(batch_id,candidate_id,revision),
            FOREIGN KEY(batch_id,candidate_id) REFERENCES hq_historical_job_source_rows(batch_id,candidate_id))""")
        db.execute("BEGIN IMMEDIATE")
        ctx = _context(db,batch_id,candidate_id)
        if not ctx["current"] or (decision == "propose" and not ctx["eligible"]):
            raise ValueError("Mapping not eligible")
        prior = ctx["history"][0] if ctx["history"] else None
        revision = prior["revision"] if prior else 0
        if prior and prior["decision"] == decision and expected in (revision,revision-1):
            return
        if revision != expected:
            raise ValueError("Mapping changed")
        row = ctx["row"]
        db.execute("INSERT INTO hq_historical_job_quote_mapping VALUES(?,?,?,?,?,?,?,?,?,?)",
            (batch_id,candidate_id,revision+1,decision,ctx["source_pence"],row["price_pence"],
             row["source_digest"],row["work_revision"],actor,datetime.now(timezone.utc).isoformat()))


def register_mapping_route(bp):
    @bp.route("/hq/migration/job-batches/<batch_id>/mapping/<candidate_id>",methods=["GET","POST"])
    @required
    def historical_quote_mapping(batch_id,candidate_id):
        from hq_historical_preview import _HEX
        if not _HEX.fullmatch(batch_id) or not _HEX.fullmatch(candidate_id):
            abort(404)
        error = None
        if request.method == "POST":
            if request.content_length is None or request.content_length > 4096:
                abort(413)
            if not secrets.compare_digest(request.form.get("csrf",""),session.get("csrf","not-set")):
                abort(403)
            revision = request.form.get("revision","")
            try:
                if not revision.isascii() or not revision.isdecimal() or len(revision)>18:
                    raise ValueError("Invalid revision")
                save_mapping(batch_id,candidate_id,int(revision),request.form.get("decision"),session.get("google_sub"))
                return redirect(url_for(".historical_quote_mapping",batch_id=batch_id,candidate_id=candidate_id,saved="1"),code=303)
            except (ValueError,LookupError,sqlite3.Error,OSError):
                error = "Mapping not saved. Reload current evidence and revision."
        root = os.environ.get("HQ_DATA_DIR","")
        path = Path(root)/"customer_requests.sqlite3"
        if not root or not Path(root).is_absolute() or not path.is_file():
            abort(404)
        try:
            with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=3)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                ctx = _context(db,batch_id,candidate_id)
        except LookupError:
            abort(404)
        except (ValueError,sqlite3.Error,OSError):
            abort(409)
        return render_template("hq_historical_job_quote_mapping.html",ctx=ctx,error=error,
            csrf=session.get("csrf",""),saved=request.args.get("saved")=="1")
