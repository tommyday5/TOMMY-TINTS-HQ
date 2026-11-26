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
        revision INTEGER NOT NULL, entry_id TEXT)""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_timer_open_job
        ON hq_timer_sessions(work_id) WHERE state IN ('running','paused','review')""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_timer_events (
        operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        work_id TEXT NOT NULL, action TEXT NOT NULL, expected_revision INTEGER NOT NULL,
        occurred_at INTEGER NOT NULL)""")

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

def transition(work_id, action, revision, operation_id):
    _validate_id(work_id)
    _validate_id(operation_id)
    if type(revision) is not int or revision < -1 or action not in ("start","pause","resume","finish"):
        raise ValueError("Invalid timer operation")
    with connect() as db:
        _schema(db)
        tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT * FROM hq_timer_events WHERE operation_id=?", (operation_id,)).fetchone()
        if prior:
            if (prior["work_id"],prior["action"],prior["expected_revision"]) != (work_id,action,revision):
                raise StaleWorkRecordError("Timer operation already used")
            return
        work = _read_work(db, work_id)
        archived = db.execute("SELECT is_archived FROM hq_work_archive_state WHERE work_id=?", (work_id,)).fetchone()
        if work["kind"] != "job" or work["status"] not in ("approved","in_progress") or (archived and archived["is_archived"]):
            raise ValueError("Timer requires an active approved job")
        row = _open(db, work_id)
        t = now()
        if action == "start":
            if row or revision != -1:
                raise StaleWorkRecordError("Reload the existing timer")
            sid = uuid.uuid4().hex
            db.execute("INSERT INTO hq_timer_sessions VALUES (?,?,?,?,?,?,?,?)",
                       (sid,work_id,"running",0,0,t,0,None))
        else:
            if not row or row["revision"] != revision:
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
            else:
                raise ValueError("Invalid timer state transition")
            state = {"pause":"paused","resume":"running","finish":"review"}[action]
            if state == "review" and not 60 <= active <= 86459:
                raise ValueError("Review requires 1–1440 whole active minutes; longer sessions require audited manual correction")
            db.execute("""UPDATE hq_timer_sessions SET state=?,active_seconds=?,break_seconds=?,
                       changed_at=?,revision=revision+1 WHERE id=?""",(state,active,breaks,t,sid))
        db.execute("INSERT INTO hq_timer_events VALUES (?,?,?,?,?,?)",
                   (operation_id,sid,work_id,action,revision,t))
