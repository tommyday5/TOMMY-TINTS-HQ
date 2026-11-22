"""Canonical HQ maintenance-visit ledger; never books or records cash receipts.

Only owner-authenticated POSTs mutate the shared durable HQ SQLite database.
Google Sheets and external calendars remain read-only and untouched.
"""
import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, current_app, redirect, render_template, request, session
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_core_records import _schema
from hq_customer_portal import active_customer

native_maintenance = Blueprint("native_maintenance", __name__)
HEX = re.compile(r"[0-9a-f]{32}\Z")
STATES = ("planned", "in_progress", "completed", "cancelled")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_maintenance_visits (
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        vehicle_id TEXT NOT NULL REFERENCES hq_vehicles(id),
        service TEXT NOT NULL,
        visit_day TEXT NOT NULL,
        visit_time TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('planned','in_progress','completed','cancelled')),
        planned_price_pence INTEGER CHECK(planned_price_pence IS NULL OR planned_price_pence>=0),
        actual_minutes INTEGER CHECK(actual_minutes IS NULL OR actual_minutes>=0),
        internal_notes TEXT NOT NULL,
        portal_visible INTEGER NOT NULL DEFAULT 0 CHECK(portal_visible IN (0,1)),
        archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_maintenance_active_slot
        ON hq_maintenance_visits(customer_id,vehicle_id,visit_day,visit_time)
        WHERE archived=0 AND status!='cancelled'""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_maintenance_visit_audit (
        id TEXT PRIMARY KEY,
        visit_id TEXT NOT NULL REFERENCES hq_maintenance_visits(id),
        revision INTEGER NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('create','edit','archive','restore')),
        before_json TEXT NOT NULL,
        after_json TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(visit_id,revision)
    )""")


def _reference(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError("Invalid canonical HQ reference")
    return value


def _details(form):
    if not isinstance(form, dict):
        raise ValueError("Invalid visit")
    customer_id = _reference(form.get("customer_id"))
    vehicle_id = _reference(form.get("vehicle_id"))
    service = str(form.get("service", "")).strip()
    notes = str(form.get("internal_notes", "")).strip()
    if not 1 <= len(service) <= 120 or len(notes) > 1200:
        raise ValueError("Enter a service (up to 120 characters) and notes up to 1200 characters")
    day = str(form.get("visit_day", ""))
    clock = str(form.get("visit_time", ""))
    try:
        parsed_day = date.fromisoformat(day)
        parsed_clock = time.fromisoformat(clock)
    except (TypeError, ValueError):
        raise ValueError("Enter a valid visit date and time")
    if (parsed_day.year < 2015 or parsed_day.year > 2100 or
            not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", clock) or
            parsed_clock.hour > 23):
        raise ValueError("Invalid visit date or time")
    status = form.get("status")
    if status not in STATES:
        raise ValueError("Choose a valid maintenance stage")
    raw_price = str(form.get("planned_price", "")).strip()
    if raw_price:
        try:
            money = Decimal(raw_price)
            if not money.is_finite() or money != money.quantize(Decimal("0.01")):
                raise ValueError()
            pence = int(money * 100)
        except (InvalidOperation, ValueError, OverflowError):
            raise ValueError("Planned price must be pounds and pence")
        if not 0 <= pence <= 10000000:
            raise ValueError("Planned price is outside permitted range")
    else:
        pence = None
    raw_minutes = str(form.get("actual_minutes", "")).strip()
    if raw_minutes and not re.fullmatch(r"(?:0|[1-9][0-9]{0,3})", raw_minutes):
        raise ValueError("Actual minutes must be a non-negative whole number")
    minutes = int(raw_minutes) if raw_minutes else None
    if minutes is not None and minutes > 1440:
        raise ValueError("Actual minutes cannot exceed 24 hours")
    visible = 1 if form.get("portal_visible") in (True, "1", "on") else 0
    return dict(customer_id=customer_id, vehicle_id=vehicle_id, service=service,
                visit_day=day, visit_time=clock, status=status,
                planned_price_pence=pence, actual_minutes=minutes,
                internal_notes=notes, portal_visible=visible)


def _record(row):
    return {key: row[key] for key in (
        "customer_id", "vehicle_id", "service", "visit_day", "visit_time",
        "status", "planned_price_pence", "actual_minutes", "internal_notes",
        "portal_visible", "archived")}


def _audit(db, visit_id, revision, action, before, after, actor):
    # Audit is private; never displayed in the customer-facing portal.
    db.execute("INSERT INTO hq_maintenance_visit_audit VALUES (?,?,?,?,?,?,?,?)", (
        uuid.uuid4().hex, visit_id, revision, action,
        json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True),
        hashlib.sha256(actor.encode()).hexdigest(), _now()))


def save_visit(form, *, visit_id=None, expected_revision=None, actor="owner"):
    """Create or edit a visit with one transaction and optimistic concurrency.

    No payments, actual receipts, calendars, or existing work/jobs are affected.
    """
    if not storage_ready():
        raise ValueError("Persistent HQ storage is unavailable")
    values = _details(form)
    if not isinstance(actor, str) or not actor:
        raise ValueError("Owner identity required")
    is_new = visit_id is None
    if is_new:
        op = _reference(form.get("operation_id"))
    else:
        _reference(visit_id)
        if type(expected_revision) is not int or not 0 <= expected_revision < 2147483647:
            raise ValueError("Invalid visit revision")
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        if is_new:
            existing = db.execute("SELECT * FROM hq_maintenance_visits WHERE operation_id=?", (op,)).fetchone()
            if existing:
                if _record(existing) != {**values, "archived": 0}:
                    raise ValueError("Submission reference already used for different details")
                return existing["id"]
        customer = db.execute("SELECT id FROM hq_customers WHERE id=?", (values["customer_id"],)).fetchone()
        vehicle = db.execute("SELECT id FROM hq_vehicles WHERE id=? AND customer_id=?",
                             (values["vehicle_id"], values["customer_id"])).fetchone()
        if not customer or not vehicle:
            raise ValueError("Customer and vehicle must belong to the same native HQ record")
        now = _now()
        try:
            if is_new:
                visit_id = uuid.uuid4().hex
                after = {**values, "archived": 0}
                db.execute("""INSERT INTO hq_maintenance_visits
                    (id,operation_id,customer_id,vehicle_id,service,visit_day,
                    visit_time,status,planned_price_pence,actual_minutes,
                    internal_notes,portal_visible,archived,revision,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?)""",
                    (visit_id, op, values["customer_id"], values["vehicle_id"],
                     values["service"], values["visit_day"], values["visit_time"],
                     values["status"], values["planned_price_pence"],
                     values["actual_minutes"], values["internal_notes"],
                     values["portal_visible"], now, now))
                _audit(db, visit_id, 0, "create", {}, after, actor)
            else:
                previous = db.execute("SELECT * FROM hq_maintenance_visits WHERE id=?", (visit_id,)).fetchone()
                if not previous:
                    raise LookupError("Maintenance visit not found")
                if previous["revision"] != expected_revision:
                    raise ValueError("Visit changed; reload before saving")
                if previous["archived"]:
                    raise ValueError("Restore the archived visit before editing")
                before = _record(previous)
                after = {**values, "archived": 0}
                if before == after:
                    return visit_id
                # A linked visit must remain the same completed service for the
                # same customer/vehicle, or its allowance accounting would drift.
                from hq_maintenance_entitlements import active_link
                if active_link(db, visit_id) and (
                    any(before[key] != after[key] for key in
                        ("customer_id", "vehicle_id", "service")) or
                    after["status"] != "completed"):
                    raise ValueError("Unlink the maintenance plan before changing a completed linked visit")
                db.execute("""UPDATE hq_maintenance_visits SET customer_id=?,vehicle_id=?,
                    service=?,visit_day=?,visit_time=?,status=?,planned_price_pence=?,
                    actual_minutes=?,internal_notes=?,portal_visible=?,revision=revision+1,
                    updated_at=? WHERE id=? AND revision=? AND archived=0""",
                    (values["customer_id"], values["vehicle_id"], values["service"],
                     values["visit_day"], values["visit_time"], values["status"],
                     values["planned_price_pence"], values["actual_minutes"],
                     values["internal_notes"], values["portal_visible"], now,
                     visit_id, expected_revision))
                _audit(db, visit_id, expected_revision + 1, "edit", before, after, actor)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Another active maintenance visit already uses this vehicle and time") from exc
    return visit_id


def archive_visit(visit_id, expected_revision, *, archive, actor="owner"):
    _reference(visit_id)
    if type(expected_revision) is not int or expected_revision < 0 or type(archive) is not bool:
        raise ValueError("Invalid archive revision or action")
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM hq_maintenance_visits WHERE id=?", (visit_id,)).fetchone()
        if row is None:
            raise LookupError("Maintenance visit not found")
        if row["revision"] != expected_revision:
            raise ValueError("Visit changed; reload")
        if bool(row["archived"]) == archive:
            return
        before = _record(row)
        after = {**before, "archived": int(archive)}
        try:
            db.execute("UPDATE hq_maintenance_visits SET archived=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                       (int(archive), _now(), visit_id, expected_revision))
            _audit(db, visit_id, expected_revision + 1,
                   "archive" if archive else "restore", before, after, actor)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Restore conflicts with another active visit") from exc


def visit_snapshot(visit_id):
    _reference(visit_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        row = db.execute("SELECT * FROM hq_maintenance_visits WHERE id=?", (visit_id,)).fetchone()
        if row is None:
            raise LookupError("Visit not found")
        return dict(row)


def visit_index(*, customer_id="", show_archived=False):
    with connect() as db:
        _schema(db)
        _tables(db)
        sql = """SELECT m.*,c.name AS customer_name,v.make,v.model,v.registration
            FROM hq_maintenance_visits m
            JOIN hq_customers c ON c.id=m.customer_id
            JOIN hq_vehicles v ON v.id=m.vehicle_id AND v.customer_id=m.customer_id
            WHERE m.archived=?"""
        args = [int(show_archived)]
        if customer_id:
            sql += " AND m.customer_id=?"
            args.append(_reference(customer_id))
        sql += " ORDER BY m.visit_day DESC,m.visit_time DESC,m.id DESC LIMIT 150"
        return [dict(row) for row in db.execute(sql, args)]


def visit_history(visit_id):
    """Private action history (details remain in owner-only database)."""
    _reference(visit_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        return [dict(row) for row in db.execute("""SELECT revision,action,created_at
            FROM hq_maintenance_visit_audit WHERE visit_id=?
            ORDER BY revision DESC LIMIT 100""", (visit_id,))]


def _options(query="", selected=""):
    if len(query) > 80:
        raise ValueError("Search is too long")
    with connect() as db:
        _schema(db)
        if selected:
            _reference(selected)
        search = "%" + query.strip() + "%"
        customers = [dict(r) for r in db.execute("""SELECT id,name FROM hq_customers
            WHERE name LIKE ? OR id=? ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END, name COLLATE NOCASE LIMIT 100""",
            (search, selected, selected))]
        vehicles = [dict(r) for r in db.execute("""SELECT id,customer_id,make,model,registration
            FROM hq_vehicles WHERE customer_id IN
            (SELECT id FROM hq_customers WHERE name LIKE ? OR id=?)
            ORDER BY CASE WHEN customer_id=? THEN 0 ELSE 1 END,make,model LIMIT 300""", (search, selected, selected))]
    return customers, vehicles


@native_maintenance.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _owner_csrf():
    if request.content_length is None or request.content_length > 5000:
        abort(413)
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
        abort(403)


@native_maintenance.get("/hq/maintenance/visits")
@required
def owner_index():
    if not storage_ready():
        abort(503)
    try:
        return render_template("hq_native_maintenance.html", listing=True,
                               visits=visit_index(show_archived=request.args.get("archived") == "1"))
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Maintenance visits unavailable")
        abort(503)


@native_maintenance.route("/hq/maintenance/visits/new", methods=["GET", "POST"])
@required
def owner_new():
    if not storage_ready():
        abort(503)
    if request.method == "POST":
        _owner_csrf()
    values = request.form.to_dict() if request.method == "POST" else {}
    selected = values.get("customer_id", request.args.get("customer_id", ""))
    query = request.args.get("q", "")[:81]
    try:
        if request.method == "POST":
            ref = save_visit(values, actor=session.get("google_sub", "owner"))
            return redirect("/hq/maintenance/visits/" + ref + "?saved=1", code=303)
        customers, vehicles = _options(query, selected)
        return render_template("hq_native_maintenance.html", listing=False, new=True,
                               record=None, values=values, customers=customers, vehicles=vehicles,
                               csrf=session["csrf"], operation_id=uuid.uuid4().hex,
                               query=query, error=None, states=STATES)
    except (ValueError, LookupError) as exc:
        customers, vehicles = _options("", selected if HEX.fullmatch(selected) else "")
        return render_template("hq_native_maintenance.html", listing=False, new=True,
                               record=None, values=values, customers=customers, vehicles=vehicles,
                               csrf=session["csrf"], operation_id=values.get("operation_id", uuid.uuid4().hex),
                               query=query, error=str(exc), states=STATES), 400
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Maintenance visit unavailable")
        abort(503)


@native_maintenance.route("/hq/maintenance/visits/<visit_id>", methods=["GET", "POST"])
@required
def owner_edit(visit_id):
    if not storage_ready():
        abort(503)
    try:
        record = visit_snapshot(visit_id)
        if request.method == "POST":
            _owner_csrf()
            raw = request.form.get("revision", "")
            if not re.fullmatch(r"(?:0|[1-9][0-9]{0,9})", raw):
                abort(400)
            revision = int(raw)
            action = request.form.get("action", "save")
            if action in ("archive", "restore"):
                archive_visit(visit_id, revision, archive=action == "archive",
                              actor=session.get("google_sub", "owner"))
            elif action == "save":
                save_visit(request.form.to_dict(), visit_id=visit_id,
                           expected_revision=revision, actor=session.get("google_sub", "owner"))
            else:
                abort(400)
            return redirect("/hq/maintenance/visits/" + visit_id + "?saved=1", code=303)
        customers, vehicles = _options("", record["customer_id"])
        return render_template("hq_native_maintenance.html", listing=False, new=False,
                               record=record, values=record, customers=customers, vehicles=vehicles,
                               csrf=session["csrf"], states=STATES, error=None,
                               events=visit_history(visit_id))
    except (ValueError, LookupError) as exc:
        if request.method == "GET":
            abort(404)
        record = visit_snapshot(visit_id)
        customers, vehicles = _options("", record["customer_id"])
        return render_template("hq_native_maintenance.html", listing=False, new=False,
                               record=record, values=request.form.to_dict(),
                               customers=customers, vehicles=vehicles, csrf=session["csrf"],
                               states=STATES, error=str(exc), events=visit_history(visit_id)), 409
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Maintenance visit unavailable")
        abort(503)


@native_maintenance.get("/customer/maintenance")
def customer_index():
    customer_id = active_customer()
    if not customer_id:
        return redirect("/customer/login")
    try:
        with connect() as db:
            _schema(db)
            _tables(db)
            rows = [dict(row) for row in db.execute("""SELECT m.id,m.service,m.visit_day,
                m.visit_time,m.status,v.make,v.model,v.registration
                FROM hq_maintenance_visits m JOIN hq_vehicles v
                ON v.id=m.vehicle_id AND v.customer_id=m.customer_id
                WHERE m.customer_id=? AND m.portal_visible=1 AND m.archived=0
                ORDER BY m.visit_day DESC,m.id DESC LIMIT 100""", (customer_id,))]
        return render_template("customer_maintenance.html", visits=rows)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Customer maintenance unavailable")
        abort(503)
