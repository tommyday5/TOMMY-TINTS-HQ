"""Versioned service/product/delivery allocations for linked combined HQ quotes.

The source enquiry is immutable and remains a retail reference, not an invoice.
Only an owner may allocate prices; a customer can see allocations only after
native job approval and an exact reconciliation. No stock, money, booking,
Sheets or external-message side effects.
"""
import hashlib
import io
import json
import re
import secrets
import sqlite3
import uuid
from decimal import Decimal

from flask import Blueprint, abort, current_app, redirect, render_template, request, send_file, session

from customer_request_store import SERVICES
from hq_auth import required
from hq_combined_baskets import _tables as basket_tables, _valid_id, _now
from hq_core_records import _audit_work, StaleWorkRecordError
from hq_customer_portal import active_customer
from hq_quote_pdf import QuoteNotPrintable, render_quote_pdf
from hq_service_quotes import _db as canonical_db

combined_quote_lines = Blueprint("combined_quote_lines", __name__)
_REV = re.compile(r"(?:0|[1-9][0-9]{0,8})\Z")
_PRICE = re.compile(r"[0-9]{1,7}(?:\.[0-9]{1,2})?\Z")
_PUBLIC_STATUSES = frozenset(("approved", "in_progress", "completed"))


def _tables(db):
    basket_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_combined_quote_lines (
        work_id TEXT NOT NULL REFERENCES hq_work_records(id),
        position INTEGER NOT NULL CHECK(position >= 0 AND position <= 40),
        line_type TEXT NOT NULL CHECK(line_type IN ('service','product','delivery')),
        source_ref TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 100),
        description TEXT NOT NULL DEFAULT '',
        amount_pence INTEGER NOT NULL CHECK(amount_pence BETWEEN 0 AND 100000000),
        PRIMARY KEY(work_id,position)
    )""")


def _source_lines(row):
    """Rebuild immutable display labels and identities from the original snapshot."""
    try:
        services = json.loads(row["services_json"])
        products = json.loads(row["products_json"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Original basket snapshot requires review") from exc
    if not isinstance(services, list) or not 1 <= len(services) <= 5:
        raise ValueError("Original service selection requires review")
    if not isinstance(products, list) or len(products) > 30:
        raise ValueError("Original product selection requires review")
    lines, seen = [], set()
    for item in services:
        if not isinstance(item, dict) or item.get("service") not in SERVICES:
            raise ValueError("Original service selection requires review")
        service = item["service"]
        if service in seen:
            raise ValueError("Duplicate source service")
        seen.add(service)
        lines.append({"position": len(lines), "line_type": "service",
                      "source_ref": service, "quantity": 1, "label": service})
    for item in products:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("Original product selection requires review")
        ref = _valid_id(item["id"], "source product")
        qty, name, variant = item.get("quantity"), item.get("name"), item.get("variant")
        if (ref in seen or type(qty) is not int or not 1 <= qty <= 100
                or not isinstance(name, str) or not 1 <= len(name) <= 120
                or not isinstance(variant, str) or len(variant) > 100):
            raise ValueError("Original product snapshot needs review")
        seen.add(ref)
        label = (name + (" - " + variant if variant else "")).strip()
        lines.append({"position": len(lines), "line_type": "product",
                      "source_ref": ref, "quantity": qty, "label": label})
    if row["fulfilment"] == "delivery":
        lines.append({"position": len(lines), "line_type": "delivery",
                      "source_ref": "delivery", "quantity": 1, "label": "Delivery"})
    elif row["fulfilment"] != "collection":
        raise ValueError("Original fulfilment requires review")
    return lines


def _load(db, ident):
    row = db.execute("""SELECT b.id AS basket_id,b.customer_id,b.vehicle_id,b.status AS basket_status,
        b.created_at,b.public_note,b.services_json,b.products_json,b.fulfilment,
        b.approved_total_pence,l.work_id,l.approved_total_pence AS linked_total_pence,
        l.confirmed_tier,w.customer_id AS work_customer_id,
        w.vehicle_id AS work_vehicle_id,w.kind,w.status AS work_status,
        w.price_pence AS work_total_pence,w.revision AS work_revision,
        COALESCE(a.is_archived,0) AS is_archived,
        c.name AS customer_name,v.make,v.model,v.registration
        FROM hq_combined_baskets b
        JOIN hq_combined_basket_work_links l ON l.basket_id=b.id
        JOIN hq_work_records w ON w.id=l.work_id
        JOIN hq_customers c ON c.id=b.customer_id
        JOIN hq_vehicles v ON v.id=b.vehicle_id AND v.customer_id=b.customer_id
        LEFT JOIN hq_work_archive_state a ON a.work_id=w.id
        WHERE b.id=?""", (ident,)).fetchone()
    if row is None:
        raise LookupError("Linked combined quote not found")
    result = dict(row)
    expected = _source_lines(row)
    stored = [dict(r) for r in db.execute("""SELECT position,line_type,source_ref,quantity,
        description,amount_pence FROM hq_combined_quote_lines
        WHERE work_id=? ORDER BY position""", (row["work_id"],))]
    if stored and (len(stored) != len(expected) or any(
            any(old[k] != new[k] for k in ("position", "line_type", "source_ref", "quantity"))
            for old, new in zip(stored, expected))):
        raise ValueError("Saved allocations differ from the original basket; manual review needed")
    result["lines"] = [{**item, "description": stored[i]["description"] if stored else "",
                        "amount_pence": stored[i]["amount_pence"] if stored else None}
                       for i, item in enumerate(expected)]
    result["fully_allocated"] = bool(stored) and (
        sum(item["amount_pence"] for item in stored) == row["approved_total_pence"]
        == row["linked_total_pence"] == row["work_total_pence"])
    result["editable"] = (row["kind"] == "quote" and row["work_status"] in (
        "draft", "ready_for_review") and not row["is_archived"])
    return result


def combined_quote_details(ident):
    _valid_id(ident, "combined request")
    with canonical_db() as db:
        _tables(db)
        return _load(db, ident)


def _parse_price(value):
    if not isinstance(value, str) or not _PRICE.fullmatch(value):
        raise ValueError("Enter each item price in pounds with at most two decimal places")
    amount = int(Decimal(value) * 100)
    if amount > 100000000:
        raise ValueError("Line price exceeds the allowed limit")
    return amount


def save_combined_quote_lines(ident, expected_revision, entries, actor="owner"):
    """Atomic CAS update. Exact retries are safe; every change is work-audited."""
    _valid_id(ident, "combined request")
    if type(expected_revision) is not int or not 0 <= expected_revision <= 100000000:
        raise ValueError("Invalid work revision")
    if not isinstance(entries, list):
        raise ValueError("Invalid quote allocations")
    with canonical_db() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        quote = _load(db, ident)
        if len(entries) != len(quote["lines"]):
            raise ValueError("Every original basket item needs an owner-approved price")
        proposed = []
        for source, entry in zip(quote["lines"], entries):
            if not isinstance(entry, dict) or entry.get("line_type") != source["line_type"] or (
                    entry.get("source_ref") != source["source_ref"]):
                raise ValueError("Source service, product and delivery items cannot change")
            description = entry.get("description", "")
            if not isinstance(description, str) or len(description.strip()) > 200:
                raise ValueError("Private item description must be at most 200 characters")
            proposed.append({
                "position": source["position"], "line_type": source["line_type"],
                "source_ref": source["source_ref"], "quantity": source["quantity"],
                "description": description.strip(), "amount_pence": _parse_price(entry.get("price")),
            })
        total = sum(line["amount_pence"] for line in proposed)
        if total != quote["approved_total_pence"] or total != quote["linked_total_pence"]:
            raise ValueError("Item prices must reconcile exactly to the owner-reviewed basket quote")
        if quote["work_total_pence"] != quote["approved_total_pence"]:
            raise ValueError("Native HQ work price differs from approved basket quote; reconcile first")
        original = [{k: v for k, v in row.items() if k in (
            "position", "line_type", "source_ref", "quantity", "description", "amount_pence")}
            for row in quote["lines"]] if quote["fully_allocated"] else []
        if original == proposed:
            return quote["work_revision"]  # idempotent retry after a saved revision
        if not quote["editable"]:
            raise ValueError("Only active, unapproved native quotes can be allocated")
        if quote["work_revision"] != expected_revision:
            raise StaleWorkRecordError("HQ work has changed; reload its item allocations")
        db.execute("DELETE FROM hq_combined_quote_lines WHERE work_id=?", (quote["work_id"],))
        db.executemany("""INSERT INTO hq_combined_quote_lines
            (work_id,position,line_type,source_ref,quantity,description,amount_pence)
            VALUES (?,?,?,?,?,?,?)""", [
                (quote["work_id"], x["position"], x["line_type"], x["source_ref"],
                 x["quantity"], x["description"], x["amount_pence"]) for x in proposed])
        updated = db.execute("""UPDATE hq_work_records SET revision=revision+1
            WHERE id=? AND revision=?""", (quote["work_id"], expected_revision))
        if updated.rowcount != 1:
            raise StaleWorkRecordError("HQ work changed; reload before saving")
        revision = expected_revision + 1
        def evidence(rows):
            return [{"type": x["line_type"], "ref": x["source_ref"],
                     "quantity": x["quantity"], "pence": x["amount_pence"],
                     "private_note_hash": hashlib.sha256(x["description"].encode()).hexdigest()}
                    for x in rows]
        _audit_work(db, quote["work_id"], revision, "combined_line_allocation",
                    {"lines": evidence(original)},
                    {"lines": evidence(proposed), "verified_total_pence": total,
                     "original_basket_id": ident})
        return revision


def approved_combined_snapshot(ident, *, customer_id=None):
    """Public PDF allowlist: no margin, costs, private notes or other customers."""
    _valid_id(ident, "combined request")
    with canonical_db() as db:
        _tables(db)
        db.execute("BEGIN")
        quote = _load(db, ident)
    if customer_id is not None and quote["customer_id"] != customer_id:
        raise LookupError("Combined quote not found")
    if (quote["basket_status"] != "quoted" or quote["kind"] != "job"
            or quote["work_status"] not in _PUBLIC_STATUSES or quote["is_archived"]
            or quote["customer_id"] != quote["work_customer_id"]
            or quote["vehicle_id"] != quote["work_vehicle_id"]
            or not quote["fully_allocated"] or not quote["approved_total_pence"]
            or quote["approved_total_pence"] <= 0):
        raise QuoteNotPrintable("Combined quote is not approved and fully reconciled")
    return {
        "reference": quote["basket_id"][:8].upper(),
        "date": quote["created_at"][:10],
        "customer_name": quote["customer_name"],
        "vehicle": " ".join(filter(None, (quote["make"], quote["model"]))),
        "registration": quote["registration"],
        "services": [{"name": (str(x["quantity"]) + " × " if x["line_type"] == "product" else "")
                      + x["label"], "pence": x["amount_pence"]} for x in quote["lines"]],
        "total_pence": quote["approved_total_pence"],
        "public_note": quote["public_note"],
        "status": quote["work_status"],
        "combined": True,
    }


@combined_quote_lines.route("/hq/combined-enquiries/<ident>/line-prices", methods=["GET", "POST"])
@required
def owner_lines(ident):
    error, form_values = None, {}
    if request.method == "POST":
        if request.content_length is None or request.content_length > 11000:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""),
                                      session.get("csrf", "missing")):
            abort(403)
        raw = request.form.get("revision", "")
        if not _REV.fullmatch(raw):
            abort(400)
        form_values = request.form
        try:
            current = combined_quote_details(ident)
            entries = [
                {"line_type": line["line_type"], "source_ref": line["source_ref"],
                 "price": request.form.get("price_" + str(i), ""),
                 "description": request.form.get("description_" + str(i), "")}
                for i, line in enumerate(current["lines"])
            ]
            save_combined_quote_lines(ident, int(raw), entries,
                                      session.get("google_sub", "owner"))
            return redirect("/hq/combined-enquiries/" + ident + "/line-prices?saved=1", code=303)
        except StaleWorkRecordError as exc:
            error = str(exc)
        except (ValueError, LookupError) as exc:
            error = str(exc)
        except (sqlite3.Error, OSError, RuntimeError):
            current_app.logger.exception("Combined item allocations unavailable")
            abort(503)
    try:
        quote = combined_quote_details(ident)
    except (ValueError, LookupError):
        abort(404)
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Combined quote allocation unavailable")
        abort(503)
    return render_template("hq_combined_lines.html", quote=quote,
                           csrf=session.get("csrf", ""),
                           error=error, form_values=form_values), (409 if error else 200)


def _download(ident, customer_id=None):
    try:
        snapshot = approved_combined_snapshot(ident, customer_id=customer_id)
    except (ValueError, LookupError):
        abort(404)
    except QuoteNotPrintable:
        abort(404 if customer_id is not None else 409)
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Combined PDF could not be prepared")
        abort(503)
    response = send_file(io.BytesIO(render_quote_pdf(snapshot)),
                         mimetype="application/pdf", as_attachment=True,
                         download_name="TommyTints-Combined-Quote-" + snapshot["reference"] + ".pdf")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@combined_quote_lines.get("/hq/combined-enquiries/<ident>/quote.pdf")
@required
def owner_pdf(ident):
    return _download(ident)


@combined_quote_lines.get("/customer/basket-requests/<ident>/quote.pdf")
def customer_pdf(ident):
    customer_id = active_customer()
    if not customer_id:
        return redirect("/customer/login")
    return _download(ident, customer_id=customer_id)
