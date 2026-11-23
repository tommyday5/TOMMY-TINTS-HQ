"""Customer-to-owner product quote proceed requests, without placing an order.

Uses the same persistent HQ quote/customer IDs as /book. A customer's request
to proceed is not acceptance of payment, stock reservation, an order, invoice,
dispatch or a booking. Only an authenticated owner can review it.
"""
import hashlib
import hmac
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone

from flask import (Blueprint, abort, current_app, redirect, render_template,
                   request, session)
from hq_auth import required
from hq_customer_portal import active_customer
from hq_product_quote_lines import _load, _tables as quote_tables
from hq_service_quotes import _db as canonical_db
from hq_work_progress import _actor_fingerprint

product_proceed = Blueprint("product_proceed", __name__)
_ID = re.compile(r"[0-9a-f]{32}\Z")
_REV = re.compile(r"(?:0|[1-9][0-9]{0,8})\Z")
_STATUSES = ("pending", "approved", "declined")
_PREPARATION_STAGES = ("not_started", "planning", "awaiting_customer_details",
                       "preparing", "ready_for_collection", "ready_for_delivery", "paused")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid HQ reference")
    return value


def _tables(db):
    quote_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_product_proceed_requests(
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE,
        basket_id TEXT NOT NULL UNIQUE REFERENCES hq_combined_baskets(id),
        quote_id TEXT NOT NULL UNIQUE REFERENCES hq_combined_product_quotes(id),
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        quote_revision INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','approved','declined')),
        public_note TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_product_proceed_customer
        ON hq_product_proceed_requests(customer_id,created_at)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_product_proceed_events(
        id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL REFERENCES hq_product_proceed_requests(id),
        revision INTEGER NOT NULL,
        status TEXT NOT NULL,
        public_note TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(request_id,revision)
    )""")
    # Separate additive ledger: existing approved requests need no ALTER TABLE or rewrite.
    db.execute("""CREATE TABLE IF NOT EXISTS hq_product_preparation(
        request_id TEXT PRIMARY KEY REFERENCES hq_product_proceed_requests(id),
        stage TEXT NOT NULL CHECK(stage IN
            ('not_started','planning','awaiting_customer_details','preparing',
             'ready_for_collection','ready_for_delivery','paused')),
        public_note TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_product_preparation_events(
        id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL REFERENCES hq_product_proceed_requests(id),
        revision INTEGER NOT NULL,
        stage TEXT NOT NULL,
        public_note TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(request_id,revision)
    )""")

    # Separate native owner-confirmed order ledger. No changes to historical requests.
    db.execute("""CREATE TABLE IF NOT EXISTS hq_product_orders (
        id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL UNIQUE REFERENCES hq_product_proceed_requests(id),
        quote_id TEXT NOT NULL REFERENCES hq_combined_product_quotes(id),
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        quote_revision INTEGER NOT NULL,
        approved_total_pence INTEGER NOT NULL CHECK(approved_total_pence >= 0),
        fulfilment TEXT NOT NULL CHECK(fulfilment IN ('collection','delivery')),
        status TEXT NOT NULL CHECK(status IN ('open','on_hold','closed','cancelled')),
        public_note TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_product_order_events (
        id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES hq_product_orders(id),
        revision INTEGER NOT NULL,
        status TEXT NOT NULL,
        public_note TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(order_id,revision)
    )""")


def _approved_unchanged_quote(db, proceed):
    quote = _load(db, proceed["basket_id"])
    if (quote["id"] != proceed["quote_id"]
            or quote["customer_id"] != proceed["customer_id"]
            or quote["status"] != "approved"
            or quote["revision"] != proceed["quote_revision"]
            or not quote["fully_allocated"]):
        raise ValueError("Quotation changed or withdrawn; order creation is locked")
    return quote


def owner_create_product_order(ident, expected_revision, actor):
    """Explicit owner action creates one native product order from a frozen quote.

    Does not reserve stock, capture payment, mark anything paid, fulfil an order,
    issue an invoice, contact a customer, or write to external systems.
    """
    _id(ident)
    if type(expected_revision) is not int or not 0 <= expected_revision < 100000000:
        raise ValueError("Invalid request revision")
    fingerprint = _actor_fingerprint(actor)
    with canonical_db() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        proceed = db.execute("SELECT * FROM hq_product_proceed_requests WHERE id=?",
                             (ident,)).fetchone()
        if proceed is None:
            raise LookupError("Product proceed request not found")
        existing = db.execute("SELECT id FROM hq_product_orders WHERE request_id=?",
                              (ident,)).fetchone()
        if existing:
            return existing["id"]  # double-click and stale retry create no second order
        if proceed["status"] != "approved":
            raise ValueError("Owner approval is required before creating an order")
        if proceed["revision"] != expected_revision:
            raise ValueError("Request changed; reload before creating an order")
        quote = _approved_unchanged_quote(db, proceed)
        now, order_id = _now(), uuid.uuid4().hex
        db.execute("""INSERT INTO hq_product_orders
            (id,request_id,quote_id,customer_id,quote_revision,
             approved_total_pence,fulfilment,status,public_note,revision,
             created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,'open','',0,?,?)""",
            (order_id, ident, proceed["quote_id"], proceed["customer_id"],
             quote["revision"], quote["approved_total_pence"],
             quote["fulfilment"], now, now))
        db.execute("""INSERT INTO hq_product_order_events
            (id,order_id,revision,status,public_note,actor_fingerprint,occurred_at)
            VALUES (?,?,0,'open','',?,?)""",
            (uuid.uuid4().hex, order_id, fingerprint, now))
        return order_id


def owner_save_product_order(ident, expected_revision, status, public_note, actor):
    """Versioned owner-entered administration status, never a payment signal."""
    _id(ident)
    if type(expected_revision) is not int or not 0 <= expected_revision < 100000000:
        raise ValueError("Invalid order revision")
    if status not in ("open", "on_hold", "closed", "cancelled"):
        raise ValueError("Choose a valid order status")
    if not isinstance(public_note, str) or len(public_note.strip()) > 400:
        raise ValueError("Customer-visible order note is too long")
    public_note = public_note.strip()
    fingerprint = _actor_fingerprint(actor)
    with canonical_db() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT o.*,p.basket_id,p.quote_revision AS request_quote_revision,
            p.quote_id AS request_quote_id,p.status AS request_status
            FROM hq_product_orders o
            JOIN hq_product_proceed_requests p ON p.id=o.request_id
            WHERE o.request_id=?""", (ident,)).fetchone()
        if row is None:
            raise LookupError("Product order not found")
        if row["status"] == status and row["public_note"] == public_note:
            return row["revision"]
        if row["revision"] != expected_revision:
            raise ValueError("Order changed; reload before saving")
        if row["status"] in ("closed", "cancelled"):
            raise ValueError("Closed or cancelled orders cannot be amended")
        if status in ("open", "closed"):
            if row["request_status"] != "approved":
                raise ValueError("Proceed approval no longer valid")
            # Withdrawn/revised quotes may only cause an administrative hold/cancellation.
            _approved_unchanged_quote(db, row)
        now = _now()
        updated = db.execute("""UPDATE hq_product_orders
            SET status=?, public_note=?, revision=revision+1,updated_at=?
            WHERE request_id=? AND revision=?""",
            (status, public_note, now, ident, expected_revision))
        if updated.rowcount != 1:
            raise ValueError("Order changed; reload before saving")
        db.execute("""INSERT INTO hq_product_order_events
            (id,order_id,revision,status,public_note,actor_fingerprint,occurred_at)
            VALUES (?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, row["id"], expected_revision+1, status,
             public_note, fingerprint, now))
        return expected_revision+1


def request_to_proceed(basket_id, customer_id, operation_id):
    """Idempotent authenticated customer request; never touches stock or payments."""
    _id(basket_id)
    _id(customer_id)
    _id(operation_id)
    with canonical_db() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("""SELECT * FROM hq_product_proceed_requests
            WHERE operation_id=?""", (operation_id,)).fetchone()
        if old:
            if old["basket_id"] != basket_id or old["customer_id"] != customer_id:
                raise ValueError("Submission token was previously used")
            return old["id"]
        quote = _load(db, basket_id)
        if quote["customer_id"] != customer_id:
            raise LookupError("Quotation not found")
        if quote["status"] != "approved" or not quote["fully_allocated"]:
            raise ValueError("An itemised owner-approved quotation is required")
        old = db.execute("""SELECT id,customer_id FROM hq_product_proceed_requests
            WHERE quote_id=?""", (quote["id"],)).fetchone()
        if old:
            if old["customer_id"] != customer_id:
                raise LookupError("Quotation not found")
            return old["id"]
        ident, now = uuid.uuid4().hex, _now()
        db.execute("""INSERT INTO hq_product_proceed_requests
            (id,operation_id,basket_id,quote_id,customer_id,quote_revision,
             status,created_at,updated_at)
             VALUES (?,?,?,?,?,?,'pending',?,?)""",
            (ident, operation_id, basket_id, quote["id"], customer_id,
             quote["revision"], now, now))
        db.execute("""INSERT INTO hq_product_proceed_events
            VALUES (?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, ident, 0, "pending", "",
             "customer_request", now))
        return ident


def customer_proceed_state(basket_id, customer_id):
    """Customer-safe allowlist, scoped by exact canonical customer + basket."""
    _id(basket_id)
    _id(customer_id)
    with canonical_db() as db:
        _tables(db)
        row = db.execute("""SELECT p.status,p.public_note,
            COALESCE(f.stage,'not_started') AS preparation_stage,
            COALESCE(f.public_note,'') AS preparation_note,
            o.id AS order_id,o.status AS order_status,o.public_note AS order_note
            FROM hq_product_proceed_requests p
            JOIN hq_combined_product_quotes q ON q.id=p.quote_id
            LEFT JOIN hq_product_preparation f ON f.request_id=p.id
            LEFT JOIN hq_product_orders o ON o.request_id=p.id
                AND o.customer_id=p.customer_id AND o.quote_id=p.quote_id
            WHERE p.basket_id=? AND p.customer_id=?
            AND q.customer_id=p.customer_id""", (basket_id, customer_id)).fetchone()
        if not row:
            return None
        result = {"status": row["status"], "public_note": row["public_note"]}
        if row["status"] == "approved":
            result["preparation_stage"] = row["preparation_stage"]
            result["preparation_note"] = row["preparation_note"]
            if row["order_id"] is not None:
                result["order_id"] = row["order_id"]
                result["order_status"] = row["order_status"]
                result["order_note"] = row["order_note"]
        return result


def owner_proceed_requests():
    """Owner-only inbox. Read-only query, no external order creation."""
    with canonical_db() as db:
        _tables(db)
        return [dict(row) for row in db.execute("""
            SELECT p.id,p.basket_id,p.customer_id,p.quote_id,p.status,
                p.revision,p.public_note,p.created_at,p.updated_at,
                c.name AS customer_name, q.status AS quote_status,
                q.approved_total_pence,
                COALESCE(f.stage,'not_started') AS preparation_stage,
                COALESCE(f.public_note,'') AS preparation_note,
                COALESCE(f.revision,0) AS preparation_revision,
                o.id AS order_id,o.status AS order_status,
                o.public_note AS order_note,COALESCE(o.revision,0) AS order_revision
            FROM hq_product_proceed_requests p
            JOIN hq_combined_product_quotes q ON q.id=p.quote_id
            JOIN hq_customers c ON c.id=p.customer_id
            LEFT JOIN hq_product_preparation f ON f.request_id=p.id
            LEFT JOIN hq_product_orders o ON o.request_id=p.id
                AND o.customer_id=p.customer_id AND o.quote_id=p.quote_id
            ORDER BY CASE p.status WHEN 'pending' THEN 0 ELSE 1 END,
                     p.created_at DESC LIMIT 100""")]


def owner_review_proceed(ident, expected_revision, status, public_note, actor):
    """Owner-only CAS; rechecks original quotation under the same write lock."""
    _id(ident)
    if type(expected_revision) is not int or not 0 <= expected_revision < 100000000:
        raise ValueError("Invalid revision")
    if status not in ("approved", "declined"):
        raise ValueError("Choose owner approval or decline")
    if not isinstance(public_note, str) or len(public_note.strip()) > 400:
        raise ValueError("Customer-visible note is too long")
    public_note = public_note.strip()
    fingerprint = _actor_fingerprint(actor)
    with canonical_db() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM hq_product_proceed_requests WHERE id=?",
                         (ident,)).fetchone()
        if not row:
            raise LookupError("Proceed request not found")
        if row["status"] == status and row["public_note"] == public_note:
            return row["revision"]
        if row["revision"] != expected_revision:
            raise ValueError("Request changed; reload before saving")
        if row["status"] != "pending":
            # Final decisions are evidence; they are not casually reversible.
            raise ValueError("This request already has a final decision")
        quote = _load(db, row["basket_id"])
        if (quote["id"] != row["quote_id"] or
                quote["customer_id"] != row["customer_id"] or
                quote["status"] != "approved" or
                quote["revision"] != row["quote_revision"] or
                not quote["fully_allocated"]):
            raise ValueError("Quotation changed or withdrawn; cannot approve request")
        now = _now()
        result = db.execute("""UPDATE hq_product_proceed_requests
            SET status=?, public_note=?, revision=revision+1, updated_at=?
            WHERE id=? AND revision=? AND status='pending'""",
            (status, public_note, now, ident, expected_revision))
        if result.rowcount != 1:
            raise ValueError("Request changed; reload before saving")
        db.execute("""INSERT INTO hq_product_proceed_events
            VALUES (?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, ident, expected_revision+1, status,
             public_note, fingerprint, now))
        return expected_revision+1


def owner_set_preparation(ident, expected_revision, stage, public_note, actor):
    """Owner-only, versioned product preparation notes; never creates an order or payment.

    A stage is an owner-entered operational statement, not evidence that stock,
    payment, a carrier or a customer confirmation has been independently checked.
    The approved, unchanged quotation remains the approval gate.
    """
    _id(ident)
    if type(expected_revision) is not int or not 0 <= expected_revision < 100000000:
        raise ValueError("Invalid preparation revision")
    if stage not in _PREPARATION_STAGES:
        raise ValueError("Choose a valid preparation stage")
    if not isinstance(public_note, str) or len(public_note.strip()) > 400:
        raise ValueError("Customer-visible preparation note is too long")
    public_note = public_note.strip()
    fingerprint = _actor_fingerprint(actor)
    with canonical_db() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        req = db.execute("SELECT * FROM hq_product_proceed_requests WHERE id=?",
                         (ident,)).fetchone()
        if req is None:
            raise LookupError("Proceed request not found")
        if req["status"] != "approved":
            raise ValueError("Owner approval is required before preparing products")
        quote = _load(db, req["basket_id"])
        if (quote["id"] != req["quote_id"] or
                quote["customer_id"] != req["customer_id"] or
                quote["status"] != "approved" or
                quote["revision"] != req["quote_revision"] or
                not quote["fully_allocated"]):
            raise ValueError("Quotation changed or withdrawn; preparation is locked")
        if stage == "ready_for_collection" and quote["fulfilment"] != "collection":
            raise ValueError("This quotation requires delivery, not collection")
        if stage == "ready_for_delivery" and quote["fulfilment"] != "delivery":
            raise ValueError("This quotation requires collection, not delivery")
        prior = db.execute("SELECT * FROM hq_product_preparation WHERE request_id=?",
                           (ident,)).fetchone()
        revision = prior["revision"] if prior else 0
        if prior and prior["stage"] == stage and prior["public_note"] == public_note:
            return revision  # Safe idempotent retry even if original form is stale.
        if not prior and stage == "not_started" and not public_note:
            return 0
        if revision != expected_revision:
            raise ValueError("Preparation changed; reload before saving")
        now = _now()
        if prior:
            changed = db.execute("""UPDATE hq_product_preparation
                SET stage=?, public_note=?, revision=revision+1, updated_at=?
                WHERE request_id=? AND revision=?""",
                (stage, public_note, now, ident, expected_revision))
            if changed.rowcount != 1:
                raise ValueError("Preparation changed; reload before saving")
        else:
            db.execute("""INSERT INTO hq_product_preparation
                (request_id,stage,public_note,revision,updated_at)
                VALUES (?,?,?,1,?)""", (ident, stage, public_note, now))
        db.execute("""INSERT INTO hq_product_preparation_events
            (id,request_id,revision,stage,public_note,actor_fingerprint,occurred_at)
            VALUES (?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, ident, revision+1, stage, public_note,
             fingerprint, now))
        return revision+1


@product_proceed.after_request
def private_cache(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@product_proceed.post("/customer/basket-requests/<basket_id>/request-proceed")
def customer_request_proceed(basket_id):
    customer_id = active_customer()
    if not customer_id:
        return redirect("/customer/login")
    if request.content_length is None or request.content_length > 1600:
        abort(413)
    if not secrets.compare_digest(request.form.get("csrf", ""),
                                  session.get("customer_portal_csrf", "missing")):
        abort(403)
    try:
        request_to_proceed(basket_id, customer_id, request.form.get("operation_id"))
    except LookupError:
        abort(404)
    except ValueError as exc:
        return str(exc), 409
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Customer proceed request unavailable")
        abort(503)
    return redirect("/customer/basket-requests?proceed=requested", code=303)


@product_proceed.route("/hq/product-proceed-requests", methods=["GET", "POST"])
@required
def owner_inbox():
    error = None
    if request.method == "POST":
        if request.content_length is None or request.content_length > 2000:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""),
                                      session.get("csrf", "missing")):
            abort(403)
        raw = request.form.get("revision", "")
        if not _REV.fullmatch(raw):
            abort(400)
        try:
            if request.form.get("action") == "order_create":
                owner_create_product_order(
                    request.form.get("request_id"), int(raw),
                    session.get("google_sub", ""))
            elif request.form.get("action") == "order_edit":
                owner_save_product_order(
                    request.form.get("request_id"), int(raw),
                    request.form.get("order_status"),
                    request.form.get("order_note", ""),
                    session.get("google_sub", ""))
            elif request.form.get("action") == "preparation":
                owner_set_preparation(
                    request.form.get("request_id"), int(raw),
                    request.form.get("stage"), request.form.get("preparation_note", ""),
                    session.get("google_sub", ""))
            elif request.form.get("action", "review") == "review":
                owner_review_proceed(
                    request.form.get("request_id"), int(raw),
                    request.form.get("status"), request.form.get("public_note", ""),
                    session.get("google_sub", ""))
            else:
                raise ValueError("Unknown product request action")
            return redirect("/hq/product-proceed-requests?saved=1", code=303)
        except (ValueError, LookupError) as exc:
            error = str(exc)
        except (sqlite3.Error, OSError, RuntimeError):
            current_app.logger.exception("Product proceed review unavailable")
            abort(503)
    try:
        return render_template("hq_product_proceed_requests.html",
                               rows=owner_proceed_requests(),
                               csrf=session.get("csrf", ""),
                               error=error), (409 if error else 200)
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Product proceed inbox unavailable")
        abort(503)
