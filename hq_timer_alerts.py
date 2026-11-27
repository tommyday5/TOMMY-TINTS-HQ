"""Durable private timer warnings; no network delivery or job mutations."""
import uuid
from customer_request_store import connect
from hq_core_records import _schema
import hq_work_timer as timer

def _tables(db):
    _schema(db)
    timer.tables(db)
    timer.time_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_timer_alerts (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, work_id TEXT NOT NULL,
        allowance_minutes INTEGER NOT NULL, level TEXT NOT NULL CHECK(level IN ('near','limit')),
        observed_seconds INTEGER NOT NULL, observed_at INTEGER NOT NULL,
        acknowledged_at INTEGER, acknowledged_by TEXT,
        UNIQUE(session_id, allowance_minutes, level))""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_timer_alerts_inbox
        ON hq_timer_alerts(acknowledged_at, observed_at)""")

def scan():
    """Run once from an owner action or a trusted local worker. Safe to repeat."""
    with connect() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        checked = timer.now()
        rows = db.execute("""SELECT t.id, t.work_id, t.allowance_minutes,
            t.active_seconds, t.state, t.changed_at,
            (SELECT COALESCE(SUM(e.minutes),0) FROM hq_work_time_entries e
             WHERE e.work_id=t.work_id AND e.voided_at IS NULL) AS saved_minutes
            FROM hq_timer_sessions t JOIN hq_work_records w ON w.id=t.work_id
            WHERE t.state IN ('running','paused','review') AND t.allowance_minutes > 0
              AND w.kind='job' AND w.status IN ('approved','in_progress')""").fetchall()
        created = 0
        for row in rows:
            if checked < row["changed_at"]:
                continue
            elapsed = checked - row["changed_at"] if row["state"] == "running" else 0
            total = row["saved_minutes"] * 60 + row["active_seconds"] + elapsed
            budget = row["allowance_minutes"] * 60
            if total * 5 < budget * 4:
                continue
            level = "limit" if total >= budget else "near"
            if level == "near" and db.execute("""SELECT 1 FROM hq_timer_alerts
                WHERE session_id=? AND allowance_minutes=? AND level='limit'""",
                (row["id"], row["allowance_minutes"])).fetchone():
                continue
            result = db.execute("""INSERT INTO hq_timer_alerts
                (id,session_id,work_id,allowance_minutes,level,observed_seconds,observed_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_id,allowance_minutes,level) DO NOTHING""",
                (uuid.uuid4().hex,row["id"],row["work_id"],row["allowance_minutes"],level,total,checked))
            created += result.rowcount
    return created

def inbox():
    with connect() as db:
        _tables(db)
        db.execute("BEGIN")
        base = """SELECT a.*, datetime(a.observed_at,'unixepoch') AS observed_utc,
            datetime(a.acknowledged_at,'unixepoch') AS acknowledged_utc,
            w.service, c.name, v.registration,
            CASE WHEN t.state IN ('running','paused','review')
                 AND t.allowance_minutes=a.allowance_minutes THEN 1 ELSE 0 END AS same_open_allowance
            FROM hq_timer_alerts a JOIN hq_work_records w ON w.id=a.work_id
            JOIN hq_customers c ON c.id=w.customer_id JOIN hq_vehicles v ON v.id=w.vehicle_id
            LEFT JOIN hq_timer_sessions t ON t.id=a.session_id"""
        pending = [dict(r) for r in db.execute(base + """ WHERE a.acknowledged_at IS NULL
            ORDER BY a.observed_at DESC,a.id DESC LIMIT 51""")]
        recent = [dict(r) for r in db.execute(base + """ WHERE a.acknowledged_at IS NOT NULL
            ORDER BY a.acknowledged_at DESC,a.id DESC LIMIT 20""")]
    return {"pending":pending[:50], "has_more":len(pending)>50, "recent":recent}

def acknowledge(alert_id, actor):
    timer._validate_id(alert_id)
    if not isinstance(actor,str) or not actor or len(actor)>256:
        raise ValueError("Owner identity required")
    with connect() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT acknowledged_at FROM hq_timer_alerts WHERE id=?", (alert_id,)).fetchone()
        if row is None:
            raise LookupError("Alert not found")
        if row["acknowledged_at"] is None:
            db.execute("""UPDATE hq_timer_alerts SET acknowledged_at=?,acknowledged_by=?
                WHERE id=?""", (timer.now(),actor,alert_id))
