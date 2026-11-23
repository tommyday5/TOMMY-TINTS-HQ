"""Owner-reviewed customer account classification in canonical HQ storage.

An account classification is not itself a service tariff, maintenance entitlement,
verified agreement, payment status, or permission for automatic bookings.
The Google workbook and all existing work/quote pricing remain untouched.
"""
import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for

from customer_request_store import connect
from hq_auth import required
from hq_core_records import _schema
from hq_customer_archive import _tables as _archive_tables
from hq_work_progress import _actor_fingerprint

account_types = Blueprint("customer_account_types", __name__)
ID = re.compile(r"[0-9a-f]{32}\Z")
REV = re.compile(r"(?:0|[1-9][0-9]{0,17})\Z")

TYPES = {"retail": "Retail", "trade": "Trade", "maintenance": "Maintenance"}
BASIS = {
    "retail": "owner_review",
    "trade": "trade_evidence_reviewed",
    "maintenance": "maintenance_agreement_reviewed",
}
BASIS_LABELS = {
    "owner_review": "Owner reviewed the Retail classification",
    "trade_evidence_reviewed": "Owner reviewed trade-account evidence",
    "maintenance_agreement_reviewed": "Owner reviewed the maintenance agreement",
}


class StaleAccountType(ValueError):
    """The account classification changed since the owner opened the editor."""


class AccountTypeRetryConflict(ValueError):
    """An idempotency token was reused for a different account change."""


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_account_types(
        customer_id TEXT PRIMARY KEY REFERENCES hq_customers(id),
        tier TEXT NOT NULL CHECK(tier IN ('retail','trade','maintenance')),
        basis TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>=1),
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_account_type_operations(
        operation_id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        request_fingerprint TEXT NOT NULL,
        resulting_revision INTEGER NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_account_type_events(
        id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        revision INTEGER NOT NULL,
        previous_tier TEXT NOT NULL,
        next_tier TEXT NOT NULL,
        basis TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(customer_id,revision)
    )""")


def _validate_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Invalid HQ customer/operation reference")


def account_status(customer_id):
    """Default Retail is unreviewed, not an automatically granted Trade account."""
    _validate_id(customer_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        if db.execute("SELECT 1 FROM hq_customers WHERE id=?", (customer_id,)).fetchone() is None:
            raise LookupError("HQ customer not found")
        row = db.execute("""SELECT tier,basis,revision,updated_at
            FROM hq_customer_account_types WHERE customer_id=?""", (customer_id,)).fetchone()
        return (dict(row) | {"reviewed": True}) if row else {
            "tier": "retail", "basis": "unreviewed", "revision": 0,
            "updated_at": None, "reviewed": False,
        }


def set_account_type(customer_id, tier, basis, expected_revision, operation_id, owner_identity):
    """Atomic, owner-reviewed, retry-safe classification without pricing side effects."""
    _validate_id(customer_id)
    _validate_id(operation_id)
    if tier not in TYPES or basis != BASIS.get(tier):
        raise ValueError("Choose a tier and the matching owner-reviewed evidence")
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**63-1:
        raise ValueError("Invalid account revision")
    secret = current_app.secret_key
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("Persistent owner session secret required")
    actor = _actor_fingerprint(owner_identity)
    payload = json.dumps({
        "customer": customer_id, "tier": tier, "basis": basis,
        "expected_revision": expected_revision, "actor": actor,
    }, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        _archive_tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("""SELECT customer_id,request_fingerprint,resulting_revision
            FROM hq_customer_account_type_operations WHERE operation_id=?""",
            (operation_id,)).fetchone()
        if prior is not None:
            if prior["customer_id"] != customer_id or not hmac.compare_digest(
                    prior["request_fingerprint"], fingerprint):
                raise AccountTypeRetryConflict("This form was reused with different details.")
            return prior["resulting_revision"]
        if db.execute("SELECT 1 FROM hq_customers WHERE id=?", (customer_id,)).fetchone() is None:
            raise LookupError("HQ customer not found")
        archived = db.execute("""SELECT is_archived FROM hq_customer_archive_state
            WHERE customer_id=?""", (customer_id,)).fetchone()
        if archived and archived["is_archived"]:
            raise StaleAccountType("Restore the archived customer before changing its account.")
        row = db.execute("""SELECT tier,basis,revision FROM hq_customer_account_types
            WHERE customer_id=?""", (customer_id,)).fetchone()
        previous = row["tier"] if row else "unreviewed"
        revision = row["revision"] if row else 0
        if revision != expected_revision:
            raise StaleAccountType("The customer account changed; reload before saving.")
        timestamp = datetime.now(timezone.utc).isoformat()
        if row is None or row["tier"] != tier or row["basis"] != basis:
            revision += 1
            db.execute("""INSERT INTO hq_customer_account_types
                (customer_id,tier,basis,revision,updated_at) VALUES (?,?,?,?,?)
                ON CONFLICT(customer_id) DO UPDATE SET
                tier=excluded.tier,basis=excluded.basis,
                revision=excluded.revision,updated_at=excluded.updated_at""",
                (customer_id, tier, basis, revision, timestamp))
            db.execute("""INSERT INTO hq_customer_account_type_events
                (id,customer_id,revision,previous_tier,next_tier,basis,
                 actor_fingerprint,occurred_at) VALUES (?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, customer_id, revision, previous, tier, basis, actor, timestamp))
        db.execute("""INSERT INTO hq_customer_account_type_operations
            (operation_id,customer_id,request_fingerprint,resulting_revision,
             actor_fingerprint,created_at) VALUES (?,?,?,?,?,?)""",
            (operation_id, customer_id, fingerprint, revision, actor, timestamp))
        return revision


def public_account_label(customer_id):
    """Allowlisted status only; excludes owner's evidence details and financial data."""
    status = account_status(customer_id)
    return TYPES[status["tier"]] if status["reviewed"] else "Retail (not yet reviewed)"


@account_types.after_request
def private_no_store(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@account_types.route("/hq/customers/<customer_id>/account-type", methods=["GET", "POST"])
@required
def edit_account_type(customer_id):
    if not isinstance(customer_id, str) or not ID.fullmatch(customer_id):
        abort(404)
    if request.method == "POST":
        if request.content_length is None or request.content_length > 2048:
            abort(413)
        if not hmac.compare_digest(request.form.get("csrf", ""),
                                   session.get("csrf", "not-set")):
            abort(403)
        revision = request.form.get("revision", "")
        if not REV.fullmatch(revision):
            abort(400)
        try:
            set_account_type(customer_id, request.form.get("tier", ""),
                             request.form.get("basis", ""), int(revision),
                             request.form.get("operation_id", ""),
                             session.get("google_sub"))
        except LookupError:
            abort(404)
        except (StaleAccountType, AccountTypeRetryConflict) as exc:
            abort(409, description=str(exc))
        except ValueError:
            abort(400)
        except (OSError, sqlite3.Error):
            current_app.logger.exception("Account classification write unavailable")
            abort(503)
        return redirect(url_for(".edit_account_type", customer_id=customer_id,
                                saved="1"), code=303)
    try:
        with connect() as db:
            _schema(db)
            customer = db.execute("SELECT id,name FROM hq_customers WHERE id=?",
                                  (customer_id,)).fetchone()
            if customer is None:
                abort(404)
            name = customer["name"]
        status = account_status(customer_id)
        with connect() as db:
            _tables(db)
            history = [dict(item) for item in db.execute("""SELECT revision,previous_tier,
                next_tier,basis,occurred_at FROM hq_customer_account_type_events
                WHERE customer_id=? ORDER BY revision DESC LIMIT 30""", (customer_id,))]
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Account classification read unavailable")
        abort(503)
    return render_template("hq_customer_account_type.html", customer_id=customer_id,
                           customer_name=name, status=status, types=TYPES, basis=BASIS,
                           basis_labels=BASIS_LABELS, history=history,
                           operation_id=uuid.uuid4().hex, csrf=session["csrf"])


# Keep the owner postcode editor on the same authenticated customer blueprint.
from hq_customer_postcodes import register_routes as _register_postcode_routes
_register_postcode_routes(account_types)
