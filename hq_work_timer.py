"""Persistent owner timer sessions. UTC server clock, no external side effects."""
import re
import uuid
from datetime import datetime, timezone
from customer_request_store import connect
from hq_core_records import _schema, StaleWorkRecordError
from hq_work_time import _read_work, _validate_id, _tables as time_tables

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

def live_status(work_id):
    """Read a consistent clock and work revision, without exposing customer data."""
    _validate_id(work_id)
    with connect() as db:
        _schema(db)
        tables(db)
        time_tables(db)
        db.execute("BEGIN")
        work = _read_work(db, work_id)
        timer = _open(db, work_id)
        saved = db.execute("""SELECT COALESCE(SUM(minutes),0) FROM hq_work_time_entries
            WHERE work_id=? AND voided_at IS NULL""", (work_id,)).fetchone()[0]
        checked_at = now()
    elapsed = max(0, checked_at - timer["changed_at"]) if timer else 0
    return {
        "work_revision": work["revision"], "saved_minutes": saved,
        "session_id": timer["id"] if timer else None,
        "timer_revision": timer["revision"] if timer else -1,
        "state": timer["state"] if timer else "standby",
        "active_seconds": timer["active_seconds"] + (elapsed if timer["state"] == "running" else 0) if timer else 0,
        "break_seconds": timer["break_seconds"] + (elapsed if timer["state"] == "paused" else 0) if timer else 0,
        "allowance_minutes": timer["allowance_minutes"] if timer else None,
    }


def open_timer_jobs():
    """Owner-only recovery queue, independent of job search and pagination."""
    with connect() as db:
        _schema(db)
        tables(db)
        time_tables(db)
        db.execute("BEGIN")
        rows = db.execute("""SELECT t.work_id AS id, t.state, t.changed_at,
                t.active_seconds, t.allowance_minutes,
                (SELECT COALESCE(SUM(e.minutes),0) FROM hq_work_time_entries e
                 WHERE e.work_id=t.work_id AND e.voided_at IS NULL) AS saved_minutes,
                w.service, c.name, v.make, v.model, v.registration
            FROM hq_timer_sessions t
            JOIN hq_work_records w ON w.id=t.work_id
            JOIN hq_customers c ON c.id=w.customer_id
            JOIN hq_vehicles v ON v.id=w.vehicle_id
            WHERE t.state IN ('running','paused','review')
            ORDER BY CASE t.state WHEN 'running' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
                t.changed_at DESC,t.id DESC LIMIT 51""").fetchall()
    checked_at = now()
    records = []
    for source in rows[:50]:
        row = dict(source)
        elapsed = max(0, checked_at - row["changed_at"]) if row["state"] == "running" else 0
        row["total_seconds"] = row["saved_minutes"] * 60 + row["active_seconds"] + elapsed
        allowance = row["allowance_minutes"]
        row["budget_state"] = "unknown"
        row["remaining_seconds"] = None
        if allowance:
            budget = allowance * 60
            remaining = budget - row["total_seconds"]
            row["remaining_seconds"] = remaining
            row["budget_state"] = ("over" if remaining < 0 else "reached" if remaining == 0
                                   else "near" if row["total_seconds"] * 5 >= budget * 4
                                   else "within")
        records.append(row)
    return {"records": records, "has_more": len(rows)>50}


def history(work_id):
    """Bounded, owner-facing audit evidence; never modifies timer state."""
    _validate_id(work_id)
    with connect() as db:
        _schema(db)
        tables(db)
        sessions = [dict(r) for r in db.execute("""
            SELECT id,state,active_seconds,break_seconds,allowance_minutes,entry_id,
                   datetime(changed_at,'unixepoch') AS changed_utc
            FROM hq_timer_sessions WHERE work_id=?
            ORDER BY changed_at DESC,id DESC LIMIT 101""", (work_id,))]
        events = [dict(r) for r in db.execute("""
            SELECT session_id,action,detail,expected_revision,
                   datetime(occurred_at,'unixepoch') AS occurred_utc
            FROM hq_timer_events WHERE work_id=?
            ORDER BY occurred_at DESC,expected_revision DESC,operation_id DESC LIMIT 101""", (work_id,))]
        previous = db.execute("""SELECT allowance_minutes FROM hq_timer_sessions
            WHERE work_id=? AND state='saved' ORDER BY changed_at DESC,id DESC LIMIT 1""",
            (work_id,)).fetchone()
    return {"previous_saved_allowance": previous["allowance_minutes"] if previous else None,
            "sessions": sessions[:100], "events": events[:100],
            "truncated": len(sessions)>100 or len(events)>100}


def transition(work_id, action, revision, operation_id, session_id='', allowance='', reason=''):
    _validate_id(work_id)
    _validate_id(operation_id)
    if action != "start":
        _validate_id(session_id)
    elif session_id:
        raise ValueError("Start must not identify an existing timer")
    if type(revision) is not int or revision < -1 or action not in ("start","pause","resume","finish","discard","allowance"):
        raise ValueError("Invalid timer operation")
    with connect() as db:
        _schema(db)
        tables(db)
        db.execute("BEGIN IMMEDIATE")
        detail = ""
        if action in ("start", "allowance"):
            if (action == "allowance" or allowance) and (not re.fullmatch(r"[0-9]{1,5}", allowance) or not 1 <= int(allowance) <= 30000):
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
            if action == "allowance":
                if row["state"] not in ("running", "paused"):
                    raise ValueError("Edit the allowance before finishing the timer")
                # Do not reset changed_at or accumulated active/break time.
                db.execute("UPDATE hq_timer_sessions SET allowance_minutes=?,revision=revision+1 WHERE id=?",
                           (int(allowance), sid))
                db.execute("INSERT INTO hq_timer_events VALUES (?,?,?,?,?,?,?)",
                           (operation_id,sid,work_id,action,revision,t,detail))
                return
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
