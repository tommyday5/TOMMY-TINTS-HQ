"""Native, owner-editable maintenance plans. No historic Sheets import or bookings.

Plans describe agreed service cadence and indicative next-due dates. A planned
price is not an invoice or a verified receipt. Calendar/Square stay untouched.
"""
import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, current_app, redirect, render_template, request, session
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_core_records import _schema
from hq_customer_portal import active_customer
from hq_native_maintenance import _options

maintenance_plans = Blueprint("maintenance_plans", __name__)
HEX = re.compile(r"[0-9a-f]{32}\Z")
FREQUENCIES = ("weekly", "fortnightly", "four_weekly", "monthly", "on_request")
STATUSES = ("active", "paused", "ended")
FIELDS = ("customer_id", "vehicle_id", "plan_name", "service", "frequency",
          "next_due", "status", "visit_price_pence", "visits_included",
          "internal_notes", "portal_visible", "archived")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ref(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError("Invalid canonical HQ reference")
    return value


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_maintenance_plans (
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        vehicle_id TEXT NOT NULL REFERENCES hq_vehicles(id),
        plan_name TEXT NOT NULL,
        service TEXT NOT NULL,
        frequency TEXT NOT NULL CHECK(frequency IN
            ('weekly','fortnightly','four_weekly','monthly','on_request')),
        next_due TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','paused','ended')),
        visit_price_pence INTEGER CHECK(visit_price_pence IS NULL OR visit_price_pence>=0),
        visits_included INTEGER CHECK(visits_included IS NULL OR visits_included BETWEEN 1 AND 1000),
        internal_notes TEXT NOT NULL,
        portal_visible INTEGER NOT NULL DEFAULT 0 CHECK(portal_visible IN (0,1)),
        archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_maintenance_plan_active_identity
        ON hq_maintenance_plans(customer_id,vehicle_id,service COLLATE NOCASE)
        WHERE archived=0 AND status!='ended'""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_maintenance_plan_audit (
        id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL REFERENCES hq_maintenance_plans(id),
        revision INTEGER NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('create','edit','archive','restore')),
        before_json TEXT NOT NULL,
        after_json TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(plan_id,revision)
    )""")


def _details(form):
    if not isinstance(form, dict):
        raise ValueError("Invalid plan")
    customer_id = _ref(form.get("customer_id"))
    vehicle_id = _ref(form.get("vehicle_id"))
    name = str(form.get("plan_name", "")).strip()
    service = str(form.get("service", "")).strip()
    notes = str(form.get("internal_notes", "")).strip()
    if not 1 <= len(name) <= 100 or not 1 <= len(service) <= 120:
        raise ValueError("Enter a plan name and maintenance service")
    if len(notes) > 1200:
        raise ValueError("Private notes must be at most 1200 characters")
    frequency = form.get("frequency")
    status = form.get("status")
    if frequency not in FREQUENCIES or status not in STATUSES:
        raise ValueError("Select a valid frequency and status")
    due = str(form.get("next_due", "")).strip()
    if due:
        try:
            parsed = date.fromisoformat(due)
        except (TypeError, ValueError):
            raise ValueError("Next due must be a valid calendar date")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) or not 2015 <= parsed.year <= 2100:
            raise ValueError("Next due must be a valid ISO date")
    raw_price = str(form.get("visit_price", "")).strip()
    if raw_price:
        try:
            amount = Decimal(raw_price)
            if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
                raise ValueError()
            pence = int(amount * 100)
        except (InvalidOperation, ValueError, OverflowError):
            raise ValueError("Enter a valid pounds-and-pence price")
        if not 0 <= pence <= 10000000:
            raise ValueError("Visit price is outside the permitted range")
    else:
        pence = None
    raw_count = str(form.get("visits_included", "")).strip()
    if raw_count and not re.fullmatch(r"[1-9][0-9]{0,3}", raw_count):
        raise ValueError("Included visits must be a positive whole number")
    visits_included = int(raw_count) if raw_count else None
    if visits_included is not None and visits_included > 1000:
        raise ValueError("Included visits cannot exceed 1000")
    return dict(customer_id=customer_id, vehicle_id=vehicle_id, plan_name=name,
                service=service, frequency=frequency, next_due=due, status=status,
                visit_price_pence=pence, visits_included=visits_included,
                internal_notes=notes,
                portal_visible=int(form.get("portal_visible") in (True, "1", "on")))


def _record(row):
    return {key: row[key] for key in FIELDS}


def _audit(db, plan_id, revision, action, before, after, actor):
    db.execute("INSERT INTO hq_maintenance_plan_audit VALUES(?,?,?,?,?,?,?,?)",
               (uuid.uuid4().hex, plan_id, revision, action,
                json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True),
                hashlib.sha256(actor.encode("utf-8")).hexdigest(), _now()))


def save_plan(form, *, plan_id=None, expected_revision=None, actor="owner"):
    """Atomic plan create/edit with optimistic revision and retry-safe creation."""
    if not storage_ready():
        raise ValueError("Persistent HQ storage is unavailable")
    if not isinstance(actor, str) or not actor:
        raise ValueError("Owner identity is required")
    values = _details(form)
    is_new = plan_id is None
    if is_new:
        operation_id = _ref(form.get("operation_id"))
    else:
        _ref(plan_id)
        if type(expected_revision) is not int or not 0 <= expected_revision < 2147483647:
            raise ValueError("Invalid plan revision")
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        if is_new:
            previous = db.execute("SELECT * FROM hq_maintenance_plans WHERE operation_id=?",
                                  (operation_id,)).fetchone()
            if previous:
                if _record(previous) != {**values, "archived": 0}:
                    raise ValueError("This submission reference has different details")
                return previous["id"]
        if not db.execute("SELECT id FROM hq_customers WHERE id=?",
                          (values["customer_id"],)).fetchone():
            raise ValueError("The HQ customer does not exist")
        if not db.execute("SELECT id FROM hq_vehicles WHERE id=? AND customer_id=?",
                          (values["vehicle_id"], values["customer_id"])).fetchone():
            raise ValueError("The vehicle does not belong to this HQ customer")
        now = _now()
        try:
            if is_new:
                plan_id = uuid.uuid4().hex
                db.execute("""INSERT INTO hq_maintenance_plans
                    (id,operation_id,customer_id,vehicle_id,plan_name,service,
                     frequency,next_due,status,visit_price_pence,visits_included,
                     internal_notes,portal_visible,archived,revision,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?)""",
                    (plan_id, operation_id, values["customer_id"], values["vehicle_id"],
                     values["plan_name"], values["service"], values["frequency"],
                     values["next_due"], values["status"], values["visit_price_pence"],
                     values["visits_included"], values["internal_notes"],
                     values["portal_visible"], now, now))
                _audit(db, plan_id, 0, "create", {}, {**values, "archived": 0}, actor)
            else:
                prior = db.execute("SELECT * FROM hq_maintenance_plans WHERE id=?",
                                   (plan_id,)).fetchone()
                if prior is None:
                    raise LookupError("Maintenance plan not found")
                if prior["revision"] != expected_revision:
                    raise ValueError("Plan changed; reload before saving")
                if prior["archived"]:
                    raise ValueError("Restore the plan before editing")
                before = _record(prior)
                after = {**values, "archived": 0}
                if before == after:
                    return plan_id
                db.execute("""UPDATE hq_maintenance_plans SET
                    customer_id=?,vehicle_id=?,plan_name=?,service=?,frequency=?,
                    next_due=?,status=?,visit_price_pence=?,visits_included=?,
                    internal_notes=?,portal_visible=?,revision=revision+1,updated_at=?
                    WHERE id=? AND revision=? AND archived=0""",
                    (values["customer_id"], values["vehicle_id"], values["plan_name"],
                     values["service"], values["frequency"], values["next_due"],
                     values["status"], values["visit_price_pence"],
                     values["visits_included"], values["internal_notes"],
                     values["portal_visible"], now, plan_id, expected_revision))
                _audit(db, plan_id, expected_revision + 1, "edit", before, after, actor)
        except sqlite3.IntegrityError as exc:
            raise ValueError("An active plan already exists for this customer, vehicle and service") from exc
    return plan_id


def archive_plan(plan_id, expected_revision, *, archive, actor="owner"):
    _ref(plan_id)
    if type(expected_revision) is not int or not 0 <= expected_revision < 2147483647 or type(archive) is not bool:
        raise ValueError("Invalid archive action or revision")
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT * FROM hq_maintenance_plans WHERE id=?", (plan_id,)).fetchone()
        if prior is None:
            raise LookupError("Maintenance plan not found")
        if prior["revision"] != expected_revision:
            raise ValueError("Plan changed; reload before saving")
        if bool(prior["archived"]) == archive:
            return
        before = _record(prior)
        try:
            db.execute("UPDATE hq_maintenance_plans SET archived=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                       (int(archive), _now(), plan_id, expected_revision))
            _audit(db, plan_id, expected_revision + 1,
                   "archive" if archive else "restore", before,
                   {**before, "archived": int(archive)}, actor)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Restore would duplicate another active plan") from exc


def plan_snapshot(plan_id):
    _ref(plan_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        row = db.execute("SELECT * FROM hq_maintenance_plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise LookupError("Maintenance plan not found")
        return dict(row)


def plan_index(*, show_archived=False):
    with connect() as db:
        _schema(db)
        _tables(db)
        return [dict(row) for row in db.execute("""SELECT p.*,c.name AS customer_name,
               v.make,v.model,v.registration
            FROM hq_maintenance_plans p
            JOIN hq_customers c ON c.id=p.customer_id
            JOIN hq_vehicles v ON v.id=p.vehicle_id AND v.customer_id=p.customer_id
            WHERE p.archived=? ORDER BY p.updated_at DESC,p.id DESC LIMIT 200""",
            (int(show_archived),))]


def plan_history(plan_id):
    _ref(plan_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        return [dict(row) for row in db.execute("""SELECT revision,action,created_at
            FROM hq_maintenance_plan_audit WHERE plan_id=?
            ORDER BY revision DESC LIMIT 100""", (plan_id,))]


def _csrf():
    if request.content_length is None or request.content_length > 5000:
        abort(413)
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
        abort(403)


@maintenance_plans.after_request
def _private(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@maintenance_plans.get("/hq/maintenance/plans")
@required
def owner_index():
    if not storage_ready():
        abort(503)
    try:
        return render_template("hq_maintenance_plans.html", listing=True,
                               plans=plan_index(show_archived=request.args.get("archived") == "1"))
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Maintenance plans unavailable")
        abort(503)


@maintenance_plans.route("/hq/maintenance/plans/new", methods=["GET", "POST"])
@required
def owner_new():
    if not storage_ready():
        abort(503)
    if request.method == "POST":
        _csrf()
    values = request.form.to_dict() if request.method == "POST" else {}
    selected = values.get("customer_id", request.args.get("customer_id", ""))
    query = request.args.get("q", "")[:81]
    try:
        if request.method == "POST":
            plan_id = save_plan(values, actor=session.get("google_sub", "owner"))
            return redirect("/hq/maintenance/plans/" + plan_id + "?saved=1", code=303)
        customers, vehicles = _options(query, selected)
        return render_template("hq_maintenance_plans.html", listing=False, new=True,
                               record=None, values=values, customers=customers, vehicles=vehicles,
                               csrf=session["csrf"], operation_id=uuid.uuid4().hex,
                               error=None, query=query, frequencies=FREQUENCIES, statuses=STATUSES)
    except (ValueError, LookupError) as exc:
        customers, vehicles = _options("", selected if isinstance(selected,str) and HEX.fullmatch(selected) else "")
        return render_template("hq_maintenance_plans.html", listing=False, new=True,
                               record=None, values=values, customers=customers, vehicles=vehicles,
                               csrf=session["csrf"], operation_id=values.get("operation_id", uuid.uuid4().hex),
                               error=str(exc), query=query, frequencies=FREQUENCIES, statuses=STATUSES), 400
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Maintenance plan unavailable")
        abort(503)


@maintenance_plans.route("/hq/maintenance/plans/<plan_id>", methods=["GET", "POST"])
@required
def owner_edit(plan_id):
    if not storage_ready():
        abort(503)
    try:
        record = plan_snapshot(plan_id)
        if request.method == "POST":
            _csrf()
            raw = request.form.get("revision", "")
            if not re.fullmatch(r"(?:0|[1-9][0-9]{0,9})", raw):
                abort(400)
            revision = int(raw)
            action = request.form.get("action", "save")
            if action in ("archive", "restore"):
                archive_plan(plan_id, revision, archive=action == "archive",
                             actor=session.get("google_sub", "owner"))
            elif action == "save":
                save_plan(request.form.to_dict(), plan_id=plan_id,
                          expected_revision=revision, actor=session.get("google_sub", "owner"))
            else:
                abort(400)
            return redirect("/hq/maintenance/plans/" + plan_id + "?saved=1", code=303)
        customers, vehicles = _options("", record["customer_id"])
        return render_template("hq_maintenance_plans.html", listing=False, new=False,
                               record=record, values=record, customers=customers, vehicles=vehicles,
                               csrf=session["csrf"], frequencies=FREQUENCIES, statuses=STATUSES,
                               error=None, events=plan_history(plan_id))
    except (ValueError, LookupError) as exc:
        if request.method == "GET":
            abort(404)
        record = plan_snapshot(plan_id)
        customers, vehicles = _options("", record["customer_id"])
        return render_template("hq_maintenance_plans.html", listing=False, new=False,
                               record=record, values=request.form.to_dict(), customers=customers,
                               vehicles=vehicles, csrf=session["csrf"], frequencies=FREQUENCIES,
                               statuses=STATUSES, error=str(exc),
                               events=plan_history(plan_id)), 409
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Maintenance plan unavailable")
        abort(503)


@maintenance_plans.get("/customer/maintenance/plans")
def customer_index():
    customer_id = active_customer()
    if not customer_id:
        return redirect("/customer/login")
    try:
        with connect() as db:
            _schema(db)
            _tables(db)
            rows = [dict(row) for row in db.execute("""SELECT p.id,p.plan_name,p.service,
                p.frequency,p.next_due,p.status,v.make,v.model,v.registration
                FROM hq_maintenance_plans p JOIN hq_vehicles v
                ON v.id=p.vehicle_id AND v.customer_id=p.customer_id
                WHERE p.customer_id=? AND p.portal_visible=1 AND p.archived=0
                ORDER BY p.updated_at DESC,p.id DESC LIMIT 100""", (customer_id,))]
        return render_template("customer_maintenance_plans.html", plans=rows)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Customer maintenance plans unavailable")
        abort(503)
