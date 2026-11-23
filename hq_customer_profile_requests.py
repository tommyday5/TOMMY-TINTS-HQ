"""Customer account profile corrections with explicit owner reconciliation.

Customers can request changes only to their own canonical HQ identity/vehicles.
Requests are durable but never silently alter historical/customer records. An
owner edits the canonical record through its existing validated editor, then
reconciles the request; mismatched values cannot be falsely marked applied.
"""
import re
import secrets
import sqlite3
import uuid
from datetime import date, datetime, timezone

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for

from customer_request_store import connect
from hq_auth import required
from hq_core_records import _schema
from hq_customer_portal import active_customer
from hq_portal_vehicle_additions import (
    submit_vehicle_request, customer_vehicle_requests, owner_vehicle_requests,
    decide_vehicle_request,
)

profile_requests = Blueprint("profile_requests", __name__)
_ID = re.compile(r"[0-9a-f]{32}\Z")
_CUSTOMER_FIELDS = {"name": 160, "email": 254, "phone": 40}
_VEHICLE_FIELDS = {"make": 60, "model": 60, "model_year": 4, "registration": 20}
_MAX_PENDING = 12


def _now():
    return datetime.now(timezone.utc).isoformat()


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_portal_profile_requests(
        id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        vehicle_id TEXT REFERENCES hq_vehicles(id),
        field TEXT NOT NULL, proposed_value TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','resolved','declined')),
        created_at TEXT NOT NULL, decided_at TEXT
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_portal_profile_requests_customer
        ON hq_portal_profile_requests(customer_id,status,created_at)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_portal_profile_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL REFERENCES hq_portal_profile_requests(id),
        action TEXT NOT NULL CHECK(action IN ('requested','resolved','declined')),
        occurred_at TEXT NOT NULL
    )""")


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid canonical HQ reference")


def _clean(field, value, vehicle):
    fields = _VEHICLE_FIELDS if vehicle else _CUSTOMER_FIELDS
    if field not in fields or not isinstance(value, str):
        raise ValueError("Select a valid profile field")
    cleaned = value.strip()
    if (not cleaned or len(cleaned) > fields[field]
            or any(ord(char) < 32 or ord(char) == 127 for char in cleaned)):
        raise ValueError("Enter a valid proposed value within the field limit")
    if field == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", cleaned):
        raise ValueError("Enter a valid email address")
    if field == "model_year" and (not re.fullmatch(r"[0-9]{4}", cleaned)
                                   or not 1950 <= int(cleaned) <= date.today().year + 1):
        raise ValueError("Enter a valid vehicle year")
    if field == "registration":
        cleaned = " ".join(cleaned.upper().split())
        if not re.fullmatch(r"[A-Z0-9 -]+", cleaned):
            raise ValueError("Enter a valid registration")
    return cleaned


def submit_profile_correction(customer_id, vehicle_id, field, proposed_value, operation_id):
    """Single-transaction, replay-safe request creation; never edits customer/vehicle."""
    _id(customer_id)
    _id(operation_id)
    if vehicle_id:
        _id(vehicle_id)
    value = _clean(field, proposed_value, bool(vehicle_id))
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("""SELECT id,customer_id,vehicle_id,field,proposed_value
            FROM hq_portal_profile_requests WHERE operation_id=?""", (operation_id,)).fetchone()
        if prior:
            if (prior["customer_id"], prior["vehicle_id"], prior["field"], prior["proposed_value"]) != (customer_id, vehicle_id or None, field, value):
                raise ValueError("This submission reference was already used")
            return prior["id"]
        if vehicle_id:
            row = db.execute("""SELECT 1 FROM hq_vehicles WHERE id=? AND customer_id=?""",
                             (vehicle_id, customer_id)).fetchone()
        else:
            row = db.execute("SELECT 1 FROM hq_customers WHERE id=?", (customer_id,)).fetchone()
        if row is None:
            raise LookupError("Profile or linked vehicle not found")
        if db.execute("""SELECT count(*) FROM hq_portal_profile_requests
            WHERE customer_id=? AND status='pending'""", (customer_id,)).fetchone()[0] >= _MAX_PENDING:
            raise ValueError("You already have several pending corrections; contact Tommy Tints")
        existing = db.execute("""SELECT id FROM hq_portal_profile_requests
            WHERE customer_id=? AND vehicle_id IS ? AND field=? AND proposed_value=?
            AND status='pending' LIMIT 1""",
            (customer_id, vehicle_id or None, field, value)).fetchone()
        if existing:
            return existing["id"]
        ref, now = uuid.uuid4().hex, _now()
        db.execute("""INSERT INTO hq_portal_profile_requests(
            id,operation_id,customer_id,vehicle_id,field,proposed_value,status,created_at)
            VALUES(?,?,?,?,?,?,'pending',?)""",
            (ref, operation_id, customer_id, vehicle_id or None, field, value, now))
        db.execute("""INSERT INTO hq_portal_profile_events(request_id,action,occurred_at)
            VALUES(?,'requested',?)""", (ref, now))
        return ref


def customer_profile_snapshot(customer_id):
    _id(customer_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        customer = db.execute("SELECT id,name,email,phone FROM hq_customers WHERE id=?",
                              (customer_id,)).fetchone()
        if customer is None:
            raise LookupError("Customer not found")
        vehicles = [dict(r) for r in db.execute("""SELECT id,make,model,model_year,registration
            FROM hq_vehicles WHERE customer_id=? ORDER BY created_at,id""", (customer_id,))]
        pending = [dict(r) for r in db.execute("""SELECT r.id,r.vehicle_id,r.field,r.proposed_value,
            r.status,r.created_at FROM hq_portal_profile_requests r
            WHERE r.customer_id=? AND r.status='pending'
            ORDER BY r.created_at DESC,r.id DESC LIMIT 20""", (customer_id,))]
    return dict(customer), vehicles, pending


def owner_pending_corrections():
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        rows = db.execute("""SELECT r.id,r.customer_id,r.vehicle_id,r.field,
            r.proposed_value,r.created_at,c.name AS customer_name,
            v.registration AS vehicle_registration
            FROM hq_portal_profile_requests r
            JOIN hq_customers c ON c.id=r.customer_id
            LEFT JOIN hq_vehicles v ON v.id=r.vehicle_id AND v.customer_id=r.customer_id
            WHERE r.status='pending' ORDER BY r.created_at,r.id LIMIT 100""").fetchall()
    return [dict(row) for row in rows]


def decide_profile_correction(ref, decision):
    """An owner may resolve only after the actual canonical field matches."""
    _id(ref)
    if decision not in ("resolved", "declined"):
        raise ValueError("Unknown decision")
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM hq_portal_profile_requests WHERE id=?", (ref,)).fetchone()
        if row is None:
            raise LookupError("Correction request not found")
        if row["status"] == decision:
            return
        if row["status"] != "pending":
            raise ValueError("This request has already been decided")
        if decision == "resolved":
            fields = _VEHICLE_FIELDS if row["vehicle_id"] else _CUSTOMER_FIELDS
            if row["field"] not in fields:
                raise ValueError("Unsupported correction field")
            column = row["field"]  # allowlisted SQL identifier
            if row["vehicle_id"]:
                current = db.execute(
                    f"SELECT {column} FROM hq_vehicles WHERE id=? AND customer_id=?",
                    (row["vehicle_id"], row["customer_id"])).fetchone()
            else:
                current = db.execute(
                    f"SELECT {column} FROM hq_customers WHERE id=?",
                    (row["customer_id"],)).fetchone()
            if current is None or (current[0] or "").strip() != row["proposed_value"]:
                raise ValueError("The saved HQ record does not match this request. Edit the canonical record first.")
        now = _now()
        result = db.execute("""UPDATE hq_portal_profile_requests
            SET status=?,decided_at=? WHERE id=? AND status='pending'""",
            (decision, now, ref))
        if result.rowcount != 1:
            raise ValueError("Correction changed. Reload and review it again.")
        db.execute("""INSERT INTO hq_portal_profile_events(request_id,action,occurred_at)
            VALUES(?,?,?)""", (ref, decision, now))


@profile_requests.after_request
def _privacy(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@profile_requests.get("/customer/profile")
def customer_profile():
    customer_id = active_customer()
    if not customer_id:
        return redirect("/customer/login", code=302)
    try:
        customer, vehicles, pending = customer_profile_snapshot(customer_id)
    except (LookupError, sqlite3.Error, OSError):
        current_app.logger.exception("Customer profile unavailable")
        abort(503)
    return render_template("customer_profile.html", customer=customer,
                           vehicles=vehicles, pending=pending,
                           vehicle_requests=customer_vehicle_requests(customer_id),
                           csrf=session["customer_portal_csrf"],
                           operation_id=secrets.token_hex(16),
                           vehicle_operation_id=secrets.token_hex(16),
                           submitted=request.args.get("submitted") == "1",
                           vehicle_submitted=request.args.get("vehicle_submitted") == "1",
                           error=None)


@profile_requests.post("/customer/profile/correction")
def customer_correction():
    customer_id = active_customer()
    if not customer_id:
        return redirect("/customer/login", code=302)
    if request.content_length is None or request.content_length > 2048:
        abort(413)
    if not secrets.compare_digest(request.form.get("csrf", ""),
                                  session.get("customer_portal_csrf", "not-set")):
        abort(403)
    try:
        submit_profile_correction(customer_id, request.form.get("vehicle_id", ""),
                                  request.form.get("field", ""), request.form.get("value", ""),
                                  request.form.get("operation_id", ""))
    except (ValueError, LookupError) as exc:
        customer, vehicles, pending = customer_profile_snapshot(customer_id)
        return render_template("customer_profile.html", customer=customer,
                               vehicles=vehicles, pending=pending,
                               vehicle_requests=customer_vehicle_requests(customer_id),
                               csrf=session["customer_portal_csrf"],
                               operation_id=secrets.token_hex(16),
                               vehicle_operation_id=secrets.token_hex(16),
                               submitted=False, vehicle_submitted=False,
                               error=str(exc)), 400
    except (sqlite3.Error, OSError):
        current_app.logger.exception("Customer correction unavailable")
        abort(503)
    return redirect(url_for(".customer_profile", submitted="1"), code=303)


@profile_requests.route("/hq/profile-change-requests", methods=["GET", "POST"])
@required
def owner_corrections():
    if request.method == "POST":
        if request.content_length is None or request.content_length > 2048:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
            abort(403)
        try:
            decide_profile_correction(request.form.get("request_id", ""),
                                      request.form.get("decision", ""))
        except LookupError:
            abort(404)
        except ValueError as exc:
            try:
                pending = owner_pending_corrections()
            except (sqlite3.Error, OSError):
                abort(503)
            return render_template("hq_profile_change_requests.html", pending=pending,
                                   csrf=session["csrf"], error=str(exc)), 409
        except (sqlite3.Error, OSError):
            current_app.logger.exception("Owner correction decision unavailable")
            abort(503)
        return redirect(url_for(".owner_corrections", saved="1"), code=303)
    try:
        pending = owner_pending_corrections()
    except (sqlite3.Error, OSError):
        current_app.logger.exception("Owner corrections unavailable")
        abort(503)
    return render_template("hq_profile_change_requests.html", pending=pending,
                           csrf=session["csrf"], error=None,
                           saved=request.args.get("saved") == "1")


@profile_requests.post("/customer/profile/vehicle-request")
def customer_vehicle_addition():
    """Customer proposes a vehicle; never saves it to the canonical vehicle table."""
    customer_id = active_customer()
    if not customer_id:
        return redirect("/customer/login", code=302)
    if request.content_length is None or request.content_length > 2048:
        abort(413)
    if not secrets.compare_digest(request.form.get("csrf", ""),
                                  session.get("customer_portal_csrf", "not-set")):
        abort(403)
    try:
        submit_vehicle_request(customer_id, request.form)
    except (ValueError, LookupError) as exc:
        try:
            customer, vehicles, pending = customer_profile_snapshot(customer_id)
            additions = customer_vehicle_requests(customer_id)
        except (sqlite3.Error, OSError):
            abort(503)
        return render_template("customer_profile.html", customer=customer,
            vehicles=vehicles, pending=pending, vehicle_requests=additions,
            csrf=session["customer_portal_csrf"], operation_id=secrets.token_hex(16),
            vehicle_operation_id=secrets.token_hex(16),
            submitted=False, vehicle_submitted=False, error=str(exc)), 400
    except (sqlite3.Error, OSError):
        current_app.logger.exception("Customer vehicle request unavailable")
        abort(503)
    return redirect(url_for(".customer_profile", vehicle_submitted="1"), code=303)


@profile_requests.route("/hq/vehicle-add-requests", methods=["GET", "POST"])
@required
def owner_vehicle_additions():
    """Owner reviews requested additions; must separately save an actual vehicle."""
    if request.method == "POST":
        if request.content_length is None or request.content_length > 2048:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""),
                                      session.get("csrf", "not-set")):
            abort(403)
        try:
            decide_vehicle_request(request.form.get("request_id", ""),
                request.form.get("decision", ""), request.form.get("vehicle_id", ""))
        except LookupError:
            abort(404)
        except ValueError as exc:
            try:
                pending = owner_vehicle_requests()
            except (sqlite3.Error, OSError):
                abort(503)
            return render_template("hq_vehicle_add_requests.html", pending=pending,
                csrf=session["csrf"], error=str(exc), saved=False), 409
        except (sqlite3.Error, OSError):
            current_app.logger.exception("Vehicle addition decision unavailable")
            abort(503)
        return redirect(url_for(".owner_vehicle_additions", saved="1"), code=303)
    try:
        pending = owner_vehicle_requests()
    except (sqlite3.Error, OSError):
        current_app.logger.exception("Vehicle addition inbox unavailable")
        abort(503)
    return render_template("hq_vehicle_add_requests.html", pending=pending,
        csrf=session["csrf"], error=None, saved=request.args.get("saved") == "1")
