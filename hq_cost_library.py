"""Editable owner-only native cost assumptions.

This is an independent, versioned catalogue. It does not change existing quote
formulae, live prices, actual job costs or the read-only Google workbook.
"""
import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for

from customer_request_store import connect
from hq_auth import required

cost_library = Blueprint("cost_library", __name__)
_ID = re.compile(r"[0-9a-f]{32}\Z")
_MULTIPLIERS = ("retail", "trade", "maintenance", "insurance")
_FIELDS = ("service_name", "default_hours", "material_cost", "consumables_cost",
           "retail_multiplier", "trade_multiplier", "maintenance_multiplier",
           "insurance_multiplier", "notes", "pricing_method", "approval_rule",
           "breakdown_note")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid cost library reference")
    return value


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_cost_library_items (
        id TEXT PRIMARY KEY, service_name TEXT NOT NULL,
        service_key TEXT NOT NULL UNIQUE,
        default_hours_hundredths INTEGER NOT NULL CHECK(default_hours_hundredths BETWEEN 0 AND 20000),
        material_pence INTEGER NOT NULL CHECK(material_pence BETWEEN 0 AND 100000000),
        consumables_pence INTEGER NOT NULL CHECK(consumables_pence BETWEEN 0 AND 100000000),
        retail_basis_points INTEGER NOT NULL,
        trade_basis_points INTEGER NOT NULL,
        maintenance_basis_points INTEGER NOT NULL,
        insurance_basis_points INTEGER NOT NULL,
        notes TEXT NOT NULL, pricing_method TEXT NOT NULL,
        approval_rule TEXT NOT NULL, breakdown_note TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0, is_archived INTEGER NOT NULL DEFAULT 0
            CHECK(is_archived IN (0,1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_cost_library_operations (
        operation_id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL REFERENCES hq_cost_library_items(id),
        action TEXT NOT NULL CHECK(action IN ('create','save','archive','restore')),
        payload_digest TEXT NOT NULL, result_revision INTEGER NOT NULL,
        occurred_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_cost_library_events (
        event_id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL REFERENCES hq_cost_library_items(id),
        revision INTEGER NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('create','save','archive','restore')),
        owner_fingerprint TEXT NOT NULL,
        before_state TEXT NOT NULL, after_state TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(item_id, revision)
    )""")


def _number(value, label, places, max_value):
    if not isinstance(value, str) or not re.fullmatch(
            r"(?:0|[1-9][0-9]{0,7})(?:\.[0-9]{1," + str(places) + r"})?", value.strip()):
        raise ValueError("Enter a valid " + label + " with up to " + str(places) + " decimal places")
    try:
        number = Decimal(value.strip())
        if not 0 <= number <= Decimal(max_value):
            raise ValueError("Enter a valid " + label + " within the allowed range")
        return int(number * (10 ** places))
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError("Invalid " + label) from exc


def _text(value, label, length, required=False):
    if not isinstance(value, str):
        raise ValueError("Invalid " + label)
    value = value.strip()
    if (len(value) > length or (required and not value)
            or any(ord(c) < 32 and c not in "\t\n" for c in value)):
        raise ValueError("Invalid " + label + " (length or characters)")
    return value


def _fields(form):
    name = _text(form.get("service_name", ""), "service name", 120, True)
    key = " ".join(name.casefold().split())
    result = {
        "service_name": name,
        "service_key": key,
        "default_hours_hundredths": _number(form.get("default_hours", ""), "hours", 2, "200"),
        "material_pence": _number(form.get("material_cost", ""), "materials cost", 2, "1000000"),
        "consumables_pence": _number(form.get("consumables_cost", ""), "consumables cost", 2, "1000000"),
        "notes": _text(form.get("notes", ""), "notes", 800),
        "pricing_method": _text(form.get("pricing_method", ""), "pricing method", 250),
        "approval_rule": _text(form.get("approval_rule", ""), "customer approval rule", 250),
        "breakdown_note": _text(form.get("breakdown_note", ""), "quote breakdown note", 500),
    }
    for tier in _MULTIPLIERS:
        amount = _number(form.get(tier + "_multiplier", ""), tier + " multiplier", 4, "10")
        if amount == 0:
            raise ValueError("Multipliers must be greater than zero")
        result[tier + "_basis_points"] = amount
    return result


def _digest(action, item_id, values, secret):
    body = json.dumps({"action": action, "item_id": item_id, "values": values},
                      sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _actor(secret, owner):
    return hmac.new(secret.encode(), str(owner or "").encode(), hashlib.sha256).hexdigest()


def _public_form(row):
    if row is None:
        return {}
    return {
        "service_name": row["service_name"],
        "default_hours": f'{row["default_hours_hundredths"] / 100:.2f}',
        "material_cost": f'{row["material_pence"] / 100:.2f}',
        "consumables_cost": f'{row["consumables_pence"] / 100:.2f}',
        **{tier + "_multiplier": f'{row[tier + "_basis_points"] / 10000:.4f}'
           for tier in _MULTIPLIERS},
        "notes": row["notes"],
        "pricing_method": row["pricing_method"],
        "approval_rule": row["approval_rule"],
        "breakdown_note": row["breakdown_note"],
    }


def list_items(query="", archived=False):
    if not isinstance(query, str) or len(query) > 80 or type(archived) is not bool:
        raise ValueError("Invalid cost library search")
    with connect() as db:
        _tables(db)
        rows = db.execute("""SELECT id,service_name,default_hours_hundredths,
                 material_pence,consumables_pence,revision,is_archived
            FROM hq_cost_library_items
            WHERE is_archived=? AND instr(lower(service_name),lower(?))>0
            ORDER BY service_name COLLATE NOCASE,id LIMIT 200""",
            (int(archived),query.strip())).fetchall()
        return [dict(r) for r in rows]


def get_item(item_id):
    _id(item_id)
    with connect() as db:
        _tables(db)
        row = db.execute("SELECT * FROM hq_cost_library_items WHERE id=?",(item_id,)).fetchone()
        if row is None:
            raise LookupError("Cost library item not found")
        return dict(row)


def apply_change(*, action, item_id="", expected_revision=None, operation_id, fields, owner, secret):
    """Atomic save with revisions, idempotency receipts and an immutable audit event."""
    if action not in ("create", "save", "archive", "restore"):
        raise ValueError("Invalid action")
    _id(operation_id)
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("Persistent HQ session secret required")
    if action != "create":
        _id(item_id)
        if type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1:
            raise ValueError("Invalid revision")
    else:
        if item_id or expected_revision is not None:
            raise ValueError("Invalid new cost item")
    values = _fields(fields) if action in ("create", "save") else {}
    digest = _digest(action, item_id, values if action in ("create","save")
                     else {"revision":expected_revision}, secret)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT item_id,action,payload_digest FROM hq_cost_library_operations WHERE operation_id=?",
                           (operation_id,)).fetchone()
        if prior:
            if prior["action"] != action or not hmac.compare_digest(prior["payload_digest"], digest):
                raise ValueError("This form was already submitted with different details")
            return prior["item_id"]
        now = _now()
        before = {}
        if action == "create":
            item_id = uuid.uuid4().hex
            data = {"id":item_id,**values,"revision":0,"is_archived":0,
                    "created_at":now,"updated_at":now}
            cols = list(data)
            try:
                db.execute("INSERT INTO hq_cost_library_items (" + ",".join(cols) +
                           ") VALUES (" + ",".join(["?"] * len(cols)) + ")",
                           tuple(data[k] for k in cols))
            except sqlite3.IntegrityError as exc:
                raise ValueError("A service with this name already exists, including archived items") from exc
            after = data
            revision = 0
        else:
            previous = db.execute("SELECT * FROM hq_cost_library_items WHERE id=?",
                                  (item_id,)).fetchone()
            if previous is None:
                raise LookupError("Cost library item not found")
            before = dict(previous)
            if before["revision"] != expected_revision:
                raise ValueError("Cost item was changed. Reload before editing")
            if action == "save" and before["is_archived"]:
                raise ValueError("Restore an archived item before editing")
            if action == "archive" and before["is_archived"]:
                raise ValueError("Cost item already archived")
            if action == "restore" and not before["is_archived"]:
                raise ValueError("Cost item is not archived")
            update = dict(values) if action == "save" else {
                "is_archived": int(action == "archive")}
            update.update(revision=expected_revision+1,updated_at=now)
            assignments = ",".join(k+"=?" for k in update)
            try:
                changed = db.execute("UPDATE hq_cost_library_items SET " + assignments +
                    " WHERE id=? AND revision=?",
                    (*update.values(), item_id, expected_revision))
            except sqlite3.IntegrityError as exc:
                raise ValueError("A service with this name already exists") from exc
            if changed.rowcount != 1:
                raise ValueError("Cost item changed. Reload before editing")
            after = dict(db.execute("SELECT * FROM hq_cost_library_items WHERE id=?",
                                    (item_id,)).fetchone())
            revision = expected_revision + 1
        db.execute("""INSERT INTO hq_cost_library_operations
            (operation_id,item_id,action,payload_digest,result_revision,occurred_at)
            VALUES (?,?,?,?,?,?)""",
            (operation_id,item_id,action,digest,revision,now))
        db.execute("""INSERT INTO hq_cost_library_events
            (event_id,item_id,revision,action,owner_fingerprint,before_state,after_state,occurred_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex,item_id,revision,action,_actor(secret,owner),
             json.dumps(before,sort_keys=True),json.dumps(after,sort_keys=True),now))
    return item_id


@cost_library.after_request
def private_headers(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@cost_library.get("/hq/cost-library")
@required
def index():
    query=request.args.get("q","")
    scope=request.args.get("scope","active")
    if scope not in ("active","archived"):
        abort(400)
    try:
        items=list_items(query,scope=="archived")
    except ValueError:
        abort(400)
    except (OSError,sqlite3.Error):
        current_app.logger.exception("HQ cost library listing unavailable")
        abort(503)
    return render_template("hq_cost_library.html",items=items,query=query,
                           scope=scope,record=None,values={},csrf=session["csrf"],
                           operation_id=uuid.uuid4().hex,error=None)


@cost_library.route("/hq/cost-library/new",methods=["GET","POST"])
@required
def new_item():
    if request.method=="GET":
        return render_template("hq_cost_library.html",items=None,query="",scope="active",
                               record=None,values={},csrf=session["csrf"],
                               operation_id=uuid.uuid4().hex,error=None)
    return _post("create")


@cost_library.route("/hq/cost-library/<item_id>",methods=["GET","POST"])
@required
def edit_item(item_id):
    try:
        record=get_item(item_id)
    except (ValueError,LookupError):
        abort(404)
    except (OSError,sqlite3.Error):
        current_app.logger.exception("HQ cost library read unavailable")
        abort(503)
    if request.method=="GET":
        return render_template("hq_cost_library.html",items=None,query="",scope="active",
                               record=record,values=_public_form(record),csrf=session["csrf"],
                               operation_id=uuid.uuid4().hex,error=None)
    return _post(request.form.get("action","save"),item_id,record)


def _post(action,item_id="",record=None):
    if request.content_length is None or request.content_length > 8192:
        abort(413)
    if not hmac.compare_digest(request.form.get("csrf",""),
                               session.get("csrf","not-set")):
        abort(403)
    values=request.form.to_dict(flat=True)
    try:
        revision = None if action=="create" else int(values.get("revision",""))
        saved=apply_change(action=action,item_id=item_id,expected_revision=revision,
                           operation_id=values.get("operation_id",""),fields=values,
                           owner=session.get("google_sub"),secret=current_app.secret_key)
    except (ValueError,TypeError) as exc:
        return render_template("hq_cost_library.html",items=None,query="",scope="active",
            record=record,values=values,csrf=session["csrf"],
            operation_id=values.get("operation_id",""),error=str(exc)),409
    except LookupError:
        abort(404)
    except (OSError,sqlite3.Error):
        current_app.logger.exception("HQ cost library save unavailable")
        abort(503)
    return redirect(url_for(".edit_item",item_id=saved,saved="1"),code=303)
