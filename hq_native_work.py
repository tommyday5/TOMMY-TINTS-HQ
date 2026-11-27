"""Owner-only native HQ customer/job creation and draft editing.

Local SQLite on HQ_DATA_DIR is the only write destination. This intentionally
does not import, reserve a J-series Sheets ID or make an external booking.
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from datetime import date, datetime, timezone

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for
from customer_request_store import SERVICES, connect
from hq_auth import required
from hq_core_records import _audit_work, _schema, StaleWorkRecordError
from hq_work_progress import WORK_STAGES, work_progress_snapshot, record_work_progress
from hq_work_time import time_ledger_snapshot, append_work_time, void_work_time, correct_work_time_rate, correct_work_time_minutes
from hq_work_timer import snapshot as timer_snapshot, transition as timer_transition, history as timer_history, open_timer_jobs
from hq_work_materials import material_ledger_snapshot, append_work_material, void_work_material, UNITS
from hq_work_expenses import expense_ledger_snapshot, append_work_expense, void_work_expense, EXPENSE_TYPES
from hq_work_archive import archived_work_snapshot, set_work_archived, list_archived_work
from hq_work_cost_overview import cost_overview_snapshot

native_work = Blueprint("native_work", __name__)
TIERS = {"retail": "Retail", "trade": "Trade", "maintenance": "Maintenance"}
HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")
PHONE = re.compile(r"[+0-9 ()-]{7,35}\Z")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_native_work_details(
        work_id TEXT PRIMARY KEY REFERENCES hq_work_records(id),
        customer_tier TEXT NOT NULL CHECK(customer_tier IN ('retail','trade','maintenance')),
        created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_native_work_operations(
        operation_id TEXT PRIMARY KEY, work_id TEXT NOT NULL UNIQUE,
        payload_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_native_work_events(
        id TEXT PRIMARY KEY, work_id TEXT NOT NULL, action TEXT NOT NULL,
        revision INTEGER NOT NULL, created_at TEXT NOT NULL)""")


def _field(payload, name, limit, required=False):
    value = payload.get(name, "")
    if not isinstance(value, str):
        raise ValueError("Invalid " + name)
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise ValueError("Missing or oversized " + name)
    return value


def _edit_fields(payload):
    service = _field(payload, "service", 80, True)
    if service not in SERVICES:
        raise ValueError("Choose a supported service")
    tier = _field(payload, "customer_tier", 15, True)
    if tier not in TIERS:
        raise ValueError("Choose Retail, Trade or Maintenance")
    preferred_date = _field(payload, "preferred_date", 10)
    preferred_time = _field(payload, "preferred_time", 5)
    if preferred_date:
        try:
            if date.fromisoformat(preferred_date).isoformat() != preferred_date:
                raise ValueError()
        except ValueError:
            raise ValueError("Enter a valid date in YYYY-MM-DD format")
    if preferred_time:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", preferred_time):
            raise ValueError("Enter a valid time in HH:MM format")
    return {
        "service": service, "customer_tier": tier,
        "notes": _field(payload, "notes", 1500),
        "preferred_date": preferred_date, "preferred_time": preferred_time,
    }


def _create_fields(payload):
    values = _edit_fields(payload)
    kind = _field(payload, "kind", 5, True)
    if kind not in {"job", "quote"}:
        raise ValueError("Choose a quote or job")
    operation_id = _field(payload, "operation_id", 32, True)
    if not HEX_ID.fullmatch(operation_id):
        raise ValueError("Invalid operation identifier")
    customer_id = _field(payload, "customer_id", 32)
    vehicle_id = _field(payload, "vehicle_id", 32)
    if (customer_id and not HEX_ID.fullmatch(customer_id)) or (
        vehicle_id and not HEX_ID.fullmatch(vehicle_id)
    ):
        raise ValueError("Invalid customer or vehicle reference")
    if vehicle_id and not customer_id:
        raise ValueError("An existing vehicle requires its customer")
    values.update({
        "kind": kind, "operation_id": operation_id,
        "customer_id": customer_id, "vehicle_id": vehicle_id,
        "customer_name": _field(payload, "customer_name", 160),
        "email": _field(payload, "email", 254).casefold(),
        "phone": _field(payload, "phone", 40),
        "make": _field(payload, "make", 60), "model": _field(payload, "model", 60),
        "model_year": _field(payload, "model_year", 4),
        "registration": _field(payload, "registration", 20).upper(),
    })
    if not customer_id:
        if not values["customer_name"] or not (values["email"] or values["phone"]):
            raise ValueError("New customers need a name and an email or telephone number")
        if values["email"] and not EMAIL.fullmatch(values["email"]):
            raise ValueError("Enter a valid customer email")
        if values["phone"] and not PHONE.fullmatch(values["phone"]):
            raise ValueError("Enter a valid customer telephone number")
    elif any(values[k] for k in ("customer_name", "email", "phone")):
        raise ValueError("Choose an existing customer or enter a new one, not both")
    if vehicle_id:
        if any(values[k] for k in ("make", "model", "model_year", "registration")):
            raise ValueError("Choose an existing vehicle or enter a new one, not both")
    else:
        if not values["make"] or not values["model"]:
            raise ValueError("Enter the new vehicle's make and model")
        year = values["model_year"]
        if year and (not year.isascii() or not year.isdecimal()
                     or len(year) != 4 or not 1950 <= int(year) <= date.today().year + 1):
            raise ValueError("Enter a valid vehicle year or leave it blank")
    return values


def _fingerprint(values):
    # Use the configured stable server secret: do not duplicate customer PII in
    # the idempotency journal, and do not expose the fingerprint to the browser.
    secret = current_app.secret_key
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("HQ session secret not configured")
    raw = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def create_native_work(payload):
    """Atomic owner-requested CREATE: new/existing customer, vehicle, quote/job.

    Same operation and payload returns one ID, conflicting replay fails closed.
    Never mutates an existing customer, external workbook or provider.
    """
    fields = _create_fields(payload)
    fingerprint = _fingerprint(fields)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("""SELECT work_id,payload_fingerprint
            FROM hq_native_work_operations WHERE operation_id=?""",
            (fields["operation_id"],)).fetchone()
        if prior:
            if not hmac.compare_digest(prior["payload_fingerprint"], fingerprint):
                raise StaleWorkRecordError("This form was already submitted with different details. Start a new draft.")
            return prior["work_id"]
        customer_id = fields["customer_id"]
        vehicle_id = fields["vehicle_id"]
        now = _now()
        if customer_id:
            if db.execute("SELECT 1 FROM hq_customers WHERE id=?", (customer_id,)).fetchone() is None:
                raise ValueError("Customer not found. Refresh the selection.")
        else:
            # Surface likely collisions for human review; never silently merge contacts.
            if fields["email"] and db.execute(
                "SELECT 1 FROM hq_customers WHERE lower(trim(email))=? LIMIT 1",
                (fields["email"],)).fetchone():
                raise ValueError("That email is already in HQ. Select the existing customer.")
            if fields["phone"] and db.execute(
                "SELECT 1 FROM hq_customers WHERE phone=? LIMIT 1",
                (fields["phone"],)).fetchone():
                raise ValueError("That phone is already in HQ. Select the existing customer.")
            customer_id = uuid.uuid4().hex
            db.execute("""INSERT INTO hq_customers(id,source_request_id,name,email,phone,created_at)
                VALUES (?,NULL,?,?,?,?)""",
                (customer_id, fields["customer_name"], fields["email"], fields["phone"], now))
        if vehicle_id:
            owner = db.execute("SELECT customer_id FROM hq_vehicles WHERE id=?", (vehicle_id,)).fetchone()
            if owner is None or owner["customer_id"] != customer_id:
                raise ValueError("This vehicle does not belong to the selected customer")
        else:
            vehicle_id = uuid.uuid4().hex
            db.execute("""INSERT INTO hq_vehicles
                (id,customer_id,make,model,model_year,registration,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (vehicle_id, customer_id, fields["make"], fields["model"],
                 fields["model_year"], fields["registration"], now))
        work_id = uuid.uuid4().hex
        # HQ identifiers do not claim any historical J number.
        db.execute("""INSERT INTO hq_work_records
            (id,source_request_id,customer_id,vehicle_id,kind,service,notes,
             preferred_date,preferred_time,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,'draft',?)""",
            (work_id, "HQ-" + work_id, customer_id, vehicle_id, fields["kind"],
             fields["service"], fields["notes"], fields["preferred_date"],
             fields["preferred_time"], now))
        db.execute("INSERT INTO hq_native_work_details VALUES (?,?,?)",
                   (work_id, fields["customer_tier"], now))
        db.execute("INSERT INTO hq_native_work_operations VALUES (?,?,?,?)",
                   (fields["operation_id"], work_id, fingerprint, now))
        db.execute("INSERT INTO hq_native_work_events VALUES (?,?,?,?,?)",
                   (uuid.uuid4().hex, work_id, "created", 0, now))
        # No Sheets outbox enqueue: workbook remains read-only until explicit
        # historical ID mapping, source ownership and separate write consent.
    return work_id


def native_work_record(work_id):
    if not isinstance(work_id, str) or not HEX_ID.fullmatch(work_id):
        raise ValueError("Invalid native work ID")
    with connect() as db:
        _schema(db)
        _tables(db)
        row = db.execute("""SELECT w.*,d.customer_tier,COALESCE(a.is_archived,0) AS is_archived,
            c.name AS customer_name,
            c.email,c.phone,v.make,v.model,v.model_year,v.registration
            FROM hq_work_records w
            JOIN hq_native_work_details d ON d.work_id=w.id
            LEFT JOIN hq_work_archive_state a ON a.work_id=w.id
            JOIN hq_customers c ON c.id=w.customer_id
            JOIN hq_vehicles v ON v.id=w.vehicle_id
            WHERE w.id=? AND w.source_request_id=?""",
            (work_id, "HQ-" + work_id)).fetchone()
        historical = None
        if row is not None and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_historical_created_work'").fetchone():
            historical = db.execute("SELECT job_ref,reversed_at,manifest FROM hq_historical_created_work WHERE work_id=?",(work_id,)).fetchone()
    if row is None:
        raise LookupError("Native work record not found")
    record = dict(row)
    if historical:
        record["historical_job_ref"] = historical["job_ref"]
        record["historical_creation_reversed"] = bool(historical["reversed_at"])
        evidence = json.loads(historical["manifest"])
        record["historical_source_status"] = evidence.get("source_status")
        record["historical_source_price_pence"] = evidence.get("price_pence")
    return record


def update_native_work(work_id, payload, expected_revision):
    """Optimistic editable draft metadata; approval locks the scope of work."""
    fields = _edit_fields(payload)
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1:
        raise ValueError("Invalid draft revision")
    if not isinstance(work_id, str) or not HEX_ID.fullmatch(work_id):
        raise ValueError("Invalid native work ID")
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT w.status,w.revision,w.service,w.notes,
            w.preferred_date,w.preferred_time,d.customer_tier,
            COALESCE(a.is_archived,0) AS is_archived
            FROM hq_work_records w JOIN hq_native_work_details d ON d.work_id=w.id
            LEFT JOIN hq_work_archive_state a ON a.work_id=w.id
            WHERE w.id=? AND w.source_request_id=?""",
            (work_id, "HQ-" + work_id)).fetchone()
        if row is None:
            raise LookupError("Native work record not found")
        if row["is_archived"]:
            raise ValueError("Restore this archived record before editing it")
        if row["status"] not in ("draft", "ready_for_review"):
            raise ValueError("Approved or closed work cannot be edited here")
        changed = [key for key, value in fields.items() if value != row[key]]
        if not changed:
            return row["revision"]
        if row["revision"] != expected_revision:
            raise StaleWorkRecordError("This work record changed. Refresh before editing.")
        updated = db.execute("""UPDATE hq_work_records SET service=?,notes=?,
            preferred_date=?,preferred_time=?,revision=revision+1
            WHERE id=? AND revision=?""",
            (fields["service"], fields["notes"], fields["preferred_date"],
             fields["preferred_time"], work_id, expected_revision))
        if updated.rowcount != 1:
            raise StaleWorkRecordError("This work record changed. Refresh before editing.")
        db.execute("UPDATE hq_native_work_details SET customer_tier=? WHERE work_id=?",
                   (fields["customer_tier"], work_id))
        revision = expected_revision + 1
        # Deliberately log field names, not PII or notes.
        _audit_work(db, work_id, revision, "native_details",
                    {"changed_fields": changed, "revision": expected_revision},
                    {"changed_fields": changed, "revision": revision})
        db.execute("INSERT INTO hq_native_work_events VALUES (?,?,?,?,?)",
                   (uuid.uuid4().hex, work_id, "details_edited", revision, _now()))
    return revision


def _choices(search="", highlighted_id=""):
    if not isinstance(search, str) or len(search) > 80:
        raise ValueError("Customer search is too long")
    with connect() as db:
        _schema(db)
        db.execute("BEGIN")
        term = search.strip()
        predicate = """WHERE instr(lower(name),lower(?))>0
            OR instr(lower(email),lower(?))>0
            OR instr(lower(phone),lower(?))>0""" if term else ""
        params = [term] * 3 if term else []
        customers = [dict(r) for r in db.execute(
            "SELECT id,name,email,phone FROM hq_customers " + predicate
            + " ORDER BY name,id LIMIT 100", params)]
        # A profile deep-link must still find its customer beyond the first 100.
        # It never changes existing contact data or silently links another customer.
        if highlighted_id and not any(c["id"] == highlighted_id for c in customers):
            selected = db.execute("SELECT id,name,email,phone FROM hq_customers WHERE id=?",
                                  (highlighted_id,)).fetchone()
            if selected is None:
                raise LookupError("HQ customer not found")
            customers.insert(0, dict(selected))
        ids = [row["id"] for row in customers]
        vehicles = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            vehicles = [dict(r) for r in db.execute(
                "SELECT id,customer_id,make,model,registration FROM hq_vehicles "
                + "WHERE customer_id IN (" + placeholders + ") ORDER BY make,model,id", ids)]
    return customers, vehicles


@native_work.after_request
def private_no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@native_work.route("/hq/work/new", methods=["GET", "POST"])
@required
def new_work():
    if request.method == "GET":
        search = request.args.get("q", "")
        preferred_customer = request.args.get("customer_id", "")
        if preferred_customer and not HEX_ID.fullmatch(preferred_customer):
            abort(404)
        try:
            customers, vehicles = _choices(search, preferred_customer)
        except ValueError:
            abort(400)
        except LookupError:
            abort(404)
        return render_template("native_work.html", new=True,
                               values={"customer_id": preferred_customer} if preferred_customer else {},
                               csrf=session["csrf"],
                               operation_id=uuid.uuid4().hex, customers=customers,
                               vehicles=vehicles, search=search, services=sorted(SERVICES),
                               tiers=TIERS, error=None)
    if request.content_length is None or request.content_length > 12000:
        abort(413)
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
        abort(403)
    values = request.form.to_dict(flat=True)
    try:
        work_id = create_native_work(values)
    except (ValueError, StaleWorkRecordError) as exc:
        customers, vehicles = _choices()
        status = 409 if isinstance(exc, StaleWorkRecordError) else 400
        return render_template("native_work.html", new=True, values=values,
            csrf=session["csrf"], operation_id=values.get("operation_id", ""),
            customers=customers, vehicles=vehicles, search="", services=sorted(SERVICES),
            tiers=TIERS, error=str(exc)), status
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Native work creation unavailable")
        abort(503)
    return redirect(url_for(".edit_work", work_id=work_id, created="1"), 303)


@native_work.route("/hq/work/<work_id>/edit", methods=["GET", "POST"])
@required
def edit_work(work_id):
    try:
        current = native_work_record(work_id)
    except (ValueError, LookupError):
        abort(404)
    if request.method == "POST":
        if request.content_length is None or request.content_length > 12000:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
            abort(403)
        raw_revision = request.form.get("revision", "")
        if not re.fullmatch(r"[0-9]{1,18}", raw_revision):
            abort(400)
        try:
            update_native_work(work_id, request.form.to_dict(flat=True), int(raw_revision))
        except StaleWorkRecordError as exc:
            current = native_work_record(work_id)
            return render_template("native_work.html", new=False, record=current,
                values={}, csrf=session["csrf"], services=sorted(SERVICES), tiers=TIERS,
                error=str(exc)), 409
        except ValueError as exc:
            return render_template("native_work.html", new=False, record=current,
                values=request.form, csrf=session["csrf"], services=sorted(SERVICES), tiers=TIERS,
                error=str(exc)), 400
        except (OSError, sqlite3.Error):
            current_app.logger.exception("Native work edit unavailable")
            abort(503)
        return redirect(url_for(".edit_work", work_id=work_id, saved="1"), 303)
    return render_template("native_work.html", new=False, record=current, values={},
        csrf=session["csrf"], services=sorted(SERVICES), tiers=TIERS, error=None)


# Vehicle details are edited in their own versioned operation, rather than
# silently changing a historical work record or reassigning ownership.
VEHICLE_KEYS = ("make", "model", "model_year", "registration")


def _vehicle_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_vehicle_edit_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT, vehicle_id TEXT NOT NULL,
        changed_fields TEXT NOT NULL, changed_at TEXT NOT NULL)""")


def _vehicle_revision(db, vehicle_id):
    return db.execute(
        "SELECT COALESCE(MAX(id),0) FROM hq_vehicle_edit_audit WHERE vehicle_id=?",
        (vehicle_id,)).fetchone()[0]


def _vehicle_fields(payload):
    values = {
        "make": _field(payload, "make", 60, True),
        "model": _field(payload, "model", 60, True),
        "model_year": _field(payload, "model_year", 4),
        "registration": _field(payload, "registration", 20).upper(),
    }
    year = values["model_year"]
    if year and (len(year) != 4 or not year.isascii() or not year.isdecimal()
                 or not 1950 <= int(year) <= date.today().year + 1):
        raise ValueError("Enter a valid vehicle year or leave it blank")
    if not re.fullmatch(r"[A-Z0-9 -]*", values["registration"]):
        raise ValueError("Enter a valid registration using letters, numbers, spaces or hyphens")
    return values


def native_vehicle_record(vehicle_id):
    """Owner-facing vehicle snapshot, including durable revision and lock state."""
    if not isinstance(vehicle_id, str) or not HEX_ID.fullmatch(vehicle_id):
        raise ValueError("Invalid vehicle reference")
    with connect() as db:
        _schema(db)
        _vehicle_tables(db)
        db.execute("BEGIN")
        row = db.execute("""SELECT v.*,c.name AS customer_name,
            (SELECT COALESCE(MAX(a.id),0) FROM hq_vehicle_edit_audit a
             WHERE a.vehicle_id=v.id) AS revision,
            EXISTS(SELECT 1 FROM hq_work_records w WHERE w.vehicle_id=v.id
                AND w.status NOT IN ('draft','ready_for_review')) AS locked
            FROM hq_vehicles v JOIN hq_customers c ON c.id=v.customer_id
            WHERE v.id=?""", (vehicle_id,)).fetchone()
    if row is None:
        raise LookupError("Vehicle not found")
    return dict(row)


def update_native_vehicle(vehicle_id, payload, expected_revision):
    """Owner-only vehicle correction under a SQLite write lock.

    Only vehicles with exclusively editable draft work may be updated.
    Approved/closed work retains its historical vehicle identity until a
    separately approved correction/versioning workflow is implemented.
    """
    if not isinstance(vehicle_id, str) or not HEX_ID.fullmatch(vehicle_id):
        raise ValueError("Invalid vehicle reference")
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1:
        raise ValueError("Invalid vehicle revision")
    values = _vehicle_fields(payload)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _vehicle_tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM hq_vehicles WHERE id=?", (vehicle_id,)).fetchone()
        if row is None:
            raise LookupError("Vehicle not found")
        changed = [k for k in VEHICLE_KEYS if values[k] != row[k]]
        if not changed:
            return _vehicle_revision(db, vehicle_id)
        if db.execute("""SELECT 1 FROM hq_work_records WHERE vehicle_id=?
            AND status NOT IN ('draft','ready_for_review') LIMIT 1""",
            (vehicle_id,)).fetchone() is not None:
            raise ValueError("A linked job is approved or closed. Vehicle details are locked to protect the work history.")
        if _vehicle_revision(db, vehicle_id) != expected_revision:
            raise StaleWorkRecordError("This vehicle changed since you opened the editor. Reload before saving.")
        registration = values["registration"]
        if registration and db.execute("""SELECT 1 FROM hq_vehicles
            WHERE id != ? AND replace(upper(registration),' ','') =
                replace(upper(?),' ','') LIMIT 1""",
            (vehicle_id, registration)).fetchone() is not None:
            raise ValueError("That registration is already used by another HQ vehicle. Review the existing record.")
        db.execute("""UPDATE hq_vehicles SET make=?, model=?, model_year=?, registration=?
            WHERE id=?""", (*(values[k] for k in VEHICLE_KEYS), vehicle_id))
        # Store only changed field names. Never copy registration or vehicle
        # identifiers into a second audit column beyond the stable vehicle ID.
        db.execute("""INSERT INTO hq_vehicle_edit_audit(vehicle_id,changed_fields,changed_at)
            VALUES (?,?,?)""", (vehicle_id, ",".join(changed), _now()))
        return _vehicle_revision(db, vehicle_id)


@native_work.route("/hq/vehicles/<vehicle_id>/edit", methods=["GET", "POST"])
@required
def edit_vehicle(vehicle_id):
    try:
        current = native_vehicle_record(vehicle_id)
    except (ValueError, LookupError):
        abort(404)
    if request.method == "POST":
        if request.content_length is None or request.content_length > 8192:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
            abort(403)
        raw_revision = request.form.get("revision", "")
        if not re.fullmatch(r"[0-9]{1,18}", raw_revision):
            abort(400)
        try:
            update_native_vehicle(vehicle_id, request.form.to_dict(flat=True), int(raw_revision))
        except StaleWorkRecordError as exc:
            current = native_vehicle_record(vehicle_id)
            return render_template("native_vehicle.html", vehicle=current, csrf=session["csrf"],
                                   values={}, error=str(exc)), 409
        except ValueError as exc:
            current = native_vehicle_record(vehicle_id)
            return render_template("native_vehicle.html", vehicle=current, csrf=session["csrf"],
                                   values=request.form, error=str(exc)), 400
        except (OSError, sqlite3.Error):
            current_app.logger.exception("Native vehicle edit unavailable")
            abort(503)
        return redirect(url_for(".edit_vehicle", vehicle_id=vehicle_id, saved="1"), 303)
    return render_template("native_vehicle.html", vehicle=current, csrf=session["csrf"],
                           values={}, error=None)


@native_work.route("/hq/work/<work_id>/progress", methods=["GET", "POST"])
@required
def edit_work_progress(work_id):
    """Save an internal milestone on a canonical HQ job, never a booking/payment."""
    try:
        current = work_progress_snapshot(work_id)
    except (ValueError, LookupError):
        abort(404)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Work progress store unavailable")
        abort(503)
    if request.method == "POST":
        if request.content_length is None or request.content_length > 8192:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
            abort(403)
        raw_revision = request.form.get("revision", "")
        if not re.fullmatch(r"[0-9]{1,18}", raw_revision):
            abort(400)
        try:
            record_work_progress(work_id, request.form.get("stage", ""),
                                 int(raw_revision), session.get("google_sub"))
        except StaleWorkRecordError as exc:
            latest = work_progress_snapshot(work_id)
            return render_template("hq_work_progress.html", work=latest,
                                   stages=WORK_STAGES, csrf=session["csrf"],
                                   error=str(exc)), 409
        except ValueError as exc:
            latest = work_progress_snapshot(work_id)
            return render_template("hq_work_progress.html", work=latest,
                                   stages=WORK_STAGES, csrf=session["csrf"],
                                   error=str(exc)), 400
        except (OSError, sqlite3.Error, RuntimeError):
            current_app.logger.exception("Work progress write unavailable")
            abort(503)
        return redirect(url_for(".edit_work_progress", work_id=work_id, saved="1"), 303)
    return render_template("hq_work_progress.html", work=current,
                           stages=WORK_STAGES, csrf=session["csrf"], error=None)


@native_work.route("/hq/work/<work_id>/time", methods=["GET", "POST"])
@required
def edit_work_time(work_id):
    """Owner-only actual-time ledger; public enquiries never see this page."""
    try:
        current = time_ledger_snapshot(work_id)
    except (ValueError, LookupError):
        abort(404)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Work time store unavailable")
        abort(503)
    if request.method == "POST":
        if request.content_length is None or request.content_length > 8192:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
            abort(403)
        revision = request.form.get("revision", "")
        if not re.fullmatch(r"[0-9]{1,18}", revision):
            abort(400)
        try:
            append_work_time(work_id, request.form.to_dict(flat=True), int(revision),
                             session.get("google_sub"))
        except (StaleWorkRecordError, ValueError) as exc:
            latest = time_ledger_snapshot(work_id)
            status = 409 if isinstance(exc, StaleWorkRecordError) else 400
            return render_template("hq_work_time.html", work=latest, stages=WORK_STAGES,
                csrf=session["csrf"], operation_id=request.form.get("operation_id", ""),
                values=request.form, error=str(exc), timer=timer_snapshot(work_id),
                timer_operation=uuid.uuid4().hex, allowance_operation=uuid.uuid4().hex,
                 timer_history=timer_history(work_id)), status
        except (OSError, sqlite3.Error, RuntimeError):
            current_app.logger.exception("Work time write unavailable")
            abort(503)
        return redirect(url_for(".edit_work_time", work_id=work_id, saved="1"), 303)
    timer = timer_snapshot(work_id)
    review = timer and timer["state"] == "review"
    # Keep the timer usable if the optional combined-cost view is unavailable.
    # The cost snapshot is internally consistent; never mix different work revisions.
    costs = None
    try:
        candidate = cost_overview_snapshot(work_id)
        if candidate["revision"] == current["revision"]:
            costs = candidate
    except (ValueError, LookupError, OSError, sqlite3.Error, RuntimeError):
        current_app.logger.warning("Timer combined cost overview unavailable")
    response = current_app.make_response(render_template("hq_work_time.html", work=current, stages=WORK_STAGES,
        csrf=session["csrf"], operation_id=timer["id"] if review else uuid.uuid4().hex,
        values={"minutes": str(timer["active_seconds"] // 60)} if review else {},
        error=None, timer=timer, timer_operation=uuid.uuid4().hex, allowance_operation=uuid.uuid4().hex,
        timer_history=timer_history(work_id), combined_costs=costs))
    response.headers["Cache-Control"] = "no-store"
    return response


@native_work.post("/hq/work/<work_id>/timer")
@required
def change_job_timer(work_id):
    if request.content_length is None or request.content_length > 2048:
        abort(413)
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
        abort(403)
    revision = request.form.get("timer_revision", "")
    if not re.fullmatch(r"-1|[0-9]{1,18}", revision):
        abort(400)
    try:
        timer_transition(work_id, request.form.get("action", ""), int(revision),
                         request.form.get("operation_id", ""), request.form.get("timer_session", ""),
                         request.form.get("allowance_minutes", ""), request.form.get("reason", ""))
    except StaleWorkRecordError:
        return redirect(url_for(".edit_work_time", work_id=work_id, conflict="1"), 303)
    except (ValueError, LookupError) as exc:
        current = time_ledger_snapshot(work_id)
        timer = timer_snapshot(work_id)
        review = timer and timer["state"] == "review"
        return render_template("hq_work_time.html", work=current, stages=WORK_STAGES,
            csrf=session["csrf"], operation_id=timer["id"] if review else uuid.uuid4().hex,
            values={"minutes": str(timer["active_seconds"] // 60)} if review else {},
            error=str(exc), timer=timer, timer_operation=uuid.uuid4().hex, allowance_operation=uuid.uuid4().hex,
                 timer_history=timer_history(work_id)), 400
    except (OSError, sqlite3.Error):
        abort(503)
    return redirect(url_for(".edit_work_time", work_id=work_id), 303)


@native_work.route("/hq/work/<work_id>/time/<entry_id>/void", methods=["POST"])
@required
def void_work_time_entry(work_id, entry_id):
    if request.content_length is None or request.content_length > 2048:
        abort(413)
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
        abort(403)
    revision = request.form.get("revision", "")
    if not re.fullmatch(r"[0-9]{1,18}", revision):
        abort(400)
    try:
        void_work_time(work_id, entry_id, int(revision), session.get("google_sub"))
    except StaleWorkRecordError:
        return redirect(url_for(".edit_work_time", work_id=work_id, conflict="1"), 303)
    except (ValueError, LookupError):
        abort(400)
    except (OSError, sqlite3.Error, RuntimeError):
        current_app.logger.exception("Work time reversal unavailable")
        abort(503)
    return redirect(url_for(".edit_work_time", work_id=work_id, voided="1"), 303)


@native_work.post("/hq/work/<work_id>/time/<entry_id>/rate")
@required
def correct_time_entry_rate(work_id, entry_id):
    if request.content_length is None or request.content_length > 2048:
        abort(413)
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
        abort(403)
    revision = request.form.get("revision", "")
    if not re.fullmatch(r"[0-9]{1,18}", revision):
        abort(400)
    try:
        correct_work_time_rate(work_id, entry_id, request.form.to_dict(flat=True),
                               int(revision), session.get("google_sub"))
    except StaleWorkRecordError:
        return redirect(url_for(".edit_work_time", work_id=work_id, conflict="1"), 303)
    except (ValueError, LookupError):
        return redirect(url_for(".edit_work_time", work_id=work_id, rate_invalid="1"), 303)
    except (OSError, sqlite3.Error, RuntimeError):
        current_app.logger.exception("Work time rate correction unavailable")
        abort(503)
    return redirect(url_for(".edit_work_time", work_id=work_id, rate_corrected="1"), 303)


@native_work.post("/hq/work/<work_id>/time/<entry_id>/minutes")
@required
def correct_time_entry_minutes(work_id, entry_id):
    if request.content_length is None or request.content_length > 2048:
        abort(413)
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
        abort(403)
    revision = request.form.get("revision", "")
    if not re.fullmatch(r"[0-9]{1,18}", revision):
        abort(400)
    try:
        correct_work_time_minutes(work_id, entry_id, request.form.to_dict(flat=True),
                                  int(revision), session.get("google_sub"))
    except StaleWorkRecordError:
        return redirect(url_for(".edit_work_time", work_id=work_id, conflict="1"), 303)
    except (ValueError, LookupError):
        return redirect(url_for(".edit_work_time", work_id=work_id, minutes_invalid="1"), 303)
    except (OSError, sqlite3.Error, RuntimeError):
        current_app.logger.exception("Work time minutes correction unavailable")
        abort(503)
    return redirect(url_for(".edit_work_time", work_id=work_id, minutes_corrected="1"), 303)


@native_work.route("/hq/work/<work_id>/materials", methods=["GET", "POST"])
@required
def edit_work_materials(work_id):
    """Actual material-use ledger; never an automatic stock or finance write."""
    try:
        current = material_ledger_snapshot(work_id)
    except (ValueError, LookupError):
        abort(404)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Materials ledger unavailable")
        abort(503)
    if request.method == "POST":
        if request.content_length is None or request.content_length > 8192:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
            abort(403)
        raw_revision = request.form.get("revision", "")
        if not re.fullmatch(r"[0-9]{1,18}", raw_revision):
            abort(400)
        try:
            append_work_material(work_id, request.form.to_dict(flat=True), int(raw_revision),
                                 session.get("google_sub"))
        except (StaleWorkRecordError, ValueError) as exc:
            latest = material_ledger_snapshot(work_id)
            status = 409 if isinstance(exc, StaleWorkRecordError) else 400
            return render_template("hq_work_materials.html", work=latest, units=UNITS,
                csrf=session["csrf"], operation_id=request.form.get("operation_id", ""),
                values=request.form, error=str(exc)), status
        except (OSError, sqlite3.Error, RuntimeError):
            current_app.logger.exception("Material usage write unavailable")
            abort(503)
        return redirect(url_for(".edit_work_materials", work_id=work_id, saved="1"), 303)
    return render_template("hq_work_materials.html", work=current, units=UNITS,
        csrf=session["csrf"], operation_id=uuid.uuid4().hex, values={}, error=None)


@native_work.route("/hq/work/<work_id>/materials/<entry_id>/void", methods=["POST"])
@required
def void_work_material_entry(work_id, entry_id):
    if request.content_length is None or request.content_length > 2048:
        abort(413)
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
        abort(403)
    raw_revision = request.form.get("revision", "")
    if not re.fullmatch(r"[0-9]{1,18}", raw_revision):
        abort(400)
    try:
        void_work_material(work_id, entry_id, int(raw_revision), session.get("google_sub"))
    except StaleWorkRecordError:
        return redirect(url_for(".edit_work_materials", work_id=work_id, conflict="1"), 303)
    except (ValueError, LookupError):
        abort(400)
    except (OSError, sqlite3.Error, RuntimeError):
        current_app.logger.exception("Material reversal unavailable")
        abort(503)
    return redirect(url_for(".edit_work_materials", work_id=work_id, voided="1"), 303)


@native_work.route("/hq/work/archived", methods=["GET"])
@required
def archived_work_index():
    """Owner-only list of archived native records, never a public endpoint."""
    raw_offset = request.args.get("offset", "0")
    if not re.fullmatch(r"[0-9]{1,6}", raw_offset) or int(raw_offset) > 100000:
        abort(400)
    try:
        listing = list_archived_work(int(raw_offset))
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Archived work list unavailable")
        abort(503)
    return render_template("hq_work_archive.html", listing=listing, current=None,
                           csrf=session["csrf"], error=None)


@native_work.route("/hq/work/<work_id>/archive", methods=["GET", "POST"])
@required
def archive_work(work_id):
    """Versioned archive/restore. No lifecycle, payment or provider side effects."""
    try:
        current = archived_work_snapshot(work_id)
    except (ValueError, LookupError):
        abort(404)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Native work archive unavailable")
        abort(503)
    if request.method == "POST":
        if request.content_length is None or request.content_length > 2048:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
            abort(403)
        raw_revision = request.form.get("revision", "")
        if not re.fullmatch(r"[0-9]{1,18}", raw_revision):
            abort(400)
        try:
            set_work_archived(work_id, request.form.get("action", ""),
                              int(raw_revision), request.form.get("operation_id", ""))
        except (StaleWorkRecordError, ValueError) as exc:
            latest = archived_work_snapshot(work_id)
            return render_template("hq_work_archive.html", current=latest,
                listing=None, csrf=session["csrf"], operation_id=uuid.uuid4().hex,
                error=str(exc)), 409 if isinstance(exc, StaleWorkRecordError) else 400
        except (OSError, sqlite3.Error):
            current_app.logger.exception("Native work archive write unavailable")
            abort(503)
        return redirect(url_for(".archive_work", work_id=work_id, saved="1"), 303)
    return render_template("hq_work_archive.html", current=current, listing=None,
                           csrf=session["csrf"], operation_id=uuid.uuid4().hex,
                           error=None)


@native_work.get("/hq/work/<work_id>/costs")
@required
def work_cost_overview(work_id):
    """Private, point-in-time recorded-cost report; never infer final profit."""
    try:
        snapshot = cost_overview_snapshot(work_id)
    except (ValueError, LookupError):
        abort(404)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Native work cost overview unavailable")
        abort(503)
    response = current_app.make_response(render_template(
        "hq_work_cost_overview.html", work=snapshot))
    response.headers["Cache-Control"] = "no-store"
    return response


@native_work.route("/hq/work/<work_id>/expenses", methods=["GET", "POST"])
@required
def edit_work_expenses(work_id):
    """Canonical owner-only job allocations; never a payment, invoice or stock write."""
    try:
        current = expense_ledger_snapshot(work_id)
    except (ValueError, LookupError):
        abort(404)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Expenses ledger unavailable")
        abort(503)
    if request.method == "POST":
        if request.content_length is None or request.content_length > 8192:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
            abort(403)
        revision = request.form.get("revision", "")
        if not re.fullmatch(r"[0-9]{1,18}", revision):
            abort(400)
        try:
            append_work_expense(work_id, request.form.to_dict(flat=True), int(revision),
                                session.get("google_sub"))
        except (StaleWorkRecordError, ValueError) as exc:
            latest = expense_ledger_snapshot(work_id)
            status = 409 if isinstance(exc, StaleWorkRecordError) else 400
            return render_template("hq_work_expenses.html", work=latest,
                expense_types=EXPENSE_TYPES, csrf=session["csrf"],
                operation_id=request.form.get("operation_id", ""), values=request.form,
                error=str(exc)), status
        except (OSError, sqlite3.Error, RuntimeError):
            current_app.logger.exception("Expense allocation unavailable")
            abort(503)
        return redirect(url_for(".edit_work_expenses", work_id=work_id, saved="1"), 303)
    return render_template("hq_work_expenses.html", work=current,
        expense_types=EXPENSE_TYPES, csrf=session["csrf"],
        operation_id=uuid.uuid4().hex, values={}, error=None)


@native_work.route("/hq/work/<work_id>/expenses/<entry_id>/void", methods=["POST"])
@required
def void_work_expense_entry(work_id, entry_id):
    if request.content_length is None or request.content_length > 2048:
        abort(413)
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
        abort(403)
    revision = request.form.get("revision", "")
    if not re.fullmatch(r"[0-9]{1,18}", revision):
        abort(400)
    try:
        void_work_expense(work_id, entry_id, int(revision), session.get("google_sub"))
    except StaleWorkRecordError:
        return redirect(url_for(".edit_work_expenses", work_id=work_id, conflict="1"), 303)
    except (ValueError, LookupError):
        abort(400)
    except (OSError, sqlite3.Error, RuntimeError):
        current_app.logger.exception("Expense reversal unavailable")
        abort(503)
    return redirect(url_for(".edit_work_expenses", work_id=work_id, voided="1"), 303)


@native_work.get('/hq/timer')
@required
def timer_jobs():
    from hq_core_records import search_work_records
    query=request.args.get('q','')
    try:
        page=search_work_records(query=query,kind='job',limit=50)
    except ValueError:
        abort(400)
    try:
        unfinished = open_timer_jobs()
    except (OSError, sqlite3.Error, RuntimeError):
        current_app.logger.exception('Timer recovery unavailable')
        abort(503)
    response = current_app.make_response(render_template('timer_jobs.html',page=page,query=query,unfinished=unfinished))
    response.headers["Cache-Control"] = "no-store"
    return response
