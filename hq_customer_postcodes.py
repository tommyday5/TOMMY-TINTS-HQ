"""Native owner-recorded customer postcode, separate from Sheets and routing.

A syntactically valid postcode is not independent address verification. This
feature never edits historical sources, customer portal data or tariff rules.
"""
import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

from flask import abort, current_app, redirect, render_template, request, session, url_for

from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_core_records import _schema
from hq_customer_archive import _tables as archive_tables
from hq_work_progress import _actor_fingerprint

ID = re.compile(r"[0-9a-f]{32}\Z")
REV = re.compile(r"(?:0|[1-9][0-9]{0,17})\Z")
POSTCODE = re.compile(r"(?:GIR0AA|[A-Z]{1,2}[0-9][0-9A-Z]?[0-9][A-Z]{2})\Z")


def normalize_postcode(value):
    if not isinstance(value, str) or len(value) > 12 or not re.fullmatch(r"[A-Za-z0-9 ]*", value):
        raise ValueError("Enter a UK postcode, or leave blank to clear it.")
    compact = value.replace(" ", "").upper()
    if not compact:
        return ""
    if not POSTCODE.fullmatch(compact):
        raise ValueError("Enter a UK postcode in a valid format.")
    return compact[:-3] + " " + compact[-3:]


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_postcodes(
        customer_id TEXT PRIMARY KEY REFERENCES hq_customers(id),
        postcode TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>=1),
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_postcode_events(
        id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        revision INTEGER NOT NULL CHECK(revision>=1),
        previous_postcode TEXT NOT NULL,
        next_postcode TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(customer_id,revision)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_postcode_operations(
        operation_id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        request_fingerprint TEXT NOT NULL,
        result_revision INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )""")


def _customer(db, customer_id):
    row = db.execute("""SELECT c.id,c.name,COALESCE(a.is_archived,0) AS archived
        FROM hq_customers c LEFT JOIN hq_customer_archive_state a
        ON a.customer_id=c.id WHERE c.id=?""", (customer_id,)).fetchone()
    if row is None:
        raise LookupError("Customer not found")
    return dict(row)


def postcode_status(db, customer_id):
    """Read only; missing table or cleared field is not a verified postcode."""
    if not db.execute("""SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='hq_customer_postcodes'""").fetchone():
        return {"postcode": "", "revision": 0, "updated_at": None}
    row = db.execute("""SELECT postcode,revision,updated_at
        FROM hq_customer_postcodes WHERE customer_id=?""", (customer_id,)).fetchone()
    return dict(row) if row else {"postcode": "", "revision": 0, "updated_at": None}


def _setup(db):
    db.execute("PRAGMA foreign_keys=ON")
    _schema(db)
    archive_tables(db)
    _tables(db)


def snapshot(customer_id):
    if not storage_ready():
        raise ValueError("Persistent HQ storage is unavailable.")
    with closing(connect()) as db, db:
        _setup(db)
        db.execute("BEGIN")
        customer = _customer(db, customer_id)
        status = postcode_status(db, customer_id)
        history = [dict(row) for row in db.execute("""SELECT revision,previous_postcode,
            next_postcode,occurred_at FROM hq_customer_postcode_events
            WHERE customer_id=? ORDER BY revision DESC LIMIT 30""", (customer_id,))]
    return customer, status, history


def save_postcode(customer_id, value, expected_revision, operation_id, owner_identity):
    if not storage_ready():
        raise ValueError("Persistent HQ storage is unavailable.")
    if not isinstance(customer_id,str) or not ID.fullmatch(customer_id) or not isinstance(operation_id,str) or not ID.fullmatch(operation_id):
        raise ValueError("Invalid customer or submission reference.")
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**63-1:
        raise ValueError("Invalid revision.")
    postcode = normalize_postcode(value)
    actor = _actor_fingerprint(owner_identity)
    secret = current_app.secret_key
    if not isinstance(secret,str) or not secret:
        raise RuntimeError("Persistent owner session secret required.")
    payload = json.dumps([customer_id,postcode,expected_revision,actor],
                         separators=(",",":")).encode()
    fingerprint = hmac.new(secret.encode(),payload,hashlib.sha256).hexdigest()
    with closing(connect()) as db, db:
        _setup(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("""SELECT customer_id,request_fingerprint,result_revision
            FROM hq_customer_postcode_operations WHERE operation_id=?""",(operation_id,)).fetchone()
        if prior:
            if prior["customer_id"] != customer_id or not hmac.compare_digest(prior["request_fingerprint"],fingerprint):
                raise ValueError("Submission was reused with different details.")
            return prior["result_revision"]
        customer = _customer(db,customer_id)
        if customer["archived"]:
            raise ValueError("Restore the archived customer before editing their postcode.")
        previous = postcode_status(db,customer_id)
        revision = previous["revision"]
        if revision != expected_revision:
            raise ValueError("Postcode changed; reload before saving.")
        now = datetime.now(timezone.utc).isoformat()
        if previous["postcode"] != postcode:
            revision += 1
            db.execute("""INSERT INTO hq_customer_postcodes
                (customer_id,postcode,revision,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(customer_id) DO UPDATE SET postcode=excluded.postcode,
                revision=excluded.revision,updated_at=excluded.updated_at""",
                (customer_id,postcode,revision,now))
            db.execute("""INSERT INTO hq_customer_postcode_events
                VALUES(?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex,customer_id,revision,previous["postcode"],postcode,actor,now))
        db.execute("""INSERT INTO hq_customer_postcode_operations
            VALUES(?,?,?,?,?)""",(operation_id,customer_id,fingerprint,revision,now))
        return revision


def register_routes(blueprint):
    @blueprint.route("/hq/customers/<customer_id>/postcode", methods=["GET","POST"])
    @required
    def edit_postcode(customer_id):
        if not ID.fullmatch(customer_id):
            abort(404)
        if request.method == "POST":
            if request.content_length is None or request.content_length > 2048:
                abort(413)
            if not hmac.compare_digest(request.form.get("csrf",""),session.get("csrf","not-set")):
                abort(403)
            raw = request.form.get("revision","")
            if not REV.fullmatch(raw) or "postcode" not in request.form:
                abort(400)
            try:
                save_postcode(customer_id,request.form["postcode"],int(raw),
                              request.form.get("operation_id",""),session.get("google_sub"))
            except LookupError:
                abort(404)
            except ValueError as exc:
                abort(409,description=str(exc))
            except (sqlite3.Error,OSError,RuntimeError):
                current_app.logger.warning("Native customer postcode save unavailable")
                abort(503)
            return redirect(url_for(".edit_postcode",customer_id=customer_id,saved="1"),code=303)
        try:
            customer,status,history = snapshot(customer_id)
        except LookupError:
            abort(404)
        except (ValueError,sqlite3.Error,OSError):
            abort(503)
        return render_template("hq_customer_postcode.html",customer=customer,status=status,
                               history=history,csrf=session.get("csrf",""),
                               operation_id=uuid.uuid4().hex,saved=request.args.get("saved")=="1")
