"""Owner edits to internal customers and local maintenance-sheet overrides."""
from contextlib import closing
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


def edit_page(customer, back, error=None, maintenance=False):
    return render_template("customer_edit.html", customer=customer, back=back, error=error,
                           maintenance=maintenance, saved=request.args.get("saved") == "1",
                           csrf=session.get("csrf"),
                           browser_key=os.environ.get("HQ_GOOGLE_MAPS_BROWSER_KEY", "")), (400 if error else 200)


def maintenance_edits():
    with closing(connect()) as db, db:
        _tables(db)
        return {row["id"]: dict(row) for row in db.execute("SELECT * FROM maintenance_customer_edits")}


def save_maintenance_edit(ref, values):
    with closing(connect()) as db, db:
        _tables(db)
        db.execute("""INSERT INTO maintenance_customer_edits VALUES (?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, email=excluded.email,
            phone=excluded.phone, address=excluded.address, postcode=excluded.postcode""",
            (ref, *(values[k] for k in FIELDS)))


@customer_edits.route("/hq/customers/<customer_id>/edit", methods=["GET", "POST"])
@required
def internal_customer(customer_id):
    if not re.fullmatch(r"[0-9a-f]{32}", customer_id):
        abort(404)
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        row = db.execute("""SELECT c.name, c.email, c.phone, a.address, a.postcode
            FROM hq_customers c LEFT JOIN hq_customer_addresses a ON a.customer_id=c.id
            WHERE c.id=?""", (customer_id,)).fetchone()
        if row is None:
            abort(404)
        if request.method == "POST":
            try:
                values = form_values()
            except ValueError as exc:
                return edit_page(request.form, "/?page=jobs", str(exc))
            db.execute("UPDATE hq_customers SET name=?,email=?,phone=? WHERE id=?",
                       (values["name"], values["email"], values["phone"], customer_id))
            db.execute("""INSERT INTO hq_customer_addresses VALUES (?,?,?)
                ON CONFLICT(customer_id) DO UPDATE SET address=excluded.address,postcode=excluded.postcode""",
                (customer_id, values["address"], values["postcode"]))
            return redirect(url_for(".internal_customer", customer_id=customer_id, saved=1), 303)
        return edit_page(row, "/?page=jobs")
