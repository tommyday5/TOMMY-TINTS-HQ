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
from datetime import date, datetime, timezone

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
    db.execute("""CREATE TABLE IF NOT EXISTS hq_product_order_handover (
        order_id TEXT PRIMARY KEY REFERENCES hq_product_orders(id),
        state TEXT NOT NULL CHECK(state IN ('recorded','disputed')),
        evidence_type TEXT NOT NULL CHECK(evidence_type IN
            ('collection_confirmation','carrier_handover_receipt')),
        evidence_reference TEXT NOT NULL,
        handover_date TEXT NOT NULL,
        public_note TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_product_order_handover_events (
        id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES hq_product_orders(id),
        revision INTEGER NOT NULL,
        state TEXT NOT NULL,
        evidence_type TEXT NOT NULL,
        evidence_reference TEXT NOT NULL,
        handover_date TEXT NOT NULL,
        public_note TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(order_id,revision)
    )""")

    # Append-only planning ledger. An amount is a proposed deposit, never a receipt.
    db.execute("""CREATE TABLE IF NOT EXISTS hq_product_order_deposit_events (
        order_id TEXT NOT NULL REFERENCES hq_product_orders(id),
        revision INTEGER NOT NULL CHECK(revision>0),
        order_revision INTEGER NOT NULL,
        proposed_pence INTEGER CHECK(proposed_pence IS NULL OR proposed_pence>=0),
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        PRIMARY KEY(order_id,revision)
    )""")

    db.execute("""CREATE TABLE IF NOT EXISTS hq_product_deposit_acknowledgements (
        order_id TEXT NOT NULL,
        deposit_revision INTEGER NOT NULL,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        acknowledged_at TEXT NOT NULL,
        PRIMARY KEY(order_id,deposit_revision),
        FOREIGN KEY(order_id,deposit_revision)
            REFERENCES hq_product_order_deposit_events(order_id,revision)
    )""")


def acknowledge_deposit_plan(basket_id, customer_id, order_id, deposit_revision):
    """Record viewing a specific current proposal, not agreement or payment."""
    for ident in (basket_id, customer_id, order_id):
        _id(ident)
    if type(deposit_revision) is not int or not 1 <= deposit_revision < 100000000:
        raise ValueError("Invalid deposit revision")
    with canonical_db() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        proceed = db.execute("""SELECT * FROM hq_product_proceed_requests
            WHERE basket_id=? AND customer_id=?""", (basket_id,customer_id)).fetchone()
        if not proceed:
            raise LookupError("Product request not found")
        order = db.execute("""SELECT * FROM hq_product_orders
            WHERE id=? AND request_id=? AND customer_id=? AND quote_id=?""",
            (order_id,proceed["id"],customer_id,proceed["quote_id"])).fetchone()
        if not order:
            raise LookupError("Product order not found")
        deposit = db.execute("""SELECT * FROM hq_product_order_deposit_events
            WHERE order_id=? ORDER BY revision DESC LIMIT 1""", (order_id,)).fetchone()
        if (proceed["status"] != "approved" or order["status"] != "open"
                or order["quote_revision"] != proceed["quote_revision"]
                or not deposit or deposit["revision"] != deposit_revision
                or deposit["order_revision"] != order["revision"]
                or deposit["proposed_pence"] is None
                or not 0 <= deposit["proposed_pence"] <= order["approved_total_pence"]
                or db.execute("SELECT 1 FROM hq_product_order_handover WHERE order_id=?",
                              (order_id,)).fetchone()):
            raise ValueError("Deposit proposal changed or is unavailable; reload")
        quote = _approved_unchanged_quote(db, proceed)
        if quote["approved_total_pence"] != order["approved_total_pence"]:
            raise ValueError("Deposit proposal changed or is unavailable; reload")
        db.execute("""INSERT INTO hq_product_deposit_acknowledgements
            (order_id,deposit_revision,customer_id,acknowledged_at)
            VALUES(?,?,?,?) ON CONFLICT(order_id,deposit_revision) DO NOTHING""",
            (order_id,deposit_revision,customer_id,_now()))


def owner_save_deposit_plan(ident, expected_revision, expected_order_revision, amount, actor):
    """Save or clear a proposed deposit; never collect or allocate funds."""
    _id(ident)
    if any(type(v) is not int or not 0 <= v < 100000000
           for v in (expected_revision, expected_order_revision)):
        raise ValueError("Invalid deposit plan revision")
    if not isinstance(amount, str) or (amount and not re.fullmatch(
            r"[0-9]{1,7}(?:\.[0-9]{1,2})?", amount)):
        raise ValueError("Enter a non-negative GBP amount or leave blank to clear")
    proposed = None
    if amount:
        whole, _, fraction = amount.partition(".")
        proposed = int(whole)*100 + int(fraction.ljust(2, "0"))
    fingerprint = _actor_fingerprint(actor)
    with canonical_db() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        proceed = db.execute("SELECT * FROM hq_product_proceed_requests WHERE id=?",
                             (ident,)).fetchone()
        order = db.execute("SELECT * FROM hq_product_orders WHERE request_id=?",
                           (ident,)).fetchone()
        if not proceed or not order:
            raise LookupError("Product order not found")
        if (proceed["status"] != "approved" or order["status"] != "open"
                or order["revision"] != expected_order_revision
                or order["customer_id"] != proceed["customer_id"]
                or order["quote_id"] != proceed["quote_id"]
                or order["quote_revision"] != proceed["quote_revision"]
                or db.execute("SELECT 1 FROM hq_product_order_handover WHERE order_id=?",
                              (order["id"],)).fetchone()):
            raise ValueError("Only current approved open orders without handover can be planned")
        quote = _approved_unchanged_quote(db, proceed)
        if (order["approved_total_pence"] != quote["approved_total_pence"]
                or (proposed is not None and proposed > order["approved_total_pence"])):
            raise ValueError("Proposed deposit exceeds or conflicts with the approved order total")
        prior = db.execute("""SELECT * FROM hq_product_order_deposit_events
            WHERE order_id=? ORDER BY revision DESC LIMIT 1""", (order["id"],)).fetchone()
        revision = prior["revision"] if prior else 0
        if (prior and prior["order_revision"] == expected_order_revision
                and prior["proposed_pence"] == proposed and expected_revision <= revision):
            return revision
        if revision != expected_revision:
            raise ValueError("Deposit plan changed; reload before saving")
        db.execute("""INSERT INTO hq_product_order_deposit_events
            (order_id,revision,order_revision,proposed_pence,actor_fingerprint,occurred_at)
            VALUES (?,?,?,?,?,?)""", (order["id"], revision+1, expected_order_revision,
                                      proposed, fingerprint, _now()))
        return revision+1


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


def owner_save_product_handover(ident, expected_revision, action, evidence_type,
                                evidence_reference, handover_date, public_note, actor):
    """Owner-entered, append-audited handover evidence. No provider verification.

    Original evidence survives a dispute. Cannot make a second handover claim,
    take payment, dispatch anything or alter inventory from this workflow.
    """
    _id(ident)
    if type(expected_revision) is not int or not 0 <= expected_revision < 100000000:
        raise ValueError("Invalid handover revision")
    if action not in ("record", "dispute"):
        raise ValueError("Invalid handover action")
    if not isinstance(public_note, str) or len(public_note.strip()) > 400:
        raise ValueError("Customer-visible handover note is too long")
    public_note = public_note.strip()
    if action == "dispute" and len(public_note) < 10:
        raise ValueError("Explain the handover dispute in at least 10 characters")
    fingerprint = _actor_fingerprint(actor)
    with canonical_db() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        order = db.execute("""SELECT o.*, p.basket_id, p.status AS request_status
            FROM hq_product_orders o
            JOIN hq_product_proceed_requests p ON p.id=o.request_id
            WHERE o.request_id=?""", (ident,)).fetchone()
        if not order:
            raise LookupError("Product order not found")
        prior = db.execute("""SELECT * FROM hq_product_order_handover
            WHERE order_id=?""", (order["id"],)).fetchone()
        if action == "record":
            required_type = ("collection_confirmation" if order["fulfilment"] == "collection"
                             else "carrier_handover_receipt")
            if evidence_type != required_type:
                raise ValueError("Evidence type does not match the approved fulfilment method")
            if not isinstance(evidence_reference, str) or not (
                    3 <= len(evidence_reference.strip()) <= 120):
                raise ValueError("Enter a 3–120 character private evidence reference")
            evidence_reference = evidence_reference.strip()
            if not isinstance(handover_date, str) or not re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}", handover_date):
                raise ValueError("Enter a valid handover date")
            try:
                actual_date = date.fromisoformat(handover_date)
            except ValueError as exc:
                raise ValueError("Enter a valid handover date") from exc
            if actual_date > datetime.now(timezone.utc).date():
                raise ValueError("Handover cannot be recorded in the future")
            if handover_date < order["created_at"][:10]:
                raise ValueError("Handover cannot predate native order creation")
            if prior and (prior["state"] == "recorded"
                          and prior["evidence_type"] == evidence_type
                          and prior["evidence_reference"] == evidence_reference
                          and prior["handover_date"] == handover_date
                          and prior["public_note"] == public_note):
                return prior["revision"]  # exact double-click/retry
            if prior:
                raise ValueError("Handover already recorded; dispute it rather than overwrite evidence")
            if expected_revision != 0:
                raise ValueError("Handover changed; reload before saving")
            if order["status"] != "open" or order["request_status"] != "approved":
                raise ValueError("Only open approved orders can record handover")
            _approved_unchanged_quote(db, order)
            state, revision = "recorded", 1
        else:
            if not prior:
                raise ValueError("No handover evidence exists to dispute")
            if prior["state"] == "disputed" and prior["public_note"] == public_note:
                return prior["revision"]
            if prior["revision"] != expected_revision:
                raise ValueError("Handover changed; reload before saving")
            if prior["state"] != "recorded":
                raise ValueError("Handover already disputed")
            state, revision = "disputed", expected_revision + 1
            evidence_type = prior["evidence_type"]
            evidence_reference = prior["evidence_reference"]
            handover_date = prior["handover_date"]
        now = _now()
        if action == "record":
            db.execute("""INSERT INTO hq_product_order_handover
                (order_id,state,evidence_type,evidence_reference,handover_date,
                 public_note,revision,updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                (order["id"], state, evidence_type, evidence_reference,
                 handover_date, public_note, revision, now))
        else:
            update = db.execute("""UPDATE hq_product_order_handover
                SET state=?, public_note=?,revision=?,updated_at=?
                WHERE order_id=? AND revision=?""",
                (state, public_note, revision, now, order["id"], expected_revision))
            if update.rowcount != 1:
                raise ValueError("Handover changed; reload before saving")
        db.execute("""INSERT INTO hq_product_order_handover_events
            (id,order_id,revision,state,evidence_type,evidence_reference,
             handover_date,public_note,actor_fingerprint,occurred_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, order["id"], revision, state, evidence_type,
             evidence_reference, handover_date, public_note, fingerprint, now))
        return revision


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
            o.id AS order_id,o.status AS order_status,o.public_note AS order_note,
            h.state AS handover_state, h.handover_date, h.public_note AS handover_note,
            CASE WHEN q.status='approved' AND q.revision=p.quote_revision
                AND o.quote_revision=q.revision AND o.status='open'
                AND o.approved_total_pence=q.approved_total_pence
                AND h.state IS NULL AND d.order_revision=o.revision
                AND d.proposed_pence BETWEEN 0 AND o.approved_total_pence
                THEN d.proposed_pence ELSE NULL END AS proposed_deposit_pence,
            o.approved_total_pence AS order_total_pence,d.revision AS deposit_revision,
            a.acknowledged_at AS deposit_acknowledged_at
            FROM hq_product_proceed_requests p
            JOIN hq_combined_product_quotes q ON q.id=p.quote_id
            LEFT JOIN hq_product_preparation f ON f.request_id=p.id
            LEFT JOIN hq_product_orders o ON o.request_id=p.id
                AND o.customer_id=p.customer_id AND o.quote_id=p.quote_id
            LEFT JOIN hq_product_order_handover h ON h.order_id=o.id
            LEFT JOIN hq_product_order_deposit_events d ON d.order_id=o.id
                AND d.revision=(SELECT MAX(revision) FROM hq_product_order_deposit_events
                                WHERE order_id=o.id)
            LEFT JOIN hq_product_deposit_acknowledgements a ON a.order_id=o.id
                AND a.deposit_revision=d.revision AND a.customer_id=p.customer_id
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
                if row["proposed_deposit_pence"] is not None:
                    result["deposit_revision"] = row["deposit_revision"]
                    result["deposit_acknowledged"] = bool(row["deposit_acknowledged_at"])
                    result["proposed_deposit_pence"] = row["proposed_deposit_pence"]
                    result["conditional_remainder_pence"] = (
                        row["order_total_pence"] - row["proposed_deposit_pence"])
                if row["handover_state"]:
                    result["handover_state"] = row["handover_state"]
                    result["handover_date"] = row["handover_date"]
                    result["handover_note"] = row["handover_note"]
        return result


def owner_proceed_requests():
    """Owner-only inbox. Read-only query, no external order creation."""
    with canonical_db() as db:
        _tables(db)
        rows = [dict(row) for row in db.execute("""
            SELECT p.id,p.basket_id,p.customer_id,p.quote_id,p.status,
                p.revision,p.public_note,p.created_at,p.updated_at,
                c.name AS customer_name, q.status AS quote_status,
                q.approved_total_pence,
                COALESCE(f.stage,'not_started') AS preparation_stage,
                COALESCE(f.public_note,'') AS preparation_note,
                COALESCE(f.revision,0) AS preparation_revision,
                o.id AS order_id,o.status AS order_status,
                o.fulfilment AS order_fulfilment,
                o.public_note AS order_note,COALESCE(o.revision,0) AS order_revision,
                h.state AS handover_state,h.evidence_type AS handover_evidence_type,
                h.evidence_reference AS handover_evidence_reference,
                h.handover_date,h.public_note AS handover_note,
                COALESCE(h.revision,0) AS handover_revision,
                COALESCE(d.revision,0) AS deposit_revision,
                d.order_revision AS deposit_order_revision,d.proposed_pence AS proposed_deposit_pence
            FROM hq_product_proceed_requests p
            JOIN hq_combined_product_quotes q ON q.id=p.quote_id
            JOIN hq_customers c ON c.id=p.customer_id
            LEFT JOIN hq_product_preparation f ON f.request_id=p.id
            LEFT JOIN hq_product_orders o ON o.request_id=p.id
                AND o.customer_id=p.customer_id AND o.quote_id=p.quote_id
            LEFT JOIN hq_product_order_handover h ON h.order_id=o.id
            LEFT JOIN hq_product_order_deposit_events d ON d.order_id=o.id
                AND d.revision=(SELECT MAX(revision) FROM hq_product_order_deposit_events
                                WHERE order_id=o.id)
            ORDER BY CASE p.status WHEN 'pending' THEN 0 ELSE 1 END,
                     p.created_at DESC LIMIT 100""")]
        for item in rows:
            item["deposit_history"] = (
                [dict(event) for event in db.execute("""SELECT d.revision,d.order_revision,
                    d.proposed_pence,d.occurred_at,a.acknowledged_at
                    FROM hq_product_order_deposit_events d
                    LEFT JOIN hq_product_deposit_acknowledgements a ON a.order_id=d.order_id
                        AND a.deposit_revision=d.revision AND a.customer_id=?
                    WHERE d.order_id=? ORDER BY d.revision DESC""", (item["customer_id"],item["order_id"],))]
                if item["order_id"] else [])
            item["handover_history"] = (
                [dict(event) for event in db.execute("""
                    SELECT revision,state,handover_date,public_note,occurred_at
                    FROM hq_product_order_handover_events
                    WHERE order_id=? ORDER BY revision DESC""", (item["order_id"],))]
                if item["order_id"] else [])
        return rows


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


@product_proceed.post("/customer/basket-requests/<basket_id>/acknowledge-deposit")
def customer_acknowledge_deposit(basket_id):
    customer_id = active_customer()
    if not customer_id:
        return redirect("/customer/login")
    if request.content_length is None or request.content_length > 1600:
        abort(413)
    if not secrets.compare_digest(request.form.get("csrf", ""),
                                  session.get("customer_portal_csrf", "missing")):
        abort(403)
    raw = request.form.get("deposit_revision", "")
    if not _REV.fullmatch(raw):
        abort(400)
    try:
        acknowledge_deposit_plan(basket_id, customer_id, request.form.get("order_id"), int(raw))
    except LookupError:
        abort(404)
    except ValueError as exc:
        return str(exc), 409
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.warning("Customer deposit acknowledgement unavailable")
        abort(503)
    return redirect("/customer/basket-requests", code=303)


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
            elif request.form.get("action") == "deposit_plan":
                order_revision = request.form.get("order_revision", "")
                if not _REV.fullmatch(order_revision):
                    abort(400)
                owner_save_deposit_plan(request.form.get("request_id"), int(raw),
                    int(order_revision), request.form.get("deposit_amount", ""),
                    session.get("google_sub", ""))
            elif request.form.get("action") in ("handover_record", "handover_dispute"):
                owner_save_product_handover(
                    request.form.get("request_id"), int(raw),
                    ("record" if request.form.get("action") == "handover_record"
                     else "dispute"),
                    request.form.get("evidence_type", ""),
                    request.form.get("evidence_reference", ""),
                    request.form.get("handover_date", ""),
                    request.form.get("handover_note", ""),
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
