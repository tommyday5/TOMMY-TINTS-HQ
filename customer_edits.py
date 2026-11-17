"""Owner edits to internal customers and local maintenance-sheet overrides."""
from contextlib import closing
from datetime import datetime, timezone
import os
import re
import secrets

from flask import Blueprint, abort, redirect, render_template, request, session, url_for

from customer_request_store import connect
from hq_auth import required
from hq_core_records import _schema

customer_edits = Blueprint("customer_edits", __name__)
FIELDS = {"name": 160, "email": 254, "phone": 40, "address": 240, "postcode": 12}


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_addresses (
        customer_id TEXT PRIMARY KEY REFERENCES hq_customers(id) ON DELETE CASCADE,
        address TEXT NOT NULL, postcode TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS maintenance_customer_edits (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
        address TEXT NOT NULL, postcode TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_edit_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL CHECK (scope IN ('internal', 'maintenance')),
        record_id TEXT NOT NULL, changed_fields TEXT NOT NULL, changed_at TEXT NOT NULL)""")



def _changed_fields(previous, values):
    """Record which fields changed without duplicating personal details in the audit."""
    if previous is None:
        return ",".join(FIELDS)
    return ",".join(key for key in FIELDS if (previous[key] or "") != values[key])


def _record_audit(db, scope, ref, changed_fields):
    if changed_fields:
        db.execute("""INSERT INTO hq_customer_edit_audit
            (scope, record_id, changed_fields, changed_at) VALUES (?,?,?,?)""",
            (scope, ref, changed_fields, datetime.now(timezone.utc).isoformat()))


class StaleCustomerEdit(Exception):
    """An edit was submitted from a page older than the saved customer data."""


def _revision(db, scope, ref):
    """Use the last PII-free audit ID as a durable optimistic concurrency token."""
    return db.execute("""SELECT COALESCE(MAX(id), 0) FROM hq_customer_edit_audit
        WHERE scope=? AND record_id=?""", (scope, ref)).fetchone()[0]


def maintenance_edit_snapshot(ref, fallback):
    """Read the override and version from one consistent SQLite snapshot."""
    with closing(connect()) as db, db:
        _tables(db)
        db.execute("BEGIN")
        stored = db.execute("SELECT * FROM maintenance_customer_edits WHERE id=?", (ref,)).fetchone()
        revision = _revision(db, "maintenance", ref)
        return (dict(stored) if stored is not None else fallback), revision


def posted_revision():
    raw = request.form.get("revision", "")
    if not re.fullmatch(r"[0-9]{1,18}", raw):
        raise StaleCustomerEdit()
    return int(raw)


CONFLICT_MESSAGE = ("This customer was changed since you opened the editor. "
                    "Your changes were not saved. Review the latest details and enter your changes again.")


def form_values():
    if request.content_length is None or request.content_length > 8192:
        abort(400)
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
        abort(403)
    values = {k: request.form.get(k, "").strip() for k in FIELDS}
    values["postcode"] = " ".join(values["postcode"].upper().split())
    if not values["name"] or any(len(values[k]) > limit for k, limit in FIELDS.items()):
        raise ValueError("Enter a customer name and check the field lengths.")
    if values["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", values["email"]):
        raise ValueError("Enter a valid email address.")
    return values


def edit_page(customer, back, error=None, maintenance=False, revision=0, status=None):
    return render_template("customer_edit.html", customer=customer, back=back, error=error,
                           maintenance=maintenance, revision=revision,
                           saved=request.args.get("saved") == "1" and not error,
                           csrf=session.get("csrf"),
                           browser_key=os.environ.get("HQ_GOOGLE_MAPS_BROWSER_KEY", "")), (
                               status if status is not None else (400 if error else 200))


def maintenance_edits():
    with closing(connect()) as db, db:
        _tables(db)
        return {row["id"]: dict(row) for row in db.execute("SELECT * FROM maintenance_customer_edits")}


def save_maintenance_edit(ref, values, expected_revision):
    with closing(connect()) as db, db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT * FROM maintenance_customer_edits WHERE id=?", (ref,)).fetchone()
        changed = _changed_fields(previous, values)
        if not changed:
            return
        if _revision(db, "maintenance", ref) != expected_revision:
            raise StaleCustomerEdit()
        db.execute("""INSERT INTO maintenance_customer_edits VALUES (?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, email=excluded.email,
            phone=excluded.phone, address=excluded.address, postcode=excluded.postcode""",
            (ref, *(values[k] for k in FIELDS)))
        _record_audit(db, "maintenance", ref, changed)


@customer_edits.route("/hq/customers/<customer_id>/edit", methods=["GET", "POST"])
@required
def internal_customer(customer_id):
    if not re.fullmatch(r"[0-9a-f]{32}", customer_id):
        abort(404)
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        if request.method == "GET":
            db.execute("BEGIN")
        row = db.execute("""SELECT c.name, c.email, c.phone, a.address, a.postcode
            FROM hq_customers c LEFT JOIN hq_customer_addresses a ON a.customer_id=c.id
            WHERE c.id=?""", (customer_id,)).fetchone()
        if row is None:
            abort(404)
        if request.method == "POST":
            try:
                values = form_values()
            except ValueError as exc:
                return edit_page(request.form, "/?page=jobs", str(exc),
                                 revision=request.form.get("revision", ""))
            try:
                expected_revision = posted_revision()
            except StaleCustomerEdit:
                return edit_page(row, "/?page=jobs", CONFLICT_MESSAGE,
                                 revision=_revision(db, "internal", customer_id), status=409)
            db.execute("BEGIN IMMEDIATE")
            # Re-read under the write lock to audit the actual previously saved values.
            row = db.execute("""SELECT c.name, c.email, c.phone, a.address, a.postcode
                FROM hq_customers c LEFT JOIN hq_customer_addresses a ON a.customer_id=c.id
                WHERE c.id=?""", (customer_id,)).fetchone()
            if row is None:
                abort(404)
            changed = _changed_fields(row, values)
            if changed:
                if _revision(db, "internal", customer_id) != expected_revision:
                    return edit_page(row, "/?page=jobs", CONFLICT_MESSAGE,
                                     revision=_revision(db, "internal", customer_id), status=409)
                db.execute("UPDATE hq_customers SET name=?,email=?,phone=? WHERE id=?",
                           (values["name"], values["email"], values["phone"], customer_id))
                db.execute("""INSERT INTO hq_customer_addresses VALUES (?,?,?)
                    ON CONFLICT(customer_id) DO UPDATE SET address=excluded.address,postcode=excluded.postcode""",
                    (customer_id, values["address"], values["postcode"]))
                _record_audit(db, "internal", customer_id, changed)
            return redirect(url_for(".internal_customer", customer_id=customer_id, saved=1), 303)
        return edit_page(row, "/?page=jobs", revision=_revision(db, "internal", customer_id))
