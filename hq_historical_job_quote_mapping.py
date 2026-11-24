"""Owner-only per-row mapping draft. Never updates canonical work or receipts."""
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from flask import abort, current_app, redirect, render_template, request, session, url_for
from customer_request_store import SERVICES, connect, storage_ready
from hq_auth import required
from hq_historical_job_full_evidence import HEADERS, WIDTH, _digest
from hq_historical_job_field_review import read_plan
from hq_historical_job_dry_run import read_comparison, _pence
from hq_work_progress import _actor_fingerprint
from hq_historical_job_actual_mapping import FIELDS, actual_context, save_actual_mapping


def _context(db, batch_id, candidate_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_historical_job_source_rows'").fetchone():
        raise LookupError("No stage")
    row = db.execute("""SELECT s.*,b.archived_at,t.plan_revision,t.dry_revision,t.headers,
        i.work_id,i.work_revision,w.revision AS live_revision,w.price_pence,w.status,w.service,
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
    classification = _classification_context(db, row, raw, effective, plan, current)
    return {"row":dict(row),"cells":list(zip(HEADERS,raw,effective)),"current":current,
            "classification":classification,
            "actual":actual_context(db, row, raw, effective, plan, current),
            "eligible":eligible,"source_pence":price,"history":history}


def _classification_context(db, row, raw, effective, plan, current):
    # Single-service drafts only. Do not collapse a multi-service job or infer
    # a customer's account classification from a historical job's price tier.
    tier = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_native_work_details'").fetchone():
        detail = db.execute("SELECT customer_tier FROM hq_native_work_details WHERE work_id=?",
                            (row["work_id"],)).fetchone()
        if detail:
            tier = detail["customer_tier"]
    fields = []
    for field, index, native, choices in (
        ("service", 5, row["service"], sorted(SERVICES)),
        ("tier", 3, tier, ["retail", "trade", "maintenance"]),
    ):
        value = raw[index]
        literal = (isinstance(value, str) and bool(value.strip()) and len(value) <= 160
                   and not value.lstrip().startswith("=") and value == effective[index])
        eligible = bool(current and literal and row["status"] == "draft"
                        and tier is not None and plan["fields"][index]["decision"] == "n")
        if field == "service":
            eligible = eligible and all(v in ("", None) for v in raw[6:10] + effective[6:10])
        else:
            # Tier is an exact, case-insensitive match; no arbitrary reclassification.
            choices = [v for v in choices if literal and value.strip().casefold() == v]
            eligible = eligible and bool(choices)
        history = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_historical_job_classification_mapping'").fetchone():
            history = [dict(r) for r in db.execute("""SELECT revision,decision,target,native_value,created_at
                FROM hq_historical_job_classification_mapping
                WHERE batch_id=? AND candidate_id=? AND field=? ORDER BY revision DESC LIMIT 50""",
                (row["batch_id"], row["candidate_id"], field))]
        fields.append(dict(field=field, label=HEADERS[index], source=effective[index],
                           native=native, eligible=eligible, choices=choices, history=history))
    return fields


def save_classification(batch_id, candidate_id, expected, field, decision, target, owner):
    if (not storage_ready() or type(expected) is not int or not 0 <= expected < 2**63-1
            or field not in ("service", "tier") or decision not in ("preserve", "propose")
            or not isinstance(target, str) or len(target) > 80
            or (decision == "preserve" and target)):
        raise ValueError("Invalid classification mapping")
    actor = _actor_fingerprint(owner)
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_job_classification_mapping (
            batch_id TEXT NOT NULL,candidate_id TEXT NOT NULL,field TEXT NOT NULL CHECK(field IN ('service','tier')),
            revision INTEGER NOT NULL CHECK(revision>0),decision TEXT NOT NULL CHECK(decision IN ('preserve','propose')),
            target TEXT NOT NULL,native_value TEXT,source_digest TEXT NOT NULL,work_revision INTEGER NOT NULL,
            actor_fingerprint TEXT NOT NULL,created_at TEXT NOT NULL,
            PRIMARY KEY(batch_id,candidate_id,field,revision),
            FOREIGN KEY(batch_id,candidate_id) REFERENCES hq_historical_job_source_rows(batch_id,candidate_id))""")
        db.execute("BEGIN IMMEDIATE")
        ctx = _context(db,batch_id,candidate_id)
        item = next(i for i in ctx["classification"] if i["field"] == field)
        if not ctx["current"] or (decision == "propose" and (not item["eligible"] or target not in item["choices"])):
            raise ValueError("Classification mapping not eligible")
        prior = item["history"][0] if item["history"] else None
        revision = prior["revision"] if prior else 0
        if prior and prior["decision"] == decision and prior["target"] == target and expected in (revision,revision-1):
            return
        if expected != revision:
            raise ValueError("Classification mapping changed")
        row = ctx["row"]
        db.execute("INSERT INTO hq_historical_job_classification_mapping VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id,candidate_id,field,revision+1,decision,target,item["native"],row["source_digest"],
             row["work_revision"],actor,datetime.now(timezone.utc).isoformat()))


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
        rehearsal = None
        backup_saved = None
        import_result = None
        if request.method == "POST":
            if request.content_length is None or request.content_length > 4096:
                abort(413)
            if not secrets.compare_digest(request.form.get("csrf",""),session.get("csrf","not-set")):
                abort(403)
            if request.form.get("action") in ("prepare_import","execute_import","reverse_import"):
                from hq_historical_price_import import prepare, execute, reverse
                import time
                action = request.form["action"]
                expected = {"csrf","action","snapshot_id"} if action == "prepare_import" else {"csrf","action","manifest_id","confirm_import"}
                if set(request.form) != expected or any(len(request.form.getlist(k)) != 1 for k in request.form):
                    abort(400)
                if action != "prepare_import" and request.form.get("confirm_import") != "yes":
                    abort(400)
                try:
                    owner = session.get("google_sub")
                    if action == "reverse_import":
                        reverse(batch_id,candidate_id,request.form["manifest_id"],owner)
                        import_result = "Imported price reversed; revision and audit history retained."
                    else:
                        from hq_historical_preview import _read_live
                        from hq_historical_job_full_evidence import read_full_width
                        provider = current_app.extensions["hq_sheets_connection"]()
                        if not provider or provider[1] <= time.time():
                            raise ValueError("Read-only Sheets connection required")
                        source = (*_read_live(provider[0]), *read_full_width(provider[0]))
                        if action == "prepare_import":
                            prepare(batch_id,candidate_id,request.form["snapshot_id"],source,owner)
                            import_result = "Exact one-row approval manifest saved; no canonical data changed."
                        else:
                            execute(batch_id,candidate_id,request.form["manifest_id"],source,owner)
                            import_result = "Historical quoted price imported and verified. No payment or booking recorded."
                except (ValueError,LookupError,sqlite3.Error,OSError):
                    error = "Import action blocked. Check current evidence, single-row batch, identity review, snapshot and draft state."
                except Exception:
                    current_app.logger.warning("Historical price import action unavailable")
                    error = "Import action unavailable. Reload the saved manifest history before retrying."
            elif request.form.get("action") == "encrypted_export":
                if set(request.form) != {"csrf","action","snapshot_id","public_key","confirm_export"} or any(len(request.form.getlist(k)) != 1 for k in request.form) or request.form.get("confirm_export") != "yes":
                    abort(400)
                from flask import Response
                from hq_recovery_export import export_snapshot
                from cryptography.exceptions import UnsupportedAlgorithm
                try:
                    envelope = export_snapshot(request.form.get("snapshot_id"),request.form.get("public_key"))
                except (ValueError,LookupError,sqlite3.Error,OSError,UnsupportedAlgorithm):
                    error = "Encrypted export not completed. Check snapshot and RSA public key."
                else:
                    return Response(envelope,mimetype="application/octet-stream",headers={
                        "Content-Disposition": 'attachment; filename="tommy-tints-hq-recovery.tthq"',
                        "Cache-Control": "private, no-store, max-age=0",
                        "Pragma": "no-cache",
                        "X-Content-Type-Options": "nosniff",
                    })
            elif request.form.get("action") == "private_backup":
                if set(request.form) != {"csrf","action","operation_id","confirm_backup"} or any(len(request.form.getlist(k)) != 1 for k in request.form) or request.form.get("confirm_backup") != "yes":
                    abort(400)
                from hq_private_recovery import create_snapshot
                try:
                    backup_root = Path(os.environ.get("HQ_DATA_DIR",""))
                    backup_source = backup_root / "customer_requests.sqlite3"
                    if not backup_root.is_absolute() or not backup_source.is_file():
                        raise LookupError("Canonical database unavailable")
                    with closing(sqlite3.connect(backup_source.as_uri()+"?mode=ro",uri=True,timeout=1)) as db:
                        db.row_factory = sqlite3.Row
                        db.execute("PRAGMA query_only=ON")
                        db.execute("BEGIN")
                        _context(db,batch_id,candidate_id)
                    backup_saved = create_snapshot(request.form.get("operation_id"))
                except (ValueError,LookupError,sqlite3.Error,OSError):
                    error = "Private backup not completed. Check storage, capacity, integrity and concurrent operations."
            elif request.form.get("action") == "rehearse":
                if set(request.form) != {"csrf","action"} or any(len(request.form.getlist(k)) != 1 for k in request.form):
                    abort(400)
                from hq_historical_job_rehearsal import rehearse_mapping
                try:
                    rehearsal = rehearse_mapping(batch_id,candidate_id)
                except (ValueError,LookupError,sqlite3.Error,OSError):
                    error = "Rehearsal not completed. Check current proposals, database availability and planned deposit."
            else:
                revision = request.form.get("revision","")
                try:
                    if not revision.isascii() or not revision.isdecimal() or len(revision)>18:
                        raise ValueError("Invalid revision")
                    field = request.form.get("field", "quote")
                    if field == "quote":
                        save_mapping(batch_id,candidate_id,int(revision),request.form.get("decision"),session.get("google_sub"))
                    elif field in FIELDS:
                        save_actual_mapping(batch_id,candidate_id,int(revision),field,
                                            request.form.get("decision"),session.get("google_sub"))
                    else:
                        decision = request.form.get("decision")
                        save_classification(batch_id,candidate_id,int(revision),field,decision,
                                            request.form.get("target","") if decision == "propose" else "",
                                            session.get("google_sub"))
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
                from hq_historical_price_import import read_manifests
                manifests = read_manifests(db,batch_id,candidate_id)
        except LookupError:
            abort(404)
        except (ValueError,sqlite3.Error,OSError):
            abort(409)
        from hq_private_recovery import list_snapshots
        try:
            backups = list_snapshots()
        except (ValueError,LookupError,OSError):
            backups = None
        return render_template("hq_historical_job_quote_mapping.html",ctx=ctx,error=error,
            backups=backups,backup_saved=backup_saved,backup_operation=secrets.token_hex(16),
            manifests=manifests,import_result=import_result,
            csrf=session.get("csrf",""),saved=request.args.get("saved")=="1",rehearsal=rehearsal)
