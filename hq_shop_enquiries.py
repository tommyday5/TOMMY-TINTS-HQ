"""Customer-authenticated, owner-reviewed retail basket enquiries.

Uses the same canonical HQ customer IDs and durable customer_requests.sqlite3
as native work. This is not checkout, a booking, an invoice, or a stock reservation.
Existing product catalogue/pricing remains managed by hq_commerce.
"""
import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, session
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_core_records import _schema
from hq_customer_portal import active_customer
from hq_commerce import basket_quote

shop_enquiries = Blueprint("shop_enquiries", __name__)
HEX = re.compile(r"[0-9a-f]{32}\Z")
STAGES = ("submitted", "reviewing", "quoted", "declined", "closed")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_shop_enquiries (
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE,
        request_hash TEXT NOT NULL,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        items_json TEXT NOT NULL,
        fulfilment TEXT NOT NULL CHECK (fulfilment IN ('collection','delivery')),
        subtotal_pence INTEGER NOT NULL,
        delivery_pence INTEGER NOT NULL,
        indicative_total_pence INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('submitted','reviewing','quoted','declined','closed')),
        approved_total_pence INTEGER,
        public_note TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_shop_enquiries_customer
        ON hq_shop_enquiries(customer_id,created_at)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_shop_enquiry_audit (
        id TEXT PRIMARY KEY, enquiry_id TEXT NOT NULL REFERENCES hq_shop_enquiries(id),
        revision INTEGER NOT NULL, action TEXT NOT NULL,
        actor TEXT NOT NULL, created_at TEXT NOT NULL
    )""")


def _database():
    if not storage_ready():
        raise RuntimeError("Persistent HQ storage is unavailable")
    db = connect()
    db.execute("PRAGMA foreign_keys=ON")
    _schema(db)
    _tables(db)
    return db


def _hex(value, description):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError("Invalid " + description)
    return value


def _lines(lines, fulfilment):
    if fulfilment not in ("collection", "delivery"):
        raise ValueError("Choose collection or delivery")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 30:
        raise ValueError("Choose 1 to 30 products")
    result = []
    seen = set()
    for line in lines:
        if not isinstance(line, dict):
            raise ValueError("Invalid basket item")
        ident = _hex(line.get("id"), "product ID")
        qty = line.get("quantity")
        if type(qty) is not int or not 1 <= qty <= 100 or ident in seen:
            raise ValueError("Invalid or duplicate basket quantity")
        seen.add(ident)
        result.append({"id": ident, "quantity": qty})
    return sorted(result, key=lambda item: item["id"])


def create_enquiry(customer_id, operation_id, lines, fulfilment):
    """Transactionally save a server-priced snapshot, never reserve stock or charge."""
    _hex(customer_id, "HQ customer")
    _hex(operation_id, "operation ID")
    normal = _lines(lines, fulfilment)
    fingerprint = hashlib.sha256(json.dumps(
        {"items": normal, "fulfilment": fulfilment}, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    with _database() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("""SELECT id,customer_id,request_hash FROM hq_shop_enquiries
            WHERE operation_id=?""", (operation_id,)).fetchone()
        if existing:
            if existing["customer_id"] != customer_id or existing["request_hash"] != fingerprint:
                raise ValueError("This request ID was used for different basket contents")
            return existing["id"]
        if not db.execute("SELECT 1 FROM hq_customers WHERE id=?", (customer_id,)).fetchone():
            raise LookupError("Customer account not found")
        hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        count = db.execute("""SELECT count(*) FROM hq_shop_enquiries
            WHERE customer_id=? AND created_at>?""", (customer_id, hour_ago)).fetchone()[0]
        if count >= 5:
            raise ValueError("Too many basket enquiries. Contact Tommy Tints.")
        # Only server-owned published retail prices. Do not trust browser totals or tiers.
        quote = basket_quote(normal, fulfilment)
        ident, timestamp = uuid.uuid4().hex, _now()
        db.execute("""INSERT INTO hq_shop_enquiries
            (id,operation_id,request_hash,customer_id,items_json,fulfilment,
             subtotal_pence,delivery_pence,indicative_total_pence,status,
             created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,'submitted',?,?)""",
            (ident, operation_id, fingerprint, customer_id,
             json.dumps(quote["items"], separators=(",", ":")), fulfilment,
             quote["subtotal_pence"], quote["delivery_pence"], quote["total_pence"],
             timestamp, timestamp))
        db.execute("INSERT INTO hq_shop_enquiry_audit VALUES (?,?,?,?,?,?)",
                   (uuid.uuid4().hex, ident, 0, "submitted", "customer", timestamp))
        return ident


def _pence(raw):
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,2})?", raw):
        raise ValueError("Enter a valid approved total in pounds")
    try:
        amount = Decimal(raw) * 100
    except InvalidOperation:
        raise ValueError("Invalid approved total")
    if amount > 100000000:
        raise ValueError("Approved total is too large")
    return int(amount)


def owner_review(enquiry_id, revision, status, approved_pounds, public_note, actor="owner"):
    _hex(enquiry_id, "enquiry")
    if type(revision) is not int or not 0 <= revision <= 100000000:
        raise ValueError("Invalid revision")
    if status not in ("reviewing", "quoted", "declined", "closed"):
        raise ValueError("Invalid review status")
    if not isinstance(public_note, str) or len(public_note.strip()) > 400:
        raise ValueError("Public note exceeds 400 characters")
    public_note = public_note.strip()
    approved = _pence(approved_pounds) if status == "quoted" else None
    with _database() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM hq_shop_enquiries WHERE id=?", (enquiry_id,)).fetchone()
        if not existing:
            raise LookupError("Basket enquiry not found")
        if (existing["status"], existing["approved_total_pence"], existing["public_note"]) == (
                status, approved, public_note):
            return existing["revision"]
        if existing["revision"] != revision:
            raise ValueError("Enquiry was updated elsewhere; reload before editing")
        if existing["status"] in ("closed", "declined"):
            raise ValueError("Closed or declined enquiries cannot be reopened")
        timestamp = _now()
        db.execute("""UPDATE hq_shop_enquiries SET status=?,approved_total_pence=?,
            public_note=?,revision=revision+1,updated_at=?
            WHERE id=? AND revision=?""", (status, approved, public_note,
                                           timestamp, enquiry_id, revision))
        db.execute("INSERT INTO hq_shop_enquiry_audit VALUES (?,?,?,?,?,?)",
                   (uuid.uuid4().hex, enquiry_id, revision + 1, status,
                    actor[:100], timestamp))
        return revision + 1


def enquiries_for_customer(customer_id):
    _hex(customer_id, "customer")
    with _database() as db:
        rows = db.execute("""SELECT id,items_json,fulfilment,indicative_total_pence,
            status,approved_total_pence,public_note,created_at FROM hq_shop_enquiries
            WHERE customer_id=? ORDER BY created_at DESC LIMIT 100""", (customer_id,)).fetchall()
        return [{**dict(row), "items": json.loads(row["items_json"])} for row in rows]


def enquiries_for_owner():
    with _database() as db:
        rows = db.execute("""SELECT e.*,c.name AS customer_name FROM hq_shop_enquiries e
            JOIN hq_customers c ON c.id=e.customer_id
            ORDER BY e.created_at DESC LIMIT 100""").fetchall()
        return [{**dict(row), "items": json.loads(row["items_json"])} for row in rows]


@shop_enquiries.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@shop_enquiries.post("/api/shop/enquiries")
def submit():
    customer_id = active_customer()
    if not customer_id:
        return jsonify(error="Sign in to request an owner-reviewed quote"), 401
    if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""),
                                  session.get("customer_portal_csrf", "missing")):
        abort(403)
    if not request.is_json or request.content_length is None or request.content_length > 8000:
        return jsonify(error="Invalid request body"), 400
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="Invalid request body"), 400
    try:
        ident = create_enquiry(customer_id, payload.get("operation_id"),
                               payload.get("lines"), payload.get("fulfilment"))
        return jsonify(id=ident, status="submitted", payment_collected=False,
                       booking_created=False), 201
    except (ValueError, LookupError) as exc:
        return jsonify(error=str(exc)), 409
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Unable to save basket enquiry")
        abort(503)


@shop_enquiries.get("/customer/shop/enquiries")
def customer_index():
    customer_id = active_customer()
    if not customer_id:
        return redirect("/customer/login")
    try:
        return render_template("customer_shop_enquiries.html",
                               enquiries=enquiries_for_customer(customer_id))
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Customer basket enquiries unavailable")
        abort(503)


@shop_enquiries.route("/hq/shop/enquiries", methods=["GET", "POST"])
@required
def owner_index():
    if request.method == "POST":
        if request.content_length is None or request.content_length > 3000:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""),
                                      session.get("csrf", "missing")):
            abort(403)
        raw_revision = request.form.get("revision", "")
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})", raw_revision):
            abort(400)
        try:
            owner_review(request.form.get("enquiry_id"), int(raw_revision),
                         request.form.get("status"), request.form.get("approved_pounds", ""),
                         request.form.get("public_note", ""),
                         actor=session.get("google_sub", "owner"))
            return redirect("/hq/shop/enquiries?saved=1", code=303)
        except (ValueError, LookupError) as exc:
            error = str(exc)
        except (sqlite3.Error, OSError, RuntimeError):
            current_app.logger.exception("Unable to review basket enquiry")
            abort(503)
    else:
        error = None
    try:
        return render_template("hq_shop_enquiries.html", enquiries=enquiries_for_owner(),
                               csrf=session.get("csrf"), error=error), (409 if error else 200)
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("HQ basket enquiries unavailable")
        abort(503)
