"""Owner-only canonical HQ customer directory and customer work history.

Read-only views of HQ's existing SQLite records. Search never queries Sheets,
touches providers, or exposes customer data to public bookings clients.
"""
import re
import secrets
import sqlite3

from flask import Blueprint, abort, current_app, render_template, request, session
from customer_request_store import connect
from hq_auth import required
from hq_core_records import _schema

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
