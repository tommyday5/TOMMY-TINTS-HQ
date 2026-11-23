"""Versioned owner-only draft cost estimates sourced from native HQ Cost Library.

An estimate is an explicit, frozen planning snapshot. It is NOT actual job
cost, selling price, invoice, payment or a historical Sheets import. Never
propagate it into the pricing engine or public customer records.
"""
import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for
from customer_request_store import connect
from hq_auth import required
from hq_core_records import _schema
from hq_cost_library import _tables as _library_tables

estimate_workspace = Blueprint("estimate_workspace", __name__)
_HEX = re.compile(r"[0-9a-f]{32}\Z")
_NUMBER = re.compile(r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,2})?\Z")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id(value):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError("Invalid HQ reference")
    return value


def _revision(value):
    if type(value) is not int or not 0 <= value < 2**63 - 1:
        raise ValueError("Invalid revision")
    return value


def _amount(value, label, ceiling):
    if not isinstance(value, str) or not _NUMBER.fullmatch(value):
        raise ValueError("Enter " + label + " with up to two decimal places")
    amount = int(Decimal(value) * 100)
    if not 0 <= amount <= ceiling:
        raise ValueError("Invalid " + label)
    return amount


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_work_cost_estimates (
        work_id TEXT PRIMARY KEY REFERENCES hq_work_records(id),
        source_item_id TEXT NOT NULL REFERENCES hq_cost_library_items(id),
        source_item_revision INTEGER NOT NULL,
        service_snapshot TEXT NOT NULL,
        hours_hundredths INTEGER NOT NULL CHECK(hours_hundredths BETWEEN 0 AND 20000),
        material_pence INTEGER NOT NULL CHECK(material_pence BETWEEN 0 AND 100000000),
        consumables_pence INTEGER NOT NULL CHECK(consumables_pence BETWEEN 0 AND 100000000),
        note TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0,
        is_archived INTEGER NOT NULL DEFAULT 0 CHECK(is_archived IN (0,1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_work_cost_estimate_operations (
        operation_id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL REFERENCES hq_work_records(id),
        action TEXT NOT NULL CHECK(action IN ('save','archive','restore')),
        payload_digest TEXT NOT NULL,
        result_revision INTEGER NOT NULL,
        occurred_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_work_cost_estimate_events (
        event_id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL REFERENCES hq_work_records(id),
        revision INTEGER NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('save','archive','restore')),
        owner_fingerprint TEXT NOT NULL,
        before_state TEXT NOT NULL, after_state TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(work_id,revision)
    )""")


def _read(db, work_id):
    work = db.execute("""SELECT w.id,w.kind,w.status,w.revision,w.service,
        c.name AS customer_name,
        COALESCE(a.is_archived,0) AS work_archived
        FROM hq_work_records w JOIN hq_customers c ON c.id=w.customer_id
        LEFT JOIN hq_work_archive_state a ON a.work_id=w.id
        WHERE w.id=?""", (work_id,)).fetchone()
    if work is None:
        raise LookupError("HQ work record not found")
    previous = db.execute(
        "SELECT * FROM hq_work_cost_estimates WHERE work_id=?", (work_id,)
    ).fetchone()
    return dict(work), dict(previous) if previous else None


def estimate_snapshot(work_id):
    _id(work_id)
    with connect() as db:
        _schema(db)
        _library_tables(db)
        _tables(db)
        db.execute("BEGIN")
        work, existing = _read(db, work_id)
        items = [dict(r) for r in db.execute("""SELECT id,service_name,revision,
            default_hours_hundredths,material_pence,consumables_pence
            FROM hq_cost_library_items WHERE is_archived=0
            ORDER BY service_name COLLATE NOCASE,id LIMIT 200""")]
    return work, existing, items


def _fields(payload, action):
    if action not in ("save", "archive", "restore"):
        raise ValueError("Choose a valid estimate action")
    if not hasattr(payload, "get"):
        raise ValueError("Invalid estimate submission")
    values = {
        "operation_id": _id(payload.get("operation_id", "")),
        "work_revision": _revision(int(payload.get("work_revision", "-1"))),
        "expected_revision": _revision(int(payload.get("expected_revision", "-1"))),
    }
    if action == "save":
        note = payload.get("note", "")
        if not isinstance(note, str) or len(note.strip()) > 300 or any(
            ord(char) < 32 and char not in "\n\t" for char in note
        ):
            raise ValueError("Estimate notes must be 300 characters or fewer")
        values.update({
            "source_item_id": _id(payload.get("source_item_id", "")),
            "source_item_revision": _revision(int(payload.get("source_item_revision", "-1"))),
            "hours_hundredths": _amount(payload.get("hours", ""), "planned hours", 20000),
            "material_pence": _amount(payload.get("materials", ""), "estimated materials", 100000000),
            "consumables_pence": _amount(payload.get("consumables", ""), "estimated consumables", 100000000),
            "note": note.strip(),
        })
    return values


def apply_estimate(work_id, action, payload, owner_identity, secret):
    """One atomic owner-approved draft edit, with optimistic locking and retry journal."""
    _id(work_id)
    if not isinstance(secret, str) or not secret:
        raise RuntimeError("Persistent HQ session secret required")
    values = _fields(payload, action)
    body = json.dumps({"work_id": work_id, "action": action, "values": values},
                      sort_keys=True, separators=(",", ":")).encode()
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    actor = hmac.new(secret.encode(), str(owner_identity or "").encode(), hashlib.sha256).hexdigest()
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _library_tables(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("""SELECT work_id,action,payload_digest,result_revision
            FROM hq_work_cost_estimate_operations WHERE operation_id=?""",
            (values["operation_id"],)).fetchone()
        if prior:
            if (prior["work_id"] != work_id or prior["action"] != action or
                    not hmac.compare_digest(prior["payload_digest"], digest)):
                raise ValueError("Estimate submission already used for different details")
            return prior["result_revision"]
        work, previous = _read(db, work_id)
        if work["work_archived"] or work["status"] not in ("draft", "ready_for_review"):
            raise ValueError("Only active draft work can change planning estimates")
        if work["revision"] != values["work_revision"]:
            raise ValueError("The linked work changed. Reload this job before saving")
        if previous:
            if previous["revision"] != values["expected_revision"]:
                raise ValueError("Estimate changed. Reload before editing")
        elif values["expected_revision"] != 0 or action != "save":
            raise ValueError("No current estimate to change")
        if action == "save":
            if previous and previous["is_archived"]:
                raise ValueError("Restore the estimate before editing")
            item = db.execute("SELECT * FROM hq_cost_library_items WHERE id=?",
                              (values["source_item_id"],)).fetchone()
            if item is None:
                raise ValueError("Choose an existing HQ cost assumption")
            same_snapshot = previous and previous["source_item_id"] == item["id"]
            if same_snapshot:
                if values["source_item_revision"] != previous["source_item_revision"]:
                    raise ValueError("The selected source snapshot changed. Reload")
                source_revision = previous["source_item_revision"]
                service = previous["service_snapshot"]
            else:
                if item["is_archived"] or item["revision"] != values["source_item_revision"]:
                    raise ValueError("Cost Library source changed or is archived. Reload")
                source_revision = item["revision"]
                service = item["service_name"]
            changes = {
                "source_item_id": item["id"], "source_item_revision": source_revision,
                "service_snapshot": service,
                "hours_hundredths": values["hours_hundredths"],
                "material_pence": values["material_pence"],
                "consumables_pence": values["consumables_pence"],
                "note": values["note"],
                "is_archived": 0,
            }
        else:
            if not previous or (action == "archive" and previous["is_archived"]) or (
                    action == "restore" and not previous["is_archived"]):
                raise ValueError("Estimate state changed. Reload")
            changes = {"is_archived": int(action == "archive")}
        now = _now()
        before = dict(previous) if previous else {}
        revision = (previous["revision"] + 1) if previous else 0
        if previous:
            assignments = ",".join(key + "=?" for key in changes)
            result = db.execute(
                "UPDATE hq_work_cost_estimates SET " + assignments +
                ",revision=?,updated_at=? WHERE work_id=? AND revision=?",
                (*changes.values(), revision, now, work_id, previous["revision"]))
            if result.rowcount != 1:
                raise ValueError("Estimate changed concurrently. Reload")
        else:
            columns = ["work_id", *changes, "revision", "created_at", "updated_at"]
            params = [work_id, *changes.values(), revision, now, now]
            db.execute("INSERT INTO hq_work_cost_estimates (" + ",".join(columns) +
                       ") VALUES (" + ",".join(["?"] * len(columns)) + ")", params)
        after = dict(db.execute("SELECT * FROM hq_work_cost_estimates WHERE work_id=?",
                                (work_id,)).fetchone())
        db.execute("""INSERT INTO hq_work_cost_estimate_events
            VALUES (?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, work_id, revision, action, actor,
             json.dumps(before,sort_keys=True), json.dumps(after,sort_keys=True), now))
        db.execute("""INSERT INTO hq_work_cost_estimate_operations
            VALUES (?,?,?,?,?,?)""",
            (values["operation_id"], work_id, action, digest, revision, now))
    return revision


@estimate_workspace.after_request
def _privacy(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@estimate_workspace.route("/hq/work/<work_id>/estimate", methods=["GET", "POST"])
@required
def edit_estimate(work_id):
    try:
        work, existing, items = estimate_snapshot(work_id)
    except (ValueError, LookupError):
        abort(404)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("HQ estimate read failed")
        abort(503)
    error = None
    if request.method == "POST":
        if request.content_length is None or request.content_length > 4096:
            abort(413)
        if not hmac.compare_digest(request.form.get("csrf", ""),
                                   session.get("csrf", "not-set")):
            abort(403)
        action = request.form.get("action", "")
        try:
            apply_estimate(work_id, action, request.form,
                           session.get("google_sub"), current_app.secret_key)
        except (ValueError, TypeError, OverflowError) as exc:
            error = str(exc)
        except (OSError, sqlite3.Error):
            current_app.logger.exception("HQ estimate update failed")
            abort(503)
        if error is None:
            return redirect(url_for(".edit_estimate", work_id=work_id, saved="1"), 303)
    chosen = request.args.get("library_id", "")
    if chosen and not _HEX.fullmatch(chosen):
        abort(400)
    source = next((item for item in items if item["id"] == chosen), None)
    if chosen and source is None:
        abort(404)
    if source:
        selected_id, source_revision = source["id"], source["revision"]
        hours, materials, consumables, note = (
            f'{source["default_hours_hundredths"]/100:.2f}',
            f'{source["material_pence"]/100:.2f}',
            f'{source["consumables_pence"]/100:.2f}', "")
    elif existing:
        selected_id, source_revision = existing["source_item_id"], existing["source_item_revision"]
        hours, materials, consumables, note = (
            f'{existing["hours_hundredths"]/100:.2f}',
            f'{existing["material_pence"]/100:.2f}',
            f'{existing["consumables_pence"]/100:.2f}', existing["note"])
    else:
        selected_id, source_revision = "", 0
        hours = materials = consumables = note = ""
    return render_template("hq_work_estimate.html", work=work, existing=existing,
        items=items, selected_id=selected_id, source_revision=source_revision,
        hours=hours, materials=materials, consumables=consumables, note=note,
        csrf=session["csrf"], operation_id=uuid.uuid4().hex, error=error)
