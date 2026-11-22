"""Audited, owner-approved linkage of completed native HQ maintenance visits to plans.

This is an entitlement ledger, not a payment, invoice, booking or historic import.
Only explicitly linked, completed HQ visits consume a plan allowance. Unlinking
preserves revisions and audit events. No external system is ever written.
"""
import hashlib
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from customer_request_store import connect, storage_ready
from hq_core_records import _schema

REF = re.compile(r"[0-9a-f]{32}\Z")


def _ref(value):
    if not isinstance(value, str) or not REF.fullmatch(value):
        raise ValueError("Invalid HQ reference")
    return value


def _revision(value):
    if type(value) is not int or not 0 <= value < 2147483647:
        raise ValueError("Invalid revision; reload the record")
    return value


def _now():
    return datetime.now(timezone.utc).isoformat()


def _tables(db):
    # Lazy imports avoid a dependency cycle between existing route blueprints.
    from hq_maintenance_packages import _tables as plans_tables
    from hq_native_maintenance import _tables as visits_tables
    plans_tables(db)
    visits_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_maintenance_plan_visit_links (
        visit_id TEXT PRIMARY KEY REFERENCES hq_maintenance_visits(id),
        plan_id TEXT NOT NULL REFERENCES hq_maintenance_plans(id),
        active INTEGER NOT NULL CHECK(active IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_maintenance_plan_links_by_plan
        ON hq_maintenance_plan_visit_links(plan_id,active)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_maintenance_plan_visit_audit (
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE,
        visit_id TEXT NOT NULL REFERENCES hq_maintenance_visits(id),
        revision INTEGER NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('link','unlink')),
        prior_plan_id TEXT,
        plan_id TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(visit_id,revision)
    )""")


def _count(db, plan_id):
    return db.execute("""SELECT COUNT(*) FROM hq_maintenance_plan_visit_links l
        JOIN hq_maintenance_visits v ON v.id=l.visit_id
        WHERE l.plan_id=? AND l.active=1 AND v.status='completed'""",
        (plan_id,)).fetchone()[0]


def usage(db, plan_id):
    """Source-specific counts: historical Sheets visit baselines are excluded."""
    _ref(plan_id)
    _tables(db)
    plan = db.execute("SELECT visits_included FROM hq_maintenance_plans WHERE id=?",
                      (plan_id,)).fetchone()
    if plan is None:
        raise LookupError("Plan not found")
    used = _count(db, plan_id)
    included = plan["visits_included"]
    return {"used": used, "included": included,
            "remaining": max(0, included - used) if included is not None else None}


def active_link(db, visit_id):
    _ref(visit_id)
    _tables(db)
    row = db.execute("""SELECT * FROM hq_maintenance_plan_visit_links
        WHERE visit_id=? AND active=1""", (visit_id,)).fetchone()
    return dict(row) if row else None


def plan_links(db, plan_id):
    _ref(plan_id)
    _tables(db)
    return [dict(row) for row in db.execute("""SELECT l.visit_id,l.revision,
        v.visit_day,v.visit_time,v.service,v.status,v.archived
        FROM hq_maintenance_plan_visit_links l
        JOIN hq_maintenance_visits v ON v.id=l.visit_id
        WHERE l.plan_id=? AND l.active=1
        ORDER BY v.visit_day DESC,v.id DESC LIMIT 1000""", (plan_id,))]


def candidates(db, plan_id):
    _ref(plan_id)
    _tables(db)
    p = db.execute("SELECT * FROM hq_maintenance_plans WHERE id=?", (plan_id,)).fetchone()
    if not p:
        raise LookupError("Plan not found")
    return [dict(row) for row in db.execute("""SELECT v.id,v.visit_day,v.visit_time,v.service
        FROM hq_maintenance_visits v
        LEFT JOIN hq_maintenance_plan_visit_links l ON l.visit_id=v.id
        WHERE v.customer_id=? AND v.vehicle_id=? AND v.service=? COLLATE NOCASE
          AND v.status='completed' AND v.archived=0 AND (l.visit_id IS NULL OR l.active=0)
        ORDER BY v.visit_day DESC,v.id DESC LIMIT 150""",
        (p["customer_id"], p["vehicle_id"], p["service"]))]


def history(db, plan_id):
    _ref(plan_id)
    _tables(db)
    return [dict(row) for row in db.execute("""SELECT action,visit_id,revision,created_at
        FROM hq_maintenance_plan_visit_audit WHERE plan_id=?
        ORDER BY created_at DESC,id DESC LIMIT 100""", (plan_id,))]


def change_link(plan_id, visit_id, *, action, operation_id, plan_revision,
                link_revision=None, actor="owner"):
    """Atomic, retry-safe owner decision. Never credits receipts or creates bookings."""
    if not storage_ready():
        raise ValueError("Persistent HQ storage unavailable")
    _ref(plan_id)
    _ref(visit_id)
    _ref(operation_id)
    _revision(plan_revision)
    if action not in ("link", "unlink") or not isinstance(actor, str) or not actor:
        raise ValueError("Invalid owner entitlement action")
    if action == "unlink":
        _revision(link_revision)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        retry = db.execute("""SELECT * FROM hq_maintenance_plan_visit_audit
            WHERE operation_id=?""", (operation_id,)).fetchone()
        if retry:
            if (retry["action"], retry["plan_id"], retry["visit_id"]) != (action, plan_id, visit_id):
                raise ValueError("Submission reference already used for another action")
            return retry["revision"]
        plan = db.execute("SELECT * FROM hq_maintenance_plans WHERE id=?", (plan_id,)).fetchone()
        visit = db.execute("SELECT * FROM hq_maintenance_visits WHERE id=?", (visit_id,)).fetchone()
        if plan is None or visit is None:
            raise LookupError("Maintenance plan or visit not found")
        if plan["revision"] != plan_revision:
            raise ValueError("Plan changed; reload before linking visits")
        current = db.execute("""SELECT * FROM hq_maintenance_plan_visit_links
            WHERE visit_id=?""", (visit_id,)).fetchone()
        if action == "link":
            if current and current["active"]:
                if current["plan_id"] == plan_id:
                    return current["revision"]
                raise ValueError("Visit is already linked to another package")
            if plan["archived"] or plan["status"] == "ended":
                raise ValueError("Restore or reactivate the plan before adding visits")
            if visit["archived"] or visit["status"] != "completed":
                raise ValueError("Only completed, unarchived visits can be linked")
            if (plan["customer_id"] != visit["customer_id"] or
                    plan["vehicle_id"] != visit["vehicle_id"] or
                    plan["service"].casefold() != visit["service"].casefold()):
                raise ValueError("Visit must match the plan's HQ customer, vehicle and service")
            included = plan["visits_included"]
            if included is not None and _count(db, plan_id) >= included:
                raise ValueError("Included visit allowance exhausted; review the plan first")
            revision = current["revision"] + 1 if current else 0
            if current:
                db.execute("""UPDATE hq_maintenance_plan_visit_links SET
                    plan_id=?,active=1,revision=?,updated_at=? WHERE visit_id=? AND revision=?""",
                    (plan_id, revision, _now(), visit_id, current["revision"]))
            else:
                db.execute("""INSERT INTO hq_maintenance_plan_visit_links
                    (visit_id,plan_id,active,revision,created_at,updated_at)
                    VALUES (?,?,1,0,?,?)""", (visit_id, plan_id, _now(), _now()))
            prior_plan = current["plan_id"] if current else None
        else:
            if not current or not current["active"] or current["plan_id"] != plan_id:
                raise ValueError("This visit is not linked to the selected plan")
            if current["revision"] != link_revision:
                raise ValueError("Link changed; reload before unlinking")
            revision = current["revision"] + 1
            db.execute("""UPDATE hq_maintenance_plan_visit_links
                SET active=0,revision=?,updated_at=? WHERE visit_id=? AND revision=?""",
                (revision, _now(), visit_id, current["revision"]))
            prior_plan = plan_id
        db.execute("""INSERT INTO hq_maintenance_plan_visit_audit
            (id,operation_id,visit_id,revision,action,prior_plan_id,plan_id,actor_fingerprint,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, operation_id, visit_id, revision, action, prior_plan,
             plan_id, hashlib.sha256(actor.encode("utf-8")).hexdigest(), _now()))
        return revision
