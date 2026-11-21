"""Owner-only, saved customer-safe work descriptions and printable document view.

This deliberately excludes private scope notes, costs, provider data, bank details,
payment status and unverified appointment information. It never sends a document,
books a slot, charges a payment or changes Google Sheets.
"""
import re
import secrets
import sqlite3
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for
from customer_request_store import connect
from hq_auth import required
from hq_core_records import _schema, _audit_work, StaleWorkRecordError

customer_documents = Blueprint("customer_documents", __name__)
_WORK_ID = re.compile(r"[0-9a-f]{32}\Z")
_APPROVED = frozenset(("approved", "in_progress", "completed"))
_MAX_DESCRIPTION = 1200


def _validate_work_id(work_id):
    if not isinstance(work_id, str) or not _WORK_ID.fullmatch(work_id):
        raise ValueError("Invalid HQ work ID")


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_work_customer_documents (
        work_id TEXT PRIMARY KEY REFERENCES hq_work_records(id),
        public_description TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")


def document_snapshot(work_id):
    """Consistent point-in-time public-safe fields; caller must enforce owner auth."""
    _validate_work_id(work_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        row = db.execute("""SELECT w.id,w.kind,w.status,w.revision,w.service,
            w.price_pence,w.preferred_date,w.preferred_time,
            c.name AS customer_name,v.make,v.model,v.registration,
            COALESCE(a.is_archived,0) AS is_archived,
            COALESCE(doc.public_description,'') AS public_description,
            doc.updated_at AS description_updated_at
            FROM hq_work_records w
            JOIN hq_customers c ON c.id=w.customer_id
            JOIN hq_vehicles v ON v.id=w.vehicle_id
            LEFT JOIN hq_work_archive_state a ON a.work_id=w.id
            LEFT JOIN hq_work_customer_documents doc ON doc.work_id=w.id
            WHERE w.id=?""", (work_id,)).fetchone()
    if row is None:
        raise LookupError("HQ work not found")
    result = dict(row)
    price = row["price_pence"]
    result["approved_price"] = (
        "£" + f"{price // 100:,}.{price % 100:02d}"
        if row["status"] in _APPROVED and price is not None else None
    )
    result["can_edit"] = not bool(row["is_archived"]) and row["status"] != "cancelled"
    result["can_print"] = bool(result["approved_price"] is not None
                               and row["public_description"].strip()
                               and not row["is_archived"])
    return result


def save_public_description(work_id, description, expected_revision):
    """Owner-approved write only; idempotent same-value retries and stale-write lock."""
    _validate_work_id(work_id)
    if not isinstance(description, str) or len(description.strip()) > _MAX_DESCRIPTION:
        raise ValueError("Customer-facing description must be 1,200 characters or fewer")
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1:
        raise ValueError("Invalid work revision")
    cleaned = description.strip()
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT w.revision,w.status,
            COALESCE(a.is_archived,0) AS is_archived,
            COALESCE(doc.public_description,'') AS public_description
            FROM hq_work_records w
            LEFT JOIN hq_work_archive_state a ON a.work_id=w.id
            LEFT JOIN hq_work_customer_documents doc ON doc.work_id=w.id
            WHERE w.id=?""", (work_id,)).fetchone()
        if row is None:
            raise LookupError("HQ work not found")
        if row["is_archived"] or row["status"] == "cancelled":
            raise ValueError("Archived or cancelled work is read-only")
        # Identical replay succeeds without a second write, even after unrelated
        # work events have advanced the canonical revision.
        if cleaned == row["public_description"]:
            return row["revision"]
        if row["revision"] != expected_revision:
            raise StaleWorkRecordError("This work changed. Reload before saving the customer document.")
        revision = row["revision"] + 1
        now = datetime.now(timezone.utc).isoformat()
        db.execute("""INSERT INTO hq_work_customer_documents
            (work_id,public_description,updated_at) VALUES (?,?,?)
            ON CONFLICT(work_id) DO UPDATE SET
                public_description=excluded.public_description,
                updated_at=excluded.updated_at""", (work_id, cleaned, now))
        saved = db.execute("""UPDATE hq_work_records SET revision=revision+1
            WHERE id=? AND revision=?""", (work_id, expected_revision))
        if saved.rowcount != 1:
            raise StaleWorkRecordError("This work changed. Reload before saving the customer document.")
        # Never store description text or customer PII in the audit JSON.
        _audit_work(db, work_id, revision, "customer_document",
                    {"changed_fields": ["public_description"], "revision": expected_revision},
                    {"changed_fields": ["public_description"], "revision": revision})
    return revision


@customer_documents.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@customer_documents.route("/hq/work/<work_id>/customer-document", methods=["GET", "POST"])
@required
def work_document(work_id):
    try:
        _validate_work_id(work_id)
    except ValueError:
        abort(404)
    if request.method == "POST":
        if request.content_length is None or request.content_length > 5000:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
            abort(403)
        raw_revision = request.form.get("revision", "")
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,17})", raw_revision):
            abort(400)
        try:
            save_public_description(work_id, request.form.get("public_description", ""),
                                    int(raw_revision))
        except LookupError:
            abort(404)
        except (ValueError, StaleWorkRecordError) as exc:
            try:
                snapshot = document_snapshot(work_id)
            except LookupError:
                abort(404)
            status = 409 if isinstance(exc, StaleWorkRecordError) else 400
            return render_template("hq_customer_document.html", record=snapshot,
                                   csrf=session["csrf"], error=str(exc)), status
        except (OSError, sqlite3.Error):
            current_app.logger.exception("Customer work document write unavailable")
            abort(503)
        return redirect(url_for(".work_document", work_id=work_id, saved="1"), code=303)
    try:
        snapshot = document_snapshot(work_id)
    except LookupError:
        abort(404)
    except (OSError, sqlite3.Error):
        current_app.logger.exception("Customer work document read unavailable")
        abort(503)
    return render_template("hq_customer_document.html", record=snapshot,
                           csrf=session["csrf"], error=None)
