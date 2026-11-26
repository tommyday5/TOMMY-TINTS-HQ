"""Persistent owner timer sessions. UTC server clock, no external side effects."""
import re
import uuid
from datetime import datetime, timezone
from customer_request_store import connect
from hq_core_records import _schema, StaleWorkRecordError
from hq_work_time import _read_work, _validate_id

def now():
    return int(datetime.now(timezone.utc).timestamp())

def tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_timer_sessions (
        id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES hq_work_records(id),
        state TEXT NOT NULL, active_seconds INTEGER NOT NULL,
        break_seconds INTEGER NOT NULL, changed_at INTEGER NOT NULL,
        revision INTEGER NOT NULL, entry_id TEXT, allowance_minutes INTEGER)""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_timer_open_job
        ON hq_timer_sessions(work_id) WHERE state IN ('running','paused','review')""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_timer_events (
        operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        work_id TEXT NOT NULL, action TEXT NOT NULL, expected_revision INTEGER NOT NULL,
        occurred_at INTEGER NOT NULL, detail TEXT NOT NULL)""")

def _open(db, work_id):
    row = db.execute("""SELECT * FROM hq_timer_sessions WHERE work_id=?
        AND state IN ('running','paused','review')""",(work_id,)).fetchone()
    return dict(row) if row else None

def snapshot(work_id):
    _validate_id(work_id)
    with connect() as db:
        _schema(db)
        tables(db)
        row = _open(db, work_id)
    if row:
        elapsed = max(0, now() - row["changed_at"])
        row["display_seconds"] = row["active_seconds"] + (elapsed if row["state"] == "running" else 0)
        row["display_break"] = row["break_seconds"] + (elapsed if row["state"] == "paused" else 0)
    return row

def transition(work_id, action, revision, operation_id, session_id='', allowance='', reason=''):
    _validate_id(work_id)
    _validate_id(operation_id)
    if action != "start":
        _validate_id(session_id)
    elif session_id:
        raise ValueError("Start must not identify an existing timer")
    if type(revision) is not int or revision < -1 or action not in ("start","pause","resume","finish","discard"):
        raise ValueError("Invalid timer operation")
    with connect() as db:
        _schema(db)
        tables(db)
        db.execute("BEGIN IMMEDIATE")
        detail = ""
        if action == "start":
            if allowance and (not re.fullmatch(r"[0-9]{1,5}", allowance) or not 1 <= int(allowance) <= 30000):
                raise ValueError("Allowance must be 1–30000 whole minutes")
            detail = allowance
        elif action == "discard":
            detail = reason.strip()
            if not 1 <= len(detail) <= 160:
                raise ValueError("Enter a correction reason (1–160 characters)")
        prior = db.execute("SELECT * FROM hq_timer_events WHERE operation_id=?", (operation_id,)).fetchone()
        if prior:
            if (prior["work_id"],prior["action"],prior["expected_revision"]) != (work_id,action,revision):
                raise StaleWorkRecordError("Timer operation already used")
            if prior["detail"] != detail:
                raise StaleWorkRecordError("Timer operation details changed")
            if action != "start" and prior["session_id"] != session_id:
                raise StaleWorkRecordError("Timer operation belongs to another session")
            return
        work = _read_work(db, work_id)
        archived = db.execute("SELECT is_archived FROM hq_work_archive_state WHERE work_id=?", (work_id,)).fetchone()
        if work["kind"] != "job" or work["status"] not in ("approved","in_progress") or (archived and archived["is_archived"]):
            raise ValueError("Timer requires an active approved job")
        # BEGIN IMMEDIATE serializes competing starts across all owner jobs.
        # Paused sessions remain recoverable; only active work must not overlap.
        if action in ("start", "resume") and db.execute(
                "SELECT 1 FROM hq_timer_sessions WHERE state='running' AND work_id != ? LIMIT 1",
                (work_id,)).fetchone():
            raise ValueError("Pause the running timer on the other job before starting or resuming work")
        row = _open(db, work_id)
        t = now()
        if action == "start":
            if row or revision != -1:
                raise StaleWorkRecordError("Reload the existing timer")
            sid = uuid.uuid4().hex
            db.execute("INSERT INTO hq_timer_sessions VALUES (?,?,?,?,?,?,?,?,?)",
                       (sid,work_id,"running",0,0,t,0,None,int(allowance) if allowance else None))
        else:
            if not row or row["id"] != session_id or row["revision"] != revision:
                raise StaleWorkRecordError("Timer changed; reload")
            sid = row["id"]
            elapsed = t - row["changed_at"]
            if elapsed < 0:
                raise ValueError("Server clock moved backwards; retry later")
            active, breaks = row["active_seconds"], row["break_seconds"]
            if row["state"] == "running" and action in ("pause","finish"):
                active += elapsed
            elif row["state"] == "paused" and action in ("resume","finish"):
                breaks += elapsed
            elif row["state"] == "review" and action == "discard":
                pass
            else:
                raise ValueError("Invalid timer state transition")
            state = {"pause":"paused","resume":"running","finish":"review","discard":"discarded"}[action]
            db.execute("""UPDATE hq_timer_sessions SET state=?,active_seconds=?,break_seconds=?,
                       changed_at=?,revision=revision+1 WHERE id=?""",(state,active,breaks,t,sid))
        db.execute("INSERT INTO hq_timer_events VALUES (?,?,?,?,?,?,?)",
                   (operation_id,sid,work_id,action,revision,t,detail))
