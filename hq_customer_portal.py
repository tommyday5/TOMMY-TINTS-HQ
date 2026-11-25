"""Owner-invited customer portal for APPROVED HQ work only.

Access is issued manually by the authenticated owner to a verified customer.
No emails, external bookings, sheets writes or financial actions occur here.
The invitation is single-use; customer sessions are stored hashed and revocable.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_core_records import _schema
from hq_customer_documents import _tables as _document_tables
from hq_customer_account_types import public_account_label
import hq_customer_invoice_publications as invoice_publications

customer_portal = Blueprint("customer_portal", __name__)
_ID = re.compile(r"[0-9a-f]{32}\Z")
_CODE = re.compile(r"[A-Za-z0-9_-]{43,60}\Z")
_PUBLIC_STATUSES = ("approved", "in_progress", "completed", "cancelled")
_INVITE_SECONDS = 48 * 3600
_SESSION_SECONDS = 3600
_ATTEMPT_WINDOW = 15 * 60
_MAX_ATTEMPTS = 8


def _now():
    return int(time.time())


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _enabled():
    # Fail closed if the signing key will not survive a restart or storage is ephemeral.
    return bool(os.environ.get("HQ_SESSION_SECRET", "").strip()) and storage_ready()


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_portal_invites (
        token_hash TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        operation_id TEXT NOT NULL UNIQUE,
        issued_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        used_at INTEGER,
        revoked_at INTEGER
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_portal_invites_customer
        ON hq_customer_portal_invites(customer_id)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_portal_sessions (
        sid_hash TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        issued_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        revoked_at INTEGER
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_portal_sessions_customer
        ON hq_customer_portal_sessions(customer_id)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_portal_failures (
        visitor_hash TEXT PRIMARY KEY,
        window_start INTEGER NOT NULL,
        attempts INTEGER NOT NULL
    )""")


def _customer_id(customer_id):
    if not isinstance(customer_id, str) or not _ID.fullmatch(customer_id):
        raise ValueError("Invalid customer ID")


def portal_access_summary(customer_id):
    _customer_id(customer_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        customer = db.execute("SELECT id,name FROM hq_customers WHERE id=?", (customer_id,)).fetchone()
        if customer is None:
            raise LookupError("HQ customer not found")
        now = _now()
        invites = db.execute("""SELECT COUNT(*) FROM hq_customer_portal_invites
            WHERE customer_id=? AND used_at IS NULL AND revoked_at IS NULL
              AND expires_at>?""", (customer_id, now)).fetchone()[0]
        sessions = db.execute("""SELECT COUNT(*) FROM hq_customer_portal_sessions
            WHERE customer_id=? AND revoked_at IS NULL AND expires_at>?""",
            (customer_id, now)).fetchone()[0]
    return {"name": customer["name"], "id": customer_id, "invites": invites, "sessions": sessions}


def issue_customer_invite(customer_id, operation_id):
    """One-time manual handover code, never persisted in plaintext.

    Replays return no token: the original token cannot be recovered or shown twice.
    """
    _customer_id(customer_id)
    if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
        raise ValueError("Invalid operation ID")
    if not _enabled():
        raise RuntimeError("Persistent customer access is not configured")
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM hq_customers WHERE id=?", (customer_id,)).fetchone() is None:
            raise LookupError("HQ customer not found")
        prior = db.execute("SELECT customer_id FROM hq_customer_portal_invites WHERE operation_id=?",
                           (operation_id,)).fetchone()
        if prior is not None:
            if prior["customer_id"] != customer_id:
                raise ValueError("Operation ID already used for another customer")
            return None
        now = _now()
        # Issuing a replacement invalidates unused older invitations, not live sessions.
        db.execute("""UPDATE hq_customer_portal_invites SET revoked_at=?
            WHERE customer_id=? AND used_at IS NULL AND revoked_at IS NULL""",
            (now, customer_id))
        code = secrets.token_urlsafe(32)
        db.execute("""INSERT INTO hq_customer_portal_invites
            (token_hash,customer_id,operation_id,issued_at,expires_at)
            VALUES (?,?,?,?,?)""", (_digest(code), customer_id, operation_id, now,
                                    now + _INVITE_SECONDS))
    return code


def revoke_customer_access(customer_id):
    _customer_id(customer_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM hq_customers WHERE id=?", (customer_id,)).fetchone() is None:
            raise LookupError("HQ customer not found")
        now = _now()
        db.execute("""UPDATE hq_customer_portal_invites SET revoked_at=?
            WHERE customer_id=? AND revoked_at IS NULL""", (now, customer_id))
        db.execute("""UPDATE hq_customer_portal_sessions SET revoked_at=?
            WHERE customer_id=? AND revoked_at IS NULL""", (now, customer_id))


def redeem_customer_invite(code, visitor):
    """Atomic one-time exchange with a persistent, revocable session."""
    if not _enabled():
        raise RuntimeError("Customer portal is not configured")
    valid_format = isinstance(code, str) and bool(_CODE.fullmatch(code))
    if not isinstance(visitor, str):
        visitor = ""
    # Do not store raw IP addresses in the failed-login ledger.
    visitor_hash = hmac.new(os.environ["HQ_SESSION_SECRET"].encode(),
                            visitor[:128].encode(), hashlib.sha256).hexdigest()
    now = _now()
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        fail = db.execute("SELECT window_start,attempts FROM hq_customer_portal_failures WHERE visitor_hash=?",
                          (visitor_hash,)).fetchone()
        if fail and now - fail["window_start"] < _ATTEMPT_WINDOW and fail["attempts"] >= _MAX_ATTEMPTS:
            raise PermissionError("Too many attempts. Try again later.")
        invite = db.execute("""SELECT customer_id FROM hq_customer_portal_invites
            WHERE token_hash=? AND used_at IS NULL AND revoked_at IS NULL
              AND expires_at>?""", (_digest(code) if valid_format else "", now)).fetchone()
        if invite is None:
            count = fail["attempts"] + 1 if fail and now - fail["window_start"] < _ATTEMPT_WINDOW else 1
            db.execute("""INSERT INTO hq_customer_portal_failures VALUES (?,?,?)
                ON CONFLICT(visitor_hash) DO UPDATE SET
                window_start=excluded.window_start,attempts=excluded.attempts""",
                (visitor_hash, fail["window_start"] if fail and now - fail["window_start"] < _ATTEMPT_WINDOW else now, count))
            return None
        db.execute("UPDATE hq_customer_portal_invites SET used_at=? WHERE token_hash=?",
                   (now, _digest(code)))
        db.execute("DELETE FROM hq_customer_portal_failures WHERE visitor_hash=?", (visitor_hash,))
        sid = secrets.token_urlsafe(32)
        db.execute("""INSERT INTO hq_customer_portal_sessions
            (sid_hash,customer_id,issued_at,expires_at) VALUES (?,?,?,?)""",
            (_digest(sid), invite["customer_id"], now, now + _SESSION_SECONDS))
    return sid


def active_customer():
    sid = session.get("customer_portal_sid")
    if not isinstance(sid, str) or not _CODE.fullmatch(sid) or not _enabled():
        return None
    try:
        with connect() as db:
            _schema(db)
            _tables(db)
            row = db.execute("""SELECT s.customer_id FROM hq_customer_portal_sessions s
                JOIN hq_customers c ON c.id=s.customer_id
                WHERE s.sid_hash=? AND s.revoked_at IS NULL AND s.expires_at>?""",
                (_digest(sid), _now())).fetchone()
            return row["customer_id"] if row else None
    except (sqlite3.Error, OSError):
        return None


def customer_portal_snapshot(customer_id):
    """Allowlisted customer-safe fields ONLY. Never return internal notes or costs."""
    _customer_id(customer_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        customer = db.execute("SELECT name FROM hq_customers WHERE id=?", (customer_id,)).fetchone()
        if customer is None:
            raise LookupError("Customer not found")
        # Both document/update tables are additive and safe to create on read.
        _document_tables(db)
        works = db.execute("""SELECT w.id,w.kind,w.status,w.service,
            w.price_pence,w.preferred_date,w.preferred_time,
            v.make,v.model,v.registration,
            COALESCE(doc.public_description,'') AS public_description,
            CASE WHEN upd.is_published=1 THEN upd.message ELSE '' END AS portal_update,
            CASE WHEN upd.is_published=1 THEN upd.published_at ELSE NULL END AS update_published_at
            FROM hq_work_records w
            JOIN hq_vehicles v ON v.id=w.vehicle_id AND v.customer_id=w.customer_id
            LEFT JOIN hq_work_customer_documents doc ON doc.work_id=w.id
            LEFT JOIN hq_work_customer_updates upd ON upd.work_id=w.id
            WHERE w.customer_id=?
              AND w.status IN ('approved','in_progress','completed','cancelled')
              AND NOT EXISTS (SELECT 1 FROM hq_work_archive_state a
                  WHERE a.work_id=w.id AND a.is_archived=1)
            ORDER BY w.created_at DESC,w.id DESC LIMIT 100""", (customer_id,)).fetchall()
    result = []
    for row in works:
        value = dict(row)
        price = value.pop("price_pence")
        value["approved_price"] = (
            "£" + f"{price // 100:,}.{price % 100:02d}" if price is not None else None
        )
        result.append(value)
    return customer["name"], result


@customer_portal.after_request
def _privacy_headers(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@customer_portal.route("/hq/customers/<customer_id>/portal-access", methods=["GET", "POST"])
@required
def owner_portal_access(customer_id):
    if not _enabled():
        return "Persistent customer access is not configured.", 503
    try:
        _customer_id(customer_id)
    except ValueError:
        abort(404)
    if request.method == "POST":
        if request.content_length is None or request.content_length > 1500:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "unset")):
            abort(403)
        action = request.form.get("action", "")
        try:
            if action == "issue":
                code = issue_customer_invite(customer_id, request.form.get("operation_id", ""))
                message = "One-time code created. Share only after verifying the customer's identity." if code else "This request was already processed. Generate a new code if needed."
            elif action == "revoke":
                revoke_customer_access(customer_id)
                code, message = None, "All invitations and customer sessions have been revoked."
            else:
                abort(400)
        except (ValueError, LookupError):
            abort(400)
        except (RuntimeError, sqlite3.Error, OSError):
            current_app.logger.exception("Customer portal access change unavailable")
            abort(503)
    else:
        code, message = None, None
    try:
        details = portal_access_summary(customer_id)
    except (ValueError, LookupError):
        abort(404)
    except (sqlite3.Error, OSError):
        abort(503)
    return render_template("hq_customer_portal_access.html", details=details,
                           csrf=session["csrf"], code=code, message=message,
                           operation_id=secrets.token_hex(16))


@customer_portal.route("/hq/customers/<customer_id>/invoice-publications", methods=["GET", "POST"])
@required
def owner_invoice_publications(customer_id):
    if not _enabled():
        abort(503)
    message = error = None
    status = 200
    try:
        details = portal_access_summary(customer_id)
    except (ValueError, LookupError):
        abort(404)
    except (sqlite3.Error, OSError):
        abort(503)
    if request.method == "POST":
        if request.content_length is None or request.content_length > 2000:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "unset")):
            abort(403)
        try:
            if request.form.get("confirm") != "yes":
                raise ValueError("Confirm this exact publication or withdrawal")
            mode = request.form.get("payment_mode", "summary")
            if mode not in ("summary", "observations"):
                raise ValueError("Invalid publication mode")
            if mode == "observations" and request.form.get("confirm_payment") != "yes":
                raise ValueError("Confirm the exact historical card observations separately")
            invoice_publications.change(customer_id, request.form.get("invoice_id", ""),
                request.form.get("action", ""), int(request.form.get("revision", "-1")),
                request.form.get("digest", ""), request.form.get("operation_id", ""),
                include_payments=mode == "observations")
            message = "Publication review saved. No current paid status, settlement or message was sent."
        except ValueError as exc:
            error, status = str(exc), 400
        except (sqlite3.Error, OSError):
            abort(503)
    try:
        rows = invoice_publications.summaries(customer_id, owner=True)
    except (ValueError, sqlite3.Error, OSError):
        abort(503)
    return render_template("hq_customer_invoice_publications.html", details=details,
                           rows=rows, csrf=session["csrf"], message=message, error=error), status


@customer_portal.route("/customer/login", methods=["GET", "POST"])
def customer_login():
    if not _enabled():
        return "Customer access is not configured.", 503
    if active_customer() is not None:
        return redirect(url_for(".customer_account"), code=303)
    csrf = session.get("customer_portal_login_csrf")
    if not isinstance(csrf, str):
        csrf = secrets.token_urlsafe(24)
        session["customer_portal_login_csrf"] = csrf
    error = None
    if request.method == "POST":
        if request.content_length is None or request.content_length > 1500:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""), csrf):
            abort(403)
        try:
            sid = redeem_customer_invite(request.form.get("code", "").strip(),
                                         request.remote_addr or "")
        except PermissionError:
            return render_template("customer_portal_login.html", csrf=csrf,
                                   error="Too many attempts. Try again later."), 429
        except (RuntimeError, sqlite3.Error, OSError):
            current_app.logger.exception("Customer portal login unavailable")
            abort(503)
        if sid is None:
            error = "Access code invalid or expired. Contact Tommy Tints."
        else:
            # Never share the owner's authenticated HQ browser session with customer access.
            session.clear()
            session.permanent = True
            session["customer_portal_sid"] = sid
            session["customer_portal_csrf"] = secrets.token_urlsafe(24)
            return redirect(url_for(".customer_account"), code=303)
    return render_template("customer_portal_login.html", csrf=csrf,
                           error=error), (401 if error else 200)


@customer_portal.get("/customer/account")
def customer_account():
    customer_id = active_customer()
    if not customer_id:
        return redirect(url_for(".customer_login"), code=302)
    try:
        name, works = customer_portal_snapshot(customer_id)
        invoices = invoice_publications.summaries(customer_id)
    except (sqlite3.Error, OSError):
        current_app.logger.exception("Customer account read unavailable")
        abort(503)
    return render_template("customer_portal_account.html", name=name,
                           account_type=public_account_label(customer_id),
                           works=works, invoices=invoices, csrf=session["customer_portal_csrf"])


@customer_portal.post("/customer/logout")
def customer_logout():
    if not secrets.compare_digest(request.form.get("csrf", ""),
                                  session.get("customer_portal_csrf", "unset")):
        abort(403)
    sid = session.get("customer_portal_sid")
    if isinstance(sid, str) and _CODE.fullmatch(sid):
        with connect() as db:
            _tables(db)
            db.execute("UPDATE hq_customer_portal_sessions SET revoked_at=? WHERE sid_hash=?",
                       (_now(), _digest(sid)))
    session.clear()
    return redirect(url_for(".customer_login"), code=303)
