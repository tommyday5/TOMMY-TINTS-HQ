"""Owner-only immutable historical Jobs review batches; never an import.

The existing candidate fingerprints cover Jobs A:AZ only. A batch is a
reversible review envelope, not full-source verification or write approval.
"""
import json
import os
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import abort, current_app, redirect, render_template, request, session, url_for
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_work_progress import _actor_fingerprint


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_job_batches (
        id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
        selection TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
        actor_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
        archived_at TEXT, CHECK(revision IN (0,1)))""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_job_batch_items (
        batch_id TEXT NOT NULL REFERENCES hq_historical_job_batches(id),
        candidate_id TEXT NOT NULL REFERENCES hq_historical_j_candidates(id),
        job_ref TEXT NOT NULL, sheet_row INTEGER NOT NULL,
        work_id TEXT NOT NULL, work_revision INTEGER NOT NULL,
        source_stamp TEXT NOT NULL,
        PRIMARY KEY(batch_id,candidate_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_job_batch_events (
        batch_id TEXT NOT NULL REFERENCES hq_historical_job_batches(id),
        revision INTEGER NOT NULL, action TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL, occurred_at TEXT NOT NULL,
        PRIMARY KEY(batch_id,revision))""")


def create_batch(candidate_ids, operation_id, *, formula, computed, owner_identity):
    from hq_historical_preview import _HEX, _verified_source_candidate
    if not storage_ready() or not isinstance(operation_id, str) or not _HEX.fullmatch(operation_id):
        raise ValueError("Invalid storage or submission")
    if (not isinstance(candidate_ids, list) or not 1 <= len(candidate_ids) <= 20
            or any(not isinstance(i, str) or not _HEX.fullmatch(i) for i in candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids)):
        raise ValueError("Choose 1–20 distinct saved proposals")
    selection = json.dumps(sorted(candidate_ids), separators=(",", ":"))
    actor = _actor_fingerprint(owner_identity)
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT id,selection FROM hq_historical_job_batches WHERE operation_id=?",
                           (operation_id,)).fetchone()
        if prior:
            if prior["selection"] != selection:
                raise ValueError("Submission already used for another selection")
            return prior["id"]
        items = []
        for cid in sorted(candidate_ids):
            row = db.execute("""SELECT c.id,c.job_ref,c.sheet_row,c.work_id,
                c.work_revision,c.source_stamp,c.withdrawn_at,w.revision AS live_revision
                FROM hq_historical_j_candidates c JOIN hq_work_records w ON w.id=c.work_id
                WHERE c.id=? AND NOT EXISTS (SELECT 1 FROM hq_work_archive_state a
                    WHERE a.work_id=w.id AND a.is_archived=1)""", (cid,)).fetchone()
            if row is None or row["withdrawn_at"] is not None or row["live_revision"] != row["work_revision"]:
                raise ValueError("A selected proposal is withdrawn, archived or stale")
            _verified_source_candidate(formula, computed, row["sheet_row"], row["job_ref"], row["source_stamp"])
            if db.execute("""SELECT 1 FROM hq_historical_job_batch_items i
                    JOIN hq_historical_job_batches b ON b.id=i.batch_id
                    WHERE i.candidate_id=? AND b.archived_at IS NULL""", (cid,)).fetchone():
                raise ValueError("A proposal is already in an active review batch")
            items.append(row)
        bid = uuid.uuid4().hex
        db.execute("""INSERT INTO hq_historical_job_batches
            (id,operation_id,selection,actor_fingerprint,created_at) VALUES(?,?,?,?,?)""",
            (bid, operation_id, selection, actor, now))
        db.executemany("""INSERT INTO hq_historical_job_batch_items
            VALUES(?,?,?,?,?,?,?)""", [(bid, r["id"], r["job_ref"], r["sheet_row"],
            r["work_id"], r["work_revision"], r["source_stamp"]) for r in items])
        db.execute("INSERT INTO hq_historical_job_batch_events VALUES(?,?,?,?,?)",
                   (bid, 0, "created", actor, now))
        return bid


def archive_batch(batch_id, expected_revision, owner_identity):
    from hq_historical_preview import _HEX
    if (not storage_ready() or not isinstance(batch_id, str) or not _HEX.fullmatch(batch_id)
            or type(expected_revision) is not int or expected_revision != 0):
        raise ValueError("Invalid batch or revision")
    actor = _actor_fingerprint(owner_identity)
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT revision,archived_at FROM hq_historical_job_batches WHERE id=?",
                         (batch_id,)).fetchone()
        if not row:
            raise ValueError("Unknown review batch")
        if row["archived_at"] is not None:
            return
        if row["revision"] != expected_revision:
            raise ValueError("Review batch changed")
        db.execute("UPDATE hq_historical_job_batches SET archived_at=?,revision=1 WHERE id=?",
                   (now, batch_id))
        db.execute("INSERT INTO hq_historical_job_batch_events VALUES(?,?,?,?,?)",
                   (batch_id, 1, "archived", actor, now))


def list_batches():
    root = os.environ.get("HQ_DATA_DIR", "")
    path = Path(root) / "customer_requests.sqlite3"
    if not root or not Path(root).is_absolute() or not path.is_file():
        return []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        if not db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='hq_historical_job_batches'""").fetchone():
            return []
        batches = [dict(r) for r in db.execute("""SELECT id,revision,created_at,archived_at
            FROM hq_historical_job_batches ORDER BY (archived_at IS NOT NULL),created_at DESC,id DESC LIMIT 100""")]
        for b in batches:
            b["items"] = [dict(r) for r in db.execute("""SELECT i.job_ref,i.sheet_row,i.work_id,
                i.work_revision,i.candidate_id,CASE WHEN c.withdrawn_at IS NOT NULL
                  OR c.id IS NULL OR w.id IS NULL OR w.revision != i.work_revision
                  OR EXISTS(SELECT 1 FROM hq_work_archive_state a WHERE a.work_id=i.work_id AND a.is_archived=1)
                  THEN 1 ELSE 0 END AS stale
                FROM hq_historical_job_batch_items i
                LEFT JOIN hq_historical_j_candidates c ON c.id=i.candidate_id
                LEFT JOIN hq_work_records w ON w.id=i.work_id WHERE i.batch_id=?
                ORDER BY i.sheet_row""", (b["id"],))]
        return batches


def register_routes(bp):
    @bp.route("/hq/migration/job-batches", methods=["GET", "POST"])
    @required
    def job_batches():
        from hq_historical_preview import _current_candidates, _read_live
        error = None
        if request.method == "POST":
            if request.content_length is None or request.content_length > 4096:
                abort(413)
            if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
                abort(403)
            action = request.form.get("action")
            if action not in ("create", "archive"):
                abort(400)
            try:
                if action == "archive":
                    if request.form.get("revision") != "0":
                        abort(400)
                    archive_batch(request.form.get("batch_id", ""), 0, session.get("google_sub"))
                else:
                    provider = current_app.extensions["hq_sheets_connection"]()
                    if not provider or provider[1] <= time.time():
                        raise ValueError("Connect Sheets read-only first")
                    formula, computed = _read_live(provider[0])
                    create_batch(request.form.getlist("candidate_id"), request.form.get("operation_id", ""),
                                 formula=formula, computed=computed, owner_identity=session.get("google_sub"))
                return redirect(url_for(".job_batches", saved="1"), code=303)
            except (ValueError, sqlite3.Error, OSError):
                error = "Batch could not be saved. Check current proposals, source access and existing batches."
            except Exception:
                current_app.logger.warning("Historical Jobs review batch unavailable")
                error = "Source comparison unavailable. No batch saved."
        try:
            batches = list_batches()
            candidates = _current_candidates()
        except (sqlite3.Error, OSError):
            batches, candidates = [], []
            error = error or "Review storage unavailable"
        return render_template("hq_historical_job_batches.html", batches=batches,
            candidates=candidates, operation_id=uuid.uuid4().hex, csrf=session.get("csrf", ""),
            error=error, saved=request.args.get("saved") == "1")
