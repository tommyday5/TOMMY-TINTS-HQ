"""Owner-editable item prices and approved PDFs for vehicle-free product quotes.

All prices live with canonical HQ quotations. Commerce stock and historic Sheets
are never altered. Customer documents expose only allowlisted public fields.
"""
import io
import json
import re
import secrets
import sqlite3
import uuid
from decimal import Decimal

from flask import (Blueprint, abort, current_app, redirect, render_template,
                   request, send_file, session)

from hq_auth import required
from hq_combined_baskets import _tables as basket_tables, _valid_id, _now
from hq_core_records import StaleWorkRecordError
from hq_customer_portal import active_customer
from hq_quote_pdf import QuoteNotPrintable, render_quote_pdf
from hq_service_quotes import _db as canonical_db

product_quote_lines = Blueprint("product_quote_lines", __name__)
_REV = re.compile(r"(?:0|[1-9][0-9]{0,8})\Z")
_PRICE = re.compile(r"[0-9]{1,7}(?:\.[0-9]{1,2})?\Z")


def _tables(db):
    basket_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_product_quote_lines (
        quote_id TEXT NOT NULL REFERENCES hq_combined_product_quotes(id),
        position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 30),
        line_type TEXT NOT NULL CHECK(line_type IN ('product','delivery')),
        source_ref TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 100),
        amount_pence INTEGER NOT NULL CHECK(amount_pence BETWEEN 0 AND 100000000),
        PRIMARY KEY(quote_id,position)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_product_quote_line_audit (
        id TEXT PRIMARY KEY,
        quote_id TEXT NOT NULL REFERENCES hq_combined_product_quotes(id),
        revision INTEGER NOT NULL,
        lines_json TEXT NOT NULL,
        actor TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(quote_id,revision)
    )""")


def _source_lines(quote):
    try:
        products = json.loads(quote["products_json"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Original product snapshot requires review") from exc
    if not isinstance(products, list) or not 1 <= len(products) <= 30:
        raise ValueError("Original product snapshot requires review")
    lines, seen = [], set()
    for item in products:
        if not isinstance(item, dict):
            raise ValueError("Original product snapshot requires review")
        ref = _valid_id(item.get("id"), "source product")
        qty = item.get("quantity")
        name, variant = item.get("name"), item.get("variant")
        if (ref in seen or type(qty) is not int or not 1 <= qty <= 100
                or not isinstance(name, str) or not 1 <= len(name) <= 120
                or not isinstance(variant, str) or len(variant) > 100):
            raise ValueError("Original product snapshot requires review")
        seen.add(ref)
        lines.append({"position": len(lines), "line_type": "product",
                      "source_ref": ref, "quantity": qty,
                      "label": name + ((" - " + variant) if variant else "")})
    if quote["fulfilment"] == "delivery":
        lines.append({"position": len(lines), "line_type": "delivery",
                      "source_ref": "delivery", "quantity": 1, "label": "Delivery"})
    elif quote["fulfilment"] != "collection":
        raise ValueError("Original fulfilment requires review")
    return lines


def _load(db, ident):
    row = db.execute("""SELECT q.*, b.status AS basket_status,
        b.revision AS source_revision,b.approved_total_pence AS source_total,
        b.products_json AS source_products,b.fulfilment AS source_fulfilment,
        b.created_at AS basket_created_at,b.public_note,
        c.name AS customer_name
        FROM hq_combined_product_quotes q
        JOIN hq_combined_baskets b ON b.id=q.basket_id AND b.customer_id=q.customer_id
        JOIN hq_customers c ON c.id=q.customer_id
        WHERE q.basket_id=?""", (ident,)).fetchone()
    if row is None:
        raise LookupError("Product-only quotation not found")
    quote = dict(row)
    if (quote["basket_status"] != "quoted" or
            quote["source_revision"] != quote["basket_revision"] or
            quote["source_total"] != quote["approved_total_pence"] or
            quote["source_products"] != quote["products_json"] or
            quote["source_fulfilment"] != quote["fulfilment"]):
        raise ValueError("Original product offer changed; owner review required")
    expected = _source_lines(quote)
    stored = [dict(x) for x in db.execute("""SELECT position,line_type,source_ref,
        quantity,amount_pence FROM hq_product_quote_lines
        WHERE quote_id=? ORDER BY position""", (quote["id"],)).fetchall()]
    if stored and (len(stored) != len(expected) or any(
            any(old[k] != source[k] for k in (
                "position", "line_type", "source_ref", "quantity"))
            for old, source in zip(stored, expected))):
        raise ValueError("Stored item identities differ from source; review required")
    quote["lines"] = [
        {**line, "amount_pence": stored[i]["amount_pence"] if stored else None}
        for i, line in enumerate(expected)]
    quote["fully_allocated"] = bool(stored) and (
        sum(line["amount_pence"] for line in stored) == quote["approved_total_pence"])
    quote["editable"] = quote["status"] == "draft"
    return quote


def product_quote_details(ident):
    _valid_id(ident, "basket")
    with canonical_db() as db:
        _tables(db)
        return _load(db, ident)


def _price(value):
    if not isinstance(value, str) or not _PRICE.fullmatch(value):
        raise ValueError("Enter a price for each item with at most two decimals")
    result = int(Decimal(value) * 100)
    if result > 100000000:
        raise ValueError("Line price exceeds allowed amount")
    return result


def save_product_quote_lines(ident, expected_revision, entries, actor="owner"):
    """Atomic owner-only CAS allocation, audit snapshot and idempotent retry."""
    _valid_id(ident, "basket")
    if type(expected_revision) is not int or not 0 <= expected_revision <= 100000000:
        raise ValueError("Invalid quotation revision")
    if not isinstance(entries, list):
        raise ValueError("Invalid item prices")
    with canonical_db() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        quote = _load(db, ident)
        if len(entries) != len(quote["lines"]):
            raise ValueError("Price every source item including delivery")
        proposed = []
        for source, price in zip(quote["lines"], entries):
            proposed.append({
                "position": source["position"], "line_type": source["line_type"],
                "source_ref": source["source_ref"], "quantity": source["quantity"],
                "amount_pence": _price(price),
            })
        if sum(x["amount_pence"] for x in proposed) != quote["approved_total_pence"]:
            raise ValueError("Itemised prices must exactly equal the approved customer total")
        if quote["fully_allocated"] and all(
                old["amount_pence"] == new["amount_pence"]
                for old, new in zip(quote["lines"], proposed)):
            return quote["revision"]
        if not quote["editable"]:
            raise ValueError("Only draft product-only quotations can be edited")
        if quote["revision"] != expected_revision:
            raise StaleWorkRecordError("Product quote changed; reload before saving")
        db.execute("DELETE FROM hq_product_quote_lines WHERE quote_id=?", (quote["id"],))
        db.executemany("""INSERT INTO hq_product_quote_lines
            (quote_id,position,line_type,source_ref,quantity,amount_pence)
            VALUES (?,?,?,?,?,?)""", [
                (quote["id"], x["position"], x["line_type"], x["source_ref"],
                 x["quantity"], x["amount_pence"]) for x in proposed])
        updated = db.execute("""UPDATE hq_combined_product_quotes
            SET revision=revision+1,updated_at=?
            WHERE id=? AND revision=? AND status='draft'""",
            (_now(), quote["id"], expected_revision))
        if updated.rowcount != 1:
            raise StaleWorkRecordError("Product quote changed; reload")
        revision = expected_revision + 1
        when = _now()
        db.execute("""INSERT INTO hq_combined_product_quote_events
            VALUES (?,?,?,?,?,?)""", (
            uuid.uuid4().hex, quote["id"], revision, "item_allocated",
            str(actor)[:100], when))
        db.execute("""INSERT INTO hq_product_quote_line_audit
            VALUES (?,?,?,?,?,?)""", (
            uuid.uuid4().hex, quote["id"], revision,
            json.dumps(proposed, sort_keys=True, separators=(",", ":")),
            str(actor)[:100], when))
        return revision


def approved_product_quote_snapshot(ident, *, customer_id=None):
    """Never show unapproved, stale, partial or another customer's quote."""
    _valid_id(ident, "basket")
    with canonical_db() as db:
        _tables(db)
        db.execute("BEGIN")
        quote = _load(db, ident)
    if customer_id is not None and quote["customer_id"] != customer_id:
        raise LookupError("Product quotation not found")
    if (quote["status"] != "approved" or not quote["fully_allocated"] or
            type(quote["approved_total_pence"]) is not int or
            quote["approved_total_pence"] <= 0):
        raise QuoteNotPrintable("Owner approval and reconciled item prices required")
    return {
        "reference": quote["id"][:8].upper(),
        "date": quote["basket_created_at"][:10],
        "customer_name": quote["customer_name"],
        "vehicle": "", "registration": "",
        "services": [
            {"name": (str(line["quantity"]) + " × " if line["line_type"] == "product" else "")
             + line["label"], "pence": line["amount_pence"]}
            for line in quote["lines"]],
        "total_pence": quote["approved_total_pence"],
        "public_note": quote["public_note"],
        "status": quote["status"],
        "combined": True, "products_only": True,
    }


@product_quote_lines.route("/hq/combined-enquiries/<ident>/product-line-prices",
                           methods=["GET", "POST"])
@required
def owner_product_lines(ident):
    error, form_values = None, {}
    if request.method == "POST":
        if request.content_length is None or request.content_length > 9000:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""),
                                      session.get("csrf", "missing")):
            abort(403)
        raw = request.form.get("revision", "")
        if not _REV.fullmatch(raw):
            abort(400)
        form_values = request.form
        try:
            quote = product_quote_details(ident)
            prices = [request.form.get("price_" + str(i), "")
                      for i in range(len(quote["lines"]))]
            save_product_quote_lines(ident, int(raw), prices,
                                     session.get("google_sub", "owner"))
            return redirect("/hq/combined-enquiries/" + ident +
                            "/product-line-prices?saved=1", code=303)
        except StaleWorkRecordError as exc:
            error = str(exc)
        except (ValueError, LookupError) as exc:
            error = str(exc)
        except (sqlite3.Error, OSError, RuntimeError):
            current_app.logger.exception("Product quote allocation failed")
            abort(503)
    try:
        quote = product_quote_details(ident)
    except (ValueError, LookupError):
        abort(404)
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Product quote allocation unavailable")
        abort(503)
    return render_template("hq_product_quote_lines.html", quote=quote,
                           csrf=session.get("csrf", ""), error=error,
                           form_values=form_values), (409 if error else 200)


def _download(ident, customer_id=None):
    try:
        snapshot = approved_product_quote_snapshot(ident, customer_id=customer_id)
    except LookupError:
        abort(404)
    except (QuoteNotPrintable, ValueError):
        abort(404 if customer_id is not None else 409)
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Product quote PDF unavailable")
        abort(503)
    response = send_file(io.BytesIO(render_quote_pdf(snapshot)),
                         mimetype="application/pdf", as_attachment=True,
                         download_name="TommyTints-Products-Quote-" +
                         snapshot["reference"] + ".pdf")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@product_quote_lines.get("/hq/combined-enquiries/<ident>/product-quote.pdf")
@required
def owner_product_pdf(ident):
    return _download(ident)


@product_quote_lines.get("/customer/basket-requests/<ident>/product-quote.pdf")
def customer_product_pdf(ident):
    customer_id = active_customer()
    if not customer_id:
        return redirect("/customer/login")
    return _download(ident, customer_id=customer_id)
