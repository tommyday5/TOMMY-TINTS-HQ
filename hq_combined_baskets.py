"""One owner-reviewed service + product basket against canonical HQ customers.

Additive request-only workflow: no appointments, payment, work creation,
inventory reservation, Sheets writes or implied trade-tier entitlement.
Existing service/shop workflows remain unchanged.
"""
import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import Blueprint, abort, current_app, redirect, render_template, request, session

from customer_request_store import SERVICES
from hq_auth import required
from hq_commerce import basket_quote, database as product_database
from hq_customer_portal import active_customer
from hq_service_quotes import _db as canonical_db, public_services

combined_baskets = Blueprint("combined_baskets", __name__)
_ID = re.compile(r"[0-9a-f]{32}\Z")
_REV = re.compile(r"(?:0|[1-9][0-9]{0,8})\Z")
_TIERS = ("retail", "trade", "maintenance")
_STAGES = ("submitted", "reviewing", "quoted", "declined", "closed")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _valid_id(value, field):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid " + field)
    return value


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_combined_baskets (
        id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
        payload_fingerprint TEXT NOT NULL,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        vehicle_id TEXT REFERENCES hq_vehicles(id),
        requested_tier TEXT NOT NULL CHECK(requested_tier IN ('retail','trade','maintenance')),
        services_json TEXT NOT NULL,
        products_json TEXT NOT NULL,
        fulfilment TEXT NOT NULL CHECK(fulfilment IN ('collection','delivery')),
        service_retail_pence INTEGER NOT NULL,
        product_retail_pence INTEGER NOT NULL,
        delivery_pence INTEGER NOT NULL,
        indicative_total_pence INTEGER NOT NULL,
        notes TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('submitted','reviewing','quoted','declined','closed')),
        approved_total_pence INTEGER,
        public_note TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_combined_customer
        ON hq_combined_baskets(customer_id,created_at)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_combined_basket_audit (
        id TEXT PRIMARY KEY, basket_id TEXT NOT NULL REFERENCES hq_combined_baskets(id),
        revision INTEGER NOT NULL, action TEXT NOT NULL,
        actor TEXT NOT NULL, occurred_at TEXT NOT NULL,
        UNIQUE(basket_id,revision)
    )""")


def _normalise(services, lines, fulfilment, vehicle_id, tier, notes):
    if tier not in _TIERS:
        raise ValueError("Choose Retail, Trade or Maintenance")
    if fulfilment not in ("collection", "delivery"):
        raise ValueError("Choose collection or delivery")
    if not isinstance(services, list) or len(services) > 5 or any(
            not isinstance(s, str) or s not in SERVICES for s in services):
        raise ValueError("Choose up to five supported services")
    if len(set(services)) != len(services):
        raise ValueError("Duplicate service")
    if not isinstance(lines, list) or len(lines) > 30:
        raise ValueError("Choose up to thirty products")
    result, seen = [], set()
    for line in lines:
        if not isinstance(line, dict):
            raise ValueError("Invalid product item")
        product = _valid_id(line.get("id"), "product")
        quantity = line.get("quantity")
        if type(quantity) is not int or not 1 <= quantity <= 100 or product in seen:
            raise ValueError("Invalid or duplicate product quantity")
        seen.add(product)
        result.append({"id": product, "quantity": quantity})
    if not services and not result:
        raise ValueError("Choose at least one service or product")
    if services:
        _valid_id(vehicle_id, "vehicle")
    elif vehicle_id not in (None, ""):
        raise ValueError("Vehicle must only be selected for services")
    if not isinstance(notes, str) or len(notes.strip()) > 1000:
        raise ValueError("Notes must be at most 1,000 characters")
    return sorted(services), sorted(result, key=lambda v: v["id"]), (
        vehicle_id if services else None), notes.strip()


def submit_combined(customer_id, operation_id, vehicle_id, tier,
                    services, lines, fulfilment, notes=""):
    """Snapshot published retail information; freeze one customer enquiry atomically."""
    _valid_id(customer_id, "customer")
    _valid_id(operation_id, "operation")
    services, lines, vehicle_id, notes = _normalise(
        services, lines, fulfilment, vehicle_id, tier, notes)
    payload = {"vehicle": vehicle_id, "tier": tier, "services": services,
               "products": lines, "fulfilment": fulfilment, "notes": notes}
    fingerprint = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with canonical_db() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("""SELECT id,customer_id,payload_fingerprint
            FROM hq_combined_baskets WHERE operation_id=?""", (operation_id,)).fetchone()
        if prior:
            if prior["customer_id"] != customer_id or prior["payload_fingerprint"] != fingerprint:
                raise ValueError("Request ID already used for different basket contents")
            return prior["id"]
        if db.execute("SELECT 1 FROM hq_customers WHERE id=?", (customer_id,)).fetchone() is None:
            raise ValueError("Customer account not found")
        if vehicle_id and db.execute(
                "SELECT 1 FROM hq_vehicles WHERE id=? AND customer_id=?",
                (vehicle_id, customer_id)).fetchone() is None:
            raise ValueError("Vehicle is not linked to your account")
        hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        if db.execute("""SELECT COUNT(*) FROM hq_combined_baskets
            WHERE customer_id=? AND created_at>?""", (customer_id, hour_ago)).fetchone()[0] >= 5:
            raise ValueError("Too many basket requests; contact Tommy Tints")
        # Only RETAIL reference prices are exposed; trade/maintenance are private.
        priced = {}
        if services:
            placeholders = ",".join("?" for _ in services)
            priced = {row["service"]: row["retail_pence"] for row in db.execute(
                "SELECT service,retail_pence FROM hq_service_prices WHERE published=1 AND service IN (" +
                placeholders + ")", services)}
            if any(s not in priced or priced[s] is None for s in services):
                raise ValueError("A selected service is no longer available")
        service_total = sum(priced[s] for s in services)
        # Commerce catalogue remains in its EXISTING durable SQLite store. Only
        # the canonical customer enquiry and its snapshot live in HQ records.
        product = basket_quote(lines, fulfilment) if lines else {
            "items": [], "subtotal_pence": 0, "delivery_pence": 0}
        subtotal = product["subtotal_pence"]
        delivery = product["delivery_pence"]
        total = service_total + subtotal + delivery
        if total > 100000000:
            raise ValueError("Basket reference total exceeds the review limit")
        now, ident = _now(), uuid.uuid4().hex
        db.execute("""INSERT INTO hq_combined_baskets
            (id,operation_id,payload_fingerprint,customer_id,vehicle_id,
             requested_tier,services_json,products_json,fulfilment,service_retail_pence,
             product_retail_pence,delivery_pence,indicative_total_pence,notes,
             status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'submitted',?,?)""", (
                ident, operation_id, fingerprint, customer_id, vehicle_id, tier,
                json.dumps([{"service": s, "retail_pence": priced[s]} for s in services]),
                json.dumps(product["items"]), fulfilment, service_total,
                subtotal, delivery, total, notes, now, now))
        db.execute("INSERT INTO hq_combined_basket_audit VALUES (?,?,?,?,?,?)",
                   (uuid.uuid4().hex, ident, 0, "submitted", "customer", now))
        return ident


def review_combined(ident, revision, status, approved_pounds, public_note, actor="owner"):
    _valid_id(ident, "basket")
    if type(revision) is not int or not 0 <= revision <= 100000000:
        raise ValueError("Invalid revision")
    if status not in ("reviewing", "quoted", "declined", "closed"):
        raise ValueError("Choose a valid status")
    if not isinstance(public_note, str) or len(public_note.strip()) > 400:
        raise ValueError("Public note must be 400 characters or fewer")
    note = public_note.strip()
    approved = None
    if status == "quoted":
        if not isinstance(approved_pounds, str) or not re.fullmatch(
                r"[0-9]{1,7}(?:\.[0-9]{1,2})?", approved_pounds):
            raise ValueError("Enter an approved price in pounds")
        approved = int(Decimal(approved_pounds) * 100)
        if not 1 <= approved <= 100000000:
            raise ValueError("Approved price must be greater than zero")
    with canonical_db() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM hq_combined_baskets WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise LookupError("Basket request not found")
        if (row["status"], row["approved_total_pence"], row["public_note"]) == (
                status, approved, note):
            return row["revision"]  # exact retry, no duplicate event
        if row["revision"] != revision:
            raise ValueError("Basket changed elsewhere; reload before editing")
        if row["status"] in ("declined", "closed"):
            raise ValueError("Final requests cannot be reopened")
        now = _now()
        changed = db.execute("""UPDATE hq_combined_baskets
            SET status=?,approved_total_pence=?,public_note=?,revision=revision+1,
                updated_at=? WHERE id=? AND revision=?""",
            (status, approved, note, now, ident, revision))
        if changed.rowcount != 1:
            raise ValueError("Basket changed elsewhere; reload")
        db.execute("INSERT INTO hq_combined_basket_audit VALUES (?,?,?,?,?,?)",
                   (uuid.uuid4().hex, ident, revision+1, status, str(actor)[:100], now))
        return revision + 1


def _list(customer_id=None):
    with canonical_db() as db:
        _tables(db)
        sql = """SELECT b.*,c.name AS customer_name,v.make,v.model,v.registration
            FROM hq_combined_baskets b JOIN hq_customers c ON c.id=b.customer_id
            LEFT JOIN hq_vehicles v ON v.id=b.vehicle_id"""
        if customer_id:
            _valid_id(customer_id, "customer")
            rows = db.execute(sql + " WHERE b.customer_id=? ORDER BY b.created_at DESC LIMIT 100",
                              (customer_id,)).fetchall()
        else:
            rows = db.execute(sql + " ORDER BY b.created_at DESC LIMIT 100").fetchall()
        return [{**dict(r), "services": json.loads(r["services_json"]),
                 "products": json.loads(r["products_json"])} for r in rows]


def _catalogue(customer_id):
    with product_database() as db:
        products = [dict(x) for x in db.execute("""SELECT id,name,variant,retail,stock
            FROM products WHERE published=1 AND stock>0 ORDER BY name,variant LIMIT 200""")]
    with canonical_db() as db:
        vehicles = [dict(x) for x in db.execute("""SELECT id,make,model,registration
            FROM hq_vehicles WHERE customer_id=? ORDER BY created_at DESC""", (customer_id,))]
    return products, vehicles


@combined_baskets.after_request
def private_cache(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@combined_baskets.route("/book/basket", methods=["GET", "POST"])
def customer_basket():
    customer_id = active_customer()
    if not customer_id:
        return redirect("/customer/login")
    error = None
    if request.method == "POST":
        if request.content_length is None or request.content_length > 12000:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""),
                                      session.get("customer_portal_csrf", "missing")):
            abort(403)
        try:
            lines = []
            for key in request.form:
                if key.startswith("quantity_"):
                    if len(request.form.getlist(key)) != 1:
                        raise ValueError("Duplicate product field")
                    value = request.form[key]
                    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,2})", value):
                        raise ValueError("Invalid product quantity")
                    if int(value):
                        lines.append({"id": key[9:], "quantity": int(value)})
            ident = submit_combined(
                customer_id, request.form.get("operation_id"),
                request.form.get("vehicle_id"), request.form.get("requested_tier"),
                request.form.getlist("services"), lines,
                request.form.get("fulfilment"), request.form.get("notes", ""))
            return redirect("/customer/basket-requests?submitted=" + ident, code=303)
        except (ValueError, LookupError) as exc:
            error = str(exc)
        except (sqlite3.Error, OSError, RuntimeError):
            current_app.logger.exception("Combined basket could not be saved")
            abort(503)
    try:
        products, vehicles = _catalogue(customer_id)
        return render_template(
            "customer_combined_basket.html", products=products,
            vehicles=vehicles, services=public_services(),
            csrf=session.get("customer_portal_csrf", ""),
            operation_id=uuid.uuid4().hex, error=error), (409 if error else 200)
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Combined basket catalogue unavailable")
        abort(503)


@combined_baskets.get("/customer/basket-requests")
def customer_history():
    customer_id = active_customer()
    if not customer_id:
        return redirect("/customer/login")
    try:
        return render_template("customer_combined_history.html", enquiries=_list(customer_id))
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Combined basket history unavailable")
        abort(503)


@combined_baskets.route("/hq/combined-enquiries", methods=["GET", "POST"])
@required
def owner_inbox():
    error = None
    if request.method == "POST":
        if request.content_length is None or request.content_length > 3000:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""),
                                      session.get("csrf", "missing")):
            abort(403)
        raw = request.form.get("revision", "")
        if not _REV.fullmatch(raw):
            abort(400)
        try:
            review_combined(request.form.get("enquiry_id"), int(raw),
                            request.form.get("status"), request.form.get("approved_pounds", ""),
                            request.form.get("public_note", ""),
                            session.get("google_sub", "owner"))
            return redirect("/hq/combined-enquiries?saved=1", code=303)
        except (ValueError, LookupError) as exc:
            error = str(exc)
        except (sqlite3.Error, OSError, RuntimeError):
            current_app.logger.exception("Combined owner review unavailable")
            abort(503)
    try:
        return render_template("hq_combined_enquiries.html", enquiries=_list(),
                               csrf=session.get("csrf", ""), error=error), (409 if error else 200)
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Combined owner inbox unavailable")
        abort(503)
