"""Durable opt-in preview queue. Deliberately no transport or network calls."""
from customer_request_store import connect
import hq_timer_alerts as alerts
import hq_timer_push as push
import hq_work_timer as timer

MAX_AGE = 900

def _tables(db):
    alerts._tables(db)
    push._tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_timer_push_consent (
        device_id TEXT PRIMARY KEY, device_revision INTEGER NOT NULL,
        revision INTEGER NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        after_row INTEGER NOT NULL, changed_at INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_timer_push_consent_events (
        device_id TEXT NOT NULL, revision INTEGER NOT NULL,
        device_revision INTEGER NOT NULL, enabled INTEGER NOT NULL,
        actor TEXT NOT NULL, changed_at INTEGER NOT NULL,
        PRIMARY KEY(device_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_timer_push_preview (
        device_id TEXT NOT NULL, device_revision INTEGER NOT NULL,
        consent_revision INTEGER NOT NULL, alert_id TEXT NOT NULL,
        queued_at INTEGER NOT NULL, cancelled_at INTEGER,
        PRIMARY KEY(device_id,device_revision,alert_id))""")

def _cancel(db, checked):
    db.execute("""UPDATE hq_timer_push_preview AS p SET cancelled_at=?
        WHERE cancelled_at IS NULL AND NOT EXISTS (
          SELECT 1 FROM hq_timer_push_devices d
          JOIN hq_timer_push_consent c ON c.device_id=d.id
          JOIN hq_timer_alerts a ON a.id=p.alert_id
          JOIN hq_timer_sessions t ON t.id=a.session_id
          JOIN hq_work_records w ON w.id=a.work_id
          WHERE d.id=p.device_id AND d.revoked_at IS NULL
          AND d.revision=p.device_revision AND d.application_key=?
          AND c.enabled=1 AND c.device_revision=d.revision AND c.revision=p.consent_revision
          AND a.acknowledged_at IS NULL AND a.observed_at BETWEEN ? AND ?
          AND t.allowance_minutes=a.allowance_minutes
          AND t.state IN ('running','paused','review')
          AND w.kind='job' AND w.status IN ('approved','in_progress')
        )""", (checked, push.public_key(), checked-MAX_AGE, checked))

def set_consent(actor, device_id, device_revision, revision, enabled):
    actor = push._actor(actor)
    if type(enabled) is not bool:
        raise ValueError("Explicit choice required")
    with connect() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        d = db.execute("""SELECT * FROM hq_timer_push_devices WHERE id=?
            AND owner_sub=? AND revoked_at IS NULL""", (device_id,actor)).fetchone()
        if not d or d["revision"] != device_revision:
            raise ValueError("Device changed; refresh")
        if enabled and (not push.public_key() or d["application_key"] != push.public_key()):
            raise ValueError("Current application key required")
        old = db.execute("SELECT * FROM hq_timer_push_consent WHERE device_id=?", (device_id,)).fetchone()
        current = old["revision"] if old else 0
        if current != revision:
            raise ValueError("Consent changed; refresh")
        if old and old["device_revision"] == device_revision and old["enabled"] == int(enabled):
            return
        checked = timer.now()
        watermark = db.execute("SELECT COALESCE(MAX(rowid),0) FROM hq_timer_alerts").fetchone()[0]
        db.execute("""INSERT INTO hq_timer_push_consent VALUES (?,?,?,?,?,?)
          ON CONFLICT(device_id) DO UPDATE SET device_revision=excluded.device_revision,
          revision=excluded.revision,enabled=excluded.enabled,after_row=excluded.after_row,
          changed_at=excluded.changed_at""", (device_id,device_revision,current+1,int(enabled),watermark,checked))
        db.execute("INSERT INTO hq_timer_push_consent_events VALUES (?,?,?,?,?,?)",
                   (device_id,current+1,device_revision,int(enabled),actor,checked))
        _cancel(db,checked)

def refresh():
    """Record eligible new observations only. No notification can be sent."""
    with connect() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        checked = timer.now()
        _cancel(db,checked)
        key = push.public_key()
        if not key:
            return 0
        return db.execute("""INSERT OR IGNORE INTO hq_timer_push_preview
            (device_id,device_revision,consent_revision,alert_id,queued_at)
            SELECT d.id,d.revision,c.revision,a.id,?
            FROM hq_timer_push_consent c JOIN hq_timer_push_devices d ON d.id=c.device_id
            JOIN hq_timer_alerts a ON a.rowid>c.after_row
            JOIN hq_timer_sessions t ON t.id=a.session_id
            JOIN hq_work_records w ON w.id=a.work_id
            WHERE c.enabled=1 AND d.revoked_at IS NULL AND c.device_revision=d.revision
            AND d.application_key=? AND a.acknowledged_at IS NULL
            AND a.observed_at BETWEEN ? AND ? AND a.observed_at>=c.changed_at
            AND t.allowance_minutes=a.allowance_minutes AND t.state IN ('running','paused','review')
            AND w.kind='job' AND w.status IN ('approved','in_progress')""",
            (checked,key,checked-MAX_AGE,checked)).rowcount

def summary(actor):
    actor = push._actor(actor)
    with connect() as db:
        _tables(db)
        return {r["id"]:dict(r) for r in db.execute("""SELECT d.id,
            COALESCE(c.revision,0) AS consent_revision,
            CASE WHEN c.device_revision=d.revision AND c.enabled=1 THEN 1 ELSE 0 END AS enabled,
            (SELECT COUNT(*) FROM hq_timer_push_preview p WHERE p.device_id=d.id
              AND p.device_revision=d.revision AND p.consent_revision=c.revision
              AND p.cancelled_at IS NULL) AS preview_count
            FROM hq_timer_push_devices d LEFT JOIN hq_timer_push_consent c ON c.device_id=d.id
            WHERE d.owner_sub=? AND d.revoked_at IS NULL""", (actor,))}
