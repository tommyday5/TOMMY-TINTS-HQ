"""Native owner-managed service prices and owner-reviewed customer service enquiries.

Records share customer_requests.sqlite3 and canonical HQ customer/vehicle IDs.
No legacy workbook import, automatic trade entitlement, booking or payment.
"""
import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, current_app, redirect, render_template, request, session
from customer_request_store import SERVICES, connect, storage_ready
from hq_auth import required
from hq_core_records import _schema, _audit_work, StaleWorkRecordError
from hq_native_work import _tables
from hq_customer_portal import active_customer

service_quotes = Blueprint("service_quotes", __name__)
HEX = re.compile(r"[0-9a-f]{32}\Z")
TIERS = ("retail", "trade", "maintenance")
STATUSES = ("submitted", "reviewing", "quoted", "declined", "closed")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _db():
    if not storage_ready():
        raise RuntimeError("Persistent HQ storage is unavailable")
    db = connect()
    db.execute("PRAGMA foreign_keys=ON")
    _schema(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_service_prices (
        service TEXT PRIMARY KEY,
        retail_pence INTEGER, trade_pence INTEGER, maintenance_pence INTEGER,
        published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_service_prices_audit (
        id TEXT PRIMARY KEY, service TEXT NOT NULL, revision INTEGER NOT NULL,
        action TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_service_quote_requests (
        id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
        fingerprint TEXT NOT NULL, customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        vehicle_id TEXT NOT NULL REFERENCES hq_vehicles(id),
        requested_tier TEXT NOT NULL CHECK(requested_tier IN ('retail','trade','maintenance')),
        services_json TEXT NOT NULL, owner_prices_json TEXT NOT NULL,
        notes TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('submitted','reviewing','quoted','declined','closed')),
        approved_total_pence INTEGER, public_note TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_service_quote_customer
        ON hq_service_quote_requests(customer_id,created_at)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_service_quote_audit (
        id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES hq_service_quote_requests(id),
        revision INTEGER NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_service_quote_work_links (
        request_id TEXT PRIMARY KEY REFERENCES hq_service_quote_requests(id),
        work_id TEXT NOT NULL UNIQUE REFERENCES hq_work_records(id),
        request_revision INTEGER NOT NULL, customer_tier TEXT NOT NULL,
        approved_total_pence INTEGER NOT NULL, services_json TEXT NOT NULL,
        actor TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    return db


def _id(value, name):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError("Invalid " + name)
    return value


def _pence(value, *, optional=False):
    if optional and value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,2})?", value):
        raise ValueError("Enter pounds with up to two decimal places")
    try:
        amount = int(Decimal(value) * 100)
    except InvalidOperation:
        raise ValueError("Invalid price")
    if not 1 <= amount <= 100000000:
        raise ValueError("Prices must be above zero and no more than £1,000,000")
    return amount


def save_service_price(service, retail, trade, maintenance, published, revision, actor="owner"):
    """Owner-maintained prices; no automatic historical Cost Library import."""
    if service not in SERVICES or not isinstance(service, str):
        raise ValueError("Choose a supported service")
    if type(revision) is not int or not 0 <= revision <= 100000000:
        raise ValueError("Invalid revision")
    if published not in (True, False) or type(published) is not bool:
        raise ValueError("Invalid publication state")
    prices = [_pence(v, optional=True) for v in (retail, trade, maintenance)]
    if published and prices[0] is None:
        # Retail indicative price can be unknown, but a published service must
        # have a considered tariff before presenting it in the request selector.
        raise ValueError("Enter an owner-reviewed retail tariff before publishing")
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM hq_service_prices WHERE service=?", (service,)).fetchone()
        previous_rev = existing["revision"] if existing else 0
        same = bool(existing) and (
            existing["retail_pence"], existing["trade_pence"],
            existing["maintenance_pence"], existing["published"]) == (
                *prices, int(published))
        if same:
            return previous_rev  # exact retries are safe
        if revision != previous_rev:
            raise ValueError("Service pricing changed elsewhere; reload before saving")
        timestamp = _now()
        if existing:
            db.execute("""UPDATE hq_service_prices SET retail_pence=?,trade_pence=?,
                maintenance_pence=?,published=?,revision=revision+1,updated_at=?
                WHERE service=? AND revision=?""",
                (*prices, int(published), timestamp, service, revision))
        else:
            db.execute("""INSERT INTO hq_service_prices VALUES (?,?,?,?,?,1,?)""",
                       (service, *prices, int(published), timestamp))
        db.execute("INSERT INTO hq_service_prices_audit VALUES (?,?,?,?,?,?)",
                   (uuid.uuid4().hex, service, previous_rev + 1,
                    "tariff_saved", str(actor)[:100], timestamp))
        return previous_rev + 1


def catalogue():
    with _db() as db:
        entries = {row["service"]: dict(row) for row in db.execute(
            "SELECT * FROM hq_service_prices")}
        return [entries.get(s, {"service": s, "retail_pence": None, "trade_pence": None,
                               "maintenance_pence": None, "published": 0, "revision": 0})
                for s in sorted(SERVICES)]


def public_services():
    with _db() as db:
        return [dict(row) for row in db.execute("""SELECT service,retail_pence
            FROM hq_service_prices WHERE published=1 ORDER BY service""")]


def create_request(customer_id, operation_id, vehicle_id, requested_tier, services, notes):
    _id(customer_id, "customer")
    _id(operation_id, "request")
    _id(vehicle_id, "vehicle")
    if requested_tier not in TIERS:
        raise ValueError("Choose Retail, Trade or Maintenance")
    if not isinstance(services, list) or not 1 <= len(services) <= 5:
        raise ValueError("Select 1 to 5 services")
    if any(not isinstance(service, str) or service not in SERVICES for service in services):
        raise ValueError("Choose supported services")
    if len(set(services)) != len(services):
        raise ValueError("Duplicate service category")
    if not isinstance(notes, str) or len(notes.strip()) > 1000:
        raise ValueError("Notes must be 1,000 characters or fewer")
    normal = sorted(services)
    notes = notes.strip()
    payload = {"vehicle": vehicle_id, "tier": requested_tier,
               "services": normal, "notes": notes}
    fingerprint = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("""SELECT id,customer_id,fingerprint FROM hq_service_quote_requests
            WHERE operation_id=?""", (operation_id,)).fetchone()
        if prior:
            if prior["customer_id"] != customer_id or prior["fingerprint"] != fingerprint:
                raise ValueError("Request ID already used for different details")
            return prior["id"]
        if not db.execute("SELECT 1 FROM hq_vehicles WHERE id=? AND customer_id=?",
                          (vehicle_id, customer_id)).fetchone():
            raise ValueError("Vehicle is not linked to your account")
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        if db.execute("""SELECT count(*) FROM hq_service_quote_requests
            WHERE customer_id=? AND created_at>?""",
            (customer_id, one_hour_ago)).fetchone()[0] >= 5:
            raise ValueError("Too many requests; contact Tommy Tints")
        query = ",".join("?" for _ in normal)
        available = {row["service"]: row for row in db.execute(
            "SELECT * FROM hq_service_prices WHERE published=1 AND service IN ("+query+")",
            normal)}
        if len(available) != len(normal):
            raise ValueError("A selected service is no longer available; reload")
        # Private owner tariff snapshot. Customer-facing responses omit this field.
        owner_prices = {service: available[service][requested_tier + "_pence"]
                        for service in normal}
        ident, timestamp = uuid.uuid4().hex, _now()
        db.execute("""INSERT INTO hq_service_quote_requests
            (id,operation_id,fingerprint,customer_id,vehicle_id,requested_tier,
             services_json,owner_prices_json,notes,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,'submitted',?,?)""",
            (ident, operation_id, fingerprint, customer_id, vehicle_id,
             requested_tier, json.dumps(normal), json.dumps(owner_prices),
             notes, timestamp, timestamp))
        db.execute("INSERT INTO hq_service_quote_audit VALUES (?,?,?,?,?,?)",
                   (uuid.uuid4().hex, ident, 0, "submitted", "customer", timestamp))
        return ident


def review_request(ident, revision, status, approved_pounds, public_note, actor="owner"):
    _id(ident, "service request")
    if type(revision) is not int or not 0 <= revision <= 100000000:
        raise ValueError("Invalid revision")
    if status not in ("reviewing", "quoted", "declined", "closed"):
        raise ValueError("Invalid review stage")
    if not isinstance(public_note, str) or len(public_note.strip()) > 400:
        raise ValueError("Public note too long")
    note = public_note.strip()
    approved = _pence(approved_pounds) if status == "quoted" else None
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM hq_service_quote_requests WHERE id=?",
                         (ident,)).fetchone()
        if not row:
            raise LookupError("Service request not found")
        if (row["status"], row["approved_total_pence"], row["public_note"]) == (
                status, approved, note):
            return row["revision"]
        if db.execute("SELECT 1 FROM hq_service_quote_work_links WHERE request_id=?",
                      (ident,)).fetchone():
            raise ValueError("This enquiry already has a native HQ quote; edit the linked quote instead")
        if row["revision"] != revision:
            raise ValueError("Request changed; reload before updating")
        if row["status"] in ("declined", "closed"):
            raise ValueError("Final requests cannot be reopened")
        timestamp = _now()
        db.execute("""UPDATE hq_service_quote_requests SET status=?,
            approved_total_pence=?,public_note=?,revision=revision+1,updated_at=?
            WHERE id=? AND revision=?""", (status, approved, note, timestamp, ident, revision))
        db.execute("INSERT INTO hq_service_quote_audit VALUES (?,?,?,?,?,?)",
                   (uuid.uuid4().hex, ident, revision + 1,
                    status, str(actor)[:100], timestamp))
        return revision + 1



def convert_approved_service_quote(ident, revision, confirmed_tier, actor="owner"):
    """Explicit owner action; canonical IDs, no J number, booking or payment."""
    _id(ident, "service request")
    if type(revision) is not int or not 0 <= revision <= 100000000:
        raise ValueError("Invalid revision")
    if not isinstance(confirmed_tier, str) or confirmed_tier not in TIERS:
        raise ValueError("Owner must select the verified pricing tier")
    with _db() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM hq_service_quote_requests WHERE id=?",
                         (ident,)).fetchone()
        if not row:
            raise LookupError("Service request not found")
        linked = db.execute("""SELECT work_id,request_revision,customer_tier
            FROM hq_service_quote_work_links WHERE request_id=?""",
            (ident,)).fetchone()
        if linked:
            if linked["request_revision"] != revision or linked["customer_tier"] != confirmed_tier:
                raise StaleWorkRecordError("An HQ quote already exists for this enquiry; open the linked quote")
            return linked["work_id"]
        if row["revision"] != revision:
            raise StaleWorkRecordError("Enquiry changed; reload before creating a quote")
        if row["status"] != "quoted" or row["approved_total_pence"] is None:
            raise ValueError("Save an owner-reviewed quoted price before creating an HQ quote")
        if not 1 <= row["approved_total_pence"] <= 100000000:
            raise ValueError("Invalid owner-approved price")
        services = json.loads(row["services_json"])
        if (not isinstance(services, list) or not 1 <= len(services) <= 5
                or any(not isinstance(item, str) or item not in SERVICES for item in services)
                or len(set(services)) != len(services)):
            raise ValueError("Enquiry services need review")
        if not db.execute("SELECT 1 FROM hq_vehicles WHERE id=? AND customer_id=?",
                          (row["vehicle_id"], row["customer_id"])).fetchone():
            raise ValueError("Enquiry vehicle ownership has changed; review required")
        now, work_id = _now(), uuid.uuid4().hex
        notes = ("Source service enquiry: " + ident + "\n"
                 + "Selected services: " + ", ".join(services) + "\n"
                 + "Customer scope: " + row["notes"])
        db.execute("""INSERT INTO hq_work_records
            (id,source_request_id,customer_id,vehicle_id,kind,service,notes,
             preferred_date,preferred_time,status,price_pence,deposit_pence,revision,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,'ready_for_review',?,0,0,?)""",
            (work_id, "HQ-" + work_id, row["customer_id"], row["vehicle_id"],
             "quote", services[0], notes, "", "", row["approved_total_pence"], now))
        db.execute("INSERT INTO hq_native_work_details VALUES (?,?,?)",
                   (work_id, confirmed_tier, now))
        db.execute("INSERT INTO hq_native_work_events VALUES (?,?,?,?,?)",
                   (uuid.uuid4().hex, work_id, "service_enquiry_quote_created", 0, now))
        db.execute("""INSERT INTO hq_service_quote_work_links
            (request_id,work_id,request_revision,customer_tier,approved_total_pence,
             services_json,actor,created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (ident, work_id, revision, confirmed_tier, row["approved_total_pence"],
             row["services_json"], str(actor)[:100], now))
        _audit_work(db, work_id, 0, "service_enquiry_origin", {},
                    {"request_id": ident, "kind": "quote", "owner_tier": confirmed_tier})
        db.execute("INSERT INTO hq_service_quote_audit VALUES (?,?,?,?,?,?)",
                   (uuid.uuid4().hex, ident, revision, "native_quote_created",
                    str(actor)[:100], now))
        # New native work is not linked to any Sheet row or sync outbox.
    return work_id


def customer_requests(customer_id):
    _id(customer_id, "customer")
    with _db() as db:
        rows = db.execute("""SELECT q.id,q.requested_tier,q.services_json,q.status,
            q.approved_total_pence,q.public_note,q.created_at,v.make,v.model,v.registration,
            CASE WHEN w.status IN ('approved','in_progress','completed','cancelled')
                THEN w.status ELSE NULL END AS linked_status,
            CASE WHEN w.status IN ('approved','in_progress','completed','cancelled')
                THEN w.price_pence ELSE NULL END AS linked_price_pence
            FROM hq_service_quote_requests q JOIN hq_vehicles v ON v.id=q.vehicle_id
            LEFT JOIN hq_service_quote_work_links l ON l.request_id=q.id
            LEFT JOIN hq_work_records w ON w.id=l.work_id
            WHERE q.customer_id=? ORDER BY q.created_at DESC,q.id DESC LIMIT 100""",
            (customer_id,)).fetchall()
        return [{**dict(r), "services": json.loads(r["services_json"])} for r in rows]


def owner_requests():
    with _db() as db:
        rows = db.execute("""SELECT q.*,c.name AS customer_name,
            v.make,v.model,v.registration,l.work_id AS linked_work_id,
            l.customer_tier AS confirmed_tier,w.status AS linked_work_status
            FROM hq_service_quote_requests q
            JOIN hq_customers c ON c.id=q.customer_id
            JOIN hq_vehicles v ON v.id=q.vehicle_id
            LEFT JOIN hq_service_quote_work_links l ON l.request_id=q.id
            LEFT JOIN hq_work_records w ON w.id=l.work_id
            ORDER BY q.created_at DESC,q.id DESC LIMIT 100""").fetchall()
        return [{**dict(r), "services": json.loads(r["services_json"]),
                 "owner_prices": json.loads(r["owner_prices_json"])} for r in rows]


def _csrf(name):
    if not secrets.compare_digest(request.form.get("csrf", ""),
                                  session.get(name, "missing")):
        abort(403)
    if request.content_length is None or request.content_length > 10000:
        abort(413)


@service_quotes.after_request
def private_headers(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@service_quotes.route("/hq/services", methods=["GET", "POST"])
@required
def owner_catalogue():
    error = None
    if request.method == "POST":
        _csrf("csrf")
        revision = request.form.get("revision", "")
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})", revision):
            abort(400)
        try:
            save_service_price(request.form.get("service"),
                               request.form.get("retail", ""), request.form.get("trade", ""),
                               request.form.get("maintenance", ""),
                               request.form.get("published") == "1", int(revision),
                               session.get("google_sub", "owner"))
            return redirect("/hq/services?saved=1", code=303)
        except ValueError as exc:
            error = str(exc)
        except (sqlite3.Error, OSError, RuntimeError):
            current_app.logger.exception("Service catalogue unavailable")
            abort(503)
    try:
        return render_template("hq_services.html", services=catalogue(),
                               csrf=session.get("csrf"), error=error), (409 if error else 200)
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Service catalogue unavailable")
        abort(503)


@service_quotes.route("/book/services", methods=["GET", "POST"])
def customer_catalogue():
    ident = active_customer()
    error = None
    if request.method == "POST":
        if not ident:
            return redirect("/customer/login")
        _csrf("customer_portal_csrf")
        try:
            create_request(ident, request.form.get("operation_id"),
                           request.form.get("vehicle_id"), request.form.get("requested_tier"),
                           request.form.getlist("services"), request.form.get("notes", ""))
            return redirect("/customer/service-enquiries?saved=1", code=303)
        except (ValueError, LookupError) as exc:
            error = str(exc)
        except (sqlite3.Error, OSError, RuntimeError):
            current_app.logger.exception("Service enquiry unavailable")
            abort(503)
    try:
        with _db() as db:
            vehicles = [dict(v) for v in db.execute(
                "SELECT id,make,model,registration FROM hq_vehicles WHERE customer_id=? ORDER BY make,model",
                (ident,))] if ident else []
        return render_template("customer_services.html", services=public_services(),
                               vehicles=vehicles, customer_active=bool(ident),
                               csrf=session.get("customer_portal_csrf"),
                               operation_id=uuid.uuid4().hex, error=error), (409 if error else 200)
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Service catalogue unavailable")
        abort(503)


@service_quotes.get("/customer/service-enquiries")
def customer_index():
    ident = active_customer()
    if not ident:
        return redirect("/customer/login")
    try:
        return render_template("customer_service_enquiries.html",
                               enquiries=customer_requests(ident))
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Customer service requests unavailable")
        abort(503)


@service_quotes.route("/hq/service-enquiries", methods=["GET", "POST"])
@required
def owner_index():
    error = None
    if request.method == "POST":
        _csrf("csrf")
        revision = request.form.get("revision", "")
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})", revision):
            abort(400)
        try:
            review_request(request.form.get("request_id"), int(revision),
                           request.form.get("status"),
                           request.form.get("approved_pounds", ""),
                           request.form.get("public_note", ""),
                           session.get("google_sub", "owner"))
            return redirect("/hq/service-enquiries?saved=1", code=303)
        except (ValueError, LookupError) as exc:
            error = str(exc)
        except (sqlite3.Error, OSError, RuntimeError):
            current_app.logger.exception("Owner service inbox unavailable")
            abort(503)
    try:
        return render_template("hq_service_enquiries.html",
                               enquiries=owner_requests(), csrf=session.get("csrf"),
                               error=error), (409 if error else 200)
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Owner service inbox unavailable")
        abort(503)


@service_quotes.post("/hq/service-enquiries/<ident>/create-work")
@required
def owner_create_service_work(ident):
    _csrf("csrf")
    raw_revision = request.form.get("revision", "")
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})", raw_revision):
        abort(400)
    try:
        work_id = convert_approved_service_quote(
            ident, int(raw_revision), request.form.get("customer_tier"),
            session.get("google_sub", "owner"))
    except (StaleWorkRecordError, ValueError) as exc:
        return render_template("hq_service_enquiries.html",
                               enquiries=owner_requests(), csrf=session.get("csrf"),
                               error=str(exc)), (409 if isinstance(exc, StaleWorkRecordError) else 400)
    except LookupError:
        abort(404)
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Service enquiry conversion unavailable")
        abort(503)
    return redirect("/hq/work/" + work_id + "/edit?created=1", code=303)
