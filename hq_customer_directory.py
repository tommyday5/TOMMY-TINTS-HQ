"""Owner-only canonical HQ customer directory and customer work history.

Read-only views of HQ's existing SQLite records. Search never queries Sheets,
touches providers, or exposes customer data to public bookings clients.
"""
import re
import secrets
import sqlite3
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for
from customer_request_store import connect
from hq_auth import required
from hq_core_records import _schema
from customer_edits import _tables as customer_edit_tables
from hq_work_progress import _actor_fingerprint

customer_directory = Blueprint("customer_directory", __name__)
HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
PAGE_SIZE = 25


def find_customers(query="", offset=0):
    """A single-snapshot, bounded customer/registration lookup."""
    if not isinstance(query, str) or len(query) > 80:
        raise ValueError("Enter a search of 80 characters or fewer")
    if type(offset) is not int or not 0 <= offset <= 100000:
        raise ValueError("Invalid page offset")
    term = query.strip()
    clause = ""
    args = []
    if term:
        clause = """ WHERE (
            instr(lower(c.name),lower(?))>0 OR
            instr(lower(c.email),lower(?))>0 OR
            instr(lower(c.phone),lower(?))>0 OR
            EXISTS(SELECT 1 FROM hq_vehicles v WHERE v.customer_id=c.id
                   AND instr(lower(v.registration),lower(?))>0))"""
        args = [term] * 4
    with connect() as db:
        _schema(db)
        db.execute("BEGIN")
        total = db.execute("SELECT count(*) FROM hq_customers c" + clause, args).fetchone()[0]
        rows = db.execute("""SELECT c.id,c.name,c.email,c.phone,
            (SELECT count(*) FROM hq_vehicles v WHERE v.customer_id=c.id) AS vehicles,
            (SELECT count(*) FROM hq_work_records w WHERE w.customer_id=c.id) AS works
            FROM hq_customers c""" + clause +
            " ORDER BY c.name COLLATE NOCASE,c.id LIMIT ? OFFSET ?",
            (*args, PAGE_SIZE, offset)).fetchall()
    return total, [dict(row) for row in rows]


def customer_snapshot(customer_id):
    """Owner-facing profile with canonical IDs; no unverified financial totals."""
    if not isinstance(customer_id, str) or not HEX_ID.fullmatch(customer_id):
        raise ValueError("Invalid HQ customer reference")
    with connect() as db:
        _schema(db)
        db.execute("BEGIN")
        row = db.execute("""SELECT id,name,email,phone,source_request_id,created_at
            FROM hq_customers WHERE id=?""", (customer_id,)).fetchone()
        if row is None:
            raise LookupError("HQ customer not found")
        vehicles = [dict(v) for v in db.execute("""SELECT id,make,model,model_year,
            registration FROM hq_vehicles WHERE customer_id=?
            ORDER BY make COLLATE NOCASE,model COLLATE NOCASE,id""", (customer_id,))]
        work = [dict(w) for w in db.execute("""SELECT id,source_request_id,kind,
            service,status,preferred_date,preferred_time,revision
            FROM hq_work_records WHERE customer_id=?
            ORDER BY created_at DESC,id DESC LIMIT 100""", (customer_id,))]
        work_count = db.execute(
            "SELECT count(*) FROM hq_work_records WHERE customer_id=?", (customer_id,)
        ).fetchone()[0]
    return dict(row), vehicles, work, work_count


@customer_directory.after_request
def private_headers(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@customer_directory.route("/hq/customers", methods=["GET", "POST"])
@required
def directory():
    query, offset = "", 0
    if request.method == "POST":
        if request.content_length is None or request.content_length > 1200:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""),
                                      session.get("csrf", "not-set")):
            abort(403)
        query = request.form.get("q", "")
        raw_offset = request.form.get("offset", "0")
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,5})", raw_offset):
            abort(400)
        offset = int(raw_offset)
    try:
        total, customers = find_customers(query, offset)
    except ValueError:
        abort(400)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("HQ customer directory unavailable")
        abort(503)
    return render_template("hq_customer_directory.html", query=query, offset=offset,
                           page_size=PAGE_SIZE, total=total, customers=customers,
                           csrf=session["csrf"])


@customer_directory.get("/hq/customers/<customer_id>")
@required
def profile(customer_id):
    try:
        customer, vehicles, work, work_count = customer_snapshot(customer_id)
    except (ValueError, LookupError):
        abort(404)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("HQ customer profile unavailable")
        abort(503)
    return render_template("hq_customer_profile.html", customer=customer,
                           vehicles=vehicles, work=work, work_count=work_count)


class CustomerCreationConflict(Exception):
    """An idempotency token was reused for a different owner-entered contact."""


def _new_customer_fields(payload):
    """Validate a standalone owner-created contact, without claiming Sheets identity."""
    if not hasattr(payload, "get"):
        raise ValueError("Invalid contact submission")
    fields = {}
    limits = {"name": 160, "email": 254, "phone": 40,
              "address": 240, "postcode": 12}
    for key, limit in limits.items():
        value = payload.get(key, "")
        if not isinstance(value, str) or len(value) > limit + 20:
            raise ValueError("Invalid " + key)
        fields[key] = value.strip()
        if len(fields[key]) > limit:
            raise ValueError("Oversized " + key)
    fields["email"] = fields["email"].casefold()
    fields["postcode"] = " ".join(fields["postcode"].upper().split())
    if not fields["name"]:
        raise ValueError("Enter a customer name")
    if not fields["email"] and not fields["phone"]:
        raise ValueError("Enter an email address or telephone number")
    if fields["email"] and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", fields["email"]):
        raise ValueError("Enter a valid customer email")
    if fields["phone"] and not re.fullmatch(r"[+0-9 ()-]{7,40}", fields["phone"]):
        raise ValueError("Enter a valid customer telephone number")
    if fields["phone"] and len(re.sub(r"\D", "", fields["phone"])) < 7:
        raise ValueError("Enter a valid customer telephone number")
    operation_id = payload.get("operation_id", "")
    if not isinstance(operation_id, str) or not HEX_ID.fullmatch(operation_id):
        raise ValueError("Invalid customer submission identifier")
    return fields, operation_id


def _customer_creation_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_standalone_customer_creations(
        operation_id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL UNIQUE REFERENCES hq_customers(id),
        payload_fingerprint TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_standalone_customer_events(
        event_id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        event_type TEXT NOT NULL CHECK(event_type='created'),
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL)""")


def create_standalone_customer(payload, owner_identity):
    """Idempotent native contact CREATE. Never creates a job, Sheets link or account."""
    fields, operation_id = _new_customer_fields(payload)
    secret = current_app.secret_key
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("Persistent HQ owner session secret required")
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = hmac.new(secret.encode(), encoded, hashlib.sha256).hexdigest()
    actor = _actor_fingerprint(owner_identity)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        customer_edit_tables(db)
        _customer_creation_tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("""SELECT customer_id,payload_fingerprint
            FROM hq_standalone_customer_creations WHERE operation_id=?""",
            (operation_id,)).fetchone()
        if prior is not None:
            if not hmac.compare_digest(prior["payload_fingerprint"], fingerprint):
                raise CustomerCreationConflict(
                    "This form was already used with different details. Open a new form.")
            return prior["customer_id"]
        if fields["email"] and db.execute("""SELECT 1 FROM hq_customers
            WHERE lower(trim(email))=? LIMIT 1""", (fields["email"],)).fetchone():
            raise ValueError("Email already exists in HQ. Select the existing customer.")
        if fields["phone"]:
            digits = re.sub(r"\D", "", fields["phone"])
            if any(re.sub(r"\D", "", row[0] or "") == digits for row in db.execute(
                    "SELECT phone FROM hq_customers WHERE phone <> ''")):
                raise ValueError("Telephone number already exists in HQ. Select the existing customer.")
        customer_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        db.execute("""INSERT INTO hq_customers
            (id,source_request_id,name,email,phone,created_at)
            VALUES (?,NULL,?,?,?,?)""",
            (customer_id, fields["name"], fields["email"], fields["phone"], now))
        if fields["address"] or fields["postcode"]:
            db.execute("""INSERT INTO hq_customer_addresses(customer_id,address,postcode)
                VALUES (?,?,?)""",
                (customer_id, fields["address"], fields["postcode"]))
        db.execute("""INSERT INTO hq_standalone_customer_creations
            VALUES (?,?,?,?,?)""",
            (operation_id, customer_id, fingerprint, actor, now))
        db.execute("""INSERT INTO hq_standalone_customer_events
            VALUES (?,?,?,?,?)""",
            (uuid.uuid4().hex, customer_id, "created", actor, now))
    return customer_id


@customer_directory.route("/hq/customers/new", methods=["GET", "POST"])
@required
def new_customer():
    """Owner-facing contact creation without forcing a fictional vehicle or job."""
    if request.method == "GET":
        return render_template("hq_customer_create.html", csrf=session["csrf"],
                               operation_id=uuid.uuid4().hex, values={}, error=None)
    if request.content_length is None or request.content_length > 8192:
        abort(413)
    if not secrets.compare_digest(request.form.get("csrf", ""),
                                  session.get("csrf", "not-set")):
        abort(403)
    payload = request.form.to_dict(flat=True)
    try:
        customer_id = create_standalone_customer(payload, session.get("google_sub"))
    except (ValueError, CustomerCreationConflict) as exc:
        return render_template("hq_customer_create.html", csrf=session["csrf"],
            operation_id=payload.get("operation_id", ""),
            values=payload, error=str(exc)), (409 if isinstance(exc, CustomerCreationConflict) else 400)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("HQ standalone customer creation unavailable")
        abort(503)
    return redirect(url_for(".profile", customer_id=customer_id), code=303)
