"""Owner and authenticated-customer PDF for *approved* native service quotes.

Uses the same canonical HQ customer, vehicle, work and enquiry records.
Fails closed when quote totals or allocations disagree; never reads private
tariffs, costs, descriptions, finance or historical Sheets. No writes.
"""
import io
import json
import re
import sqlite3
from xml.sax.saxutils import escape

from flask import Blueprint, abort, current_app, redirect, send_file
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from hq_auth import required
from hq_customer_portal import active_customer
from hq_service_quotes import _db
from customer_request_store import SERVICES

service_quote_pdfs = Blueprint("service_quote_pdfs", __name__)
_ID = re.compile(r"[0-9a-f]{32}\Z")
_ALLOWED = frozenset(("approved", "in_progress", "completed"))


class QuoteNotPrintable(ValueError):
    """Quote is not yet approved, itemised and reconciled for the customer."""


def approved_quote_snapshot(ident, *, customer_id=None):
    """One transaction and a strict public-field allowlist.

    If customer_id is supplied, it must match the canonical customer and the
    same visibility rules still apply. The owner uses customer_id=None.
    """
    if not isinstance(ident, str) or not _ID.fullmatch(ident):
        raise LookupError("Service quote not found")
    with _db() as db:
        db.execute("BEGIN")
        quote = db.execute("""SELECT q.id,q.customer_id,q.services_json,
            q.status AS enquiry_status,q.approved_total_pence,q.public_note,
            q.created_at,l.work_id,w.status AS work_status,w.kind,
            w.price_pence,COALESCE(a.is_archived,0) AS is_archived,
            c.name AS customer_name,v.make,v.model,v.registration
            FROM hq_service_quote_requests q
            JOIN hq_service_quote_work_links l ON l.request_id=q.id
            JOIN hq_work_records w ON w.id=l.work_id
              AND w.customer_id=q.customer_id AND w.vehicle_id=q.vehicle_id
            JOIN hq_customers c ON c.id=q.customer_id
            JOIN hq_vehicles v ON v.id=q.vehicle_id AND v.customer_id=q.customer_id
            LEFT JOIN hq_work_archive_state a ON a.work_id=w.id
            WHERE q.id=?""", (ident,)).fetchone()
        if quote is None or (customer_id is not None and
                             quote["customer_id"] != customer_id):
            raise LookupError("Service quote not found")
        if (quote["enquiry_status"] != "quoted" or
                quote["work_status"] not in _ALLOWED or
                quote["kind"] != "job" or quote["is_archived"] or
                quote["approved_total_pence"] is None or
                quote["approved_total_pence"] <= 0 or
                quote["approved_total_pence"] != quote["price_pence"]):
            raise QuoteNotPrintable("Approved quote totals or state require review")
        lines = [dict(r) for r in db.execute("""SELECT position,service,amount_pence
            FROM hq_service_quote_lines WHERE work_id=? ORDER BY position""",
            (quote["work_id"],)).fetchall()]
        try:
            original = json.loads(quote["services_json"])
        except (TypeError, ValueError):
            raise QuoteNotPrintable("Service scope requires review")
        if (not isinstance(original, list) or not 1 <= len(original) <= 5 or
                len(lines) != len(original) or
                any(not isinstance(service, str) or service not in SERVICES
                    for service in original) or
                any(line["position"] != i or line["service"] != original[i] or
                    type(line["amount_pence"]) is not int or
                    not 0 <= line["amount_pence"] <= 100000000
                    for i, line in enumerate(lines)) or
                sum(line["amount_pence"] for line in lines) != quote["approved_total_pence"]):
            raise QuoteNotPrintable("Service line allocations require review")
        # Never return owner scope descriptions, tariff snapshots, costs,
        # deposit assumptions, financial ledgers, notes or internal IDs.
        return {
            "reference": quote["id"][:8].upper(),
            "date": quote["created_at"][:10],
            "customer_name": quote["customer_name"],
            "vehicle": " ".join(filter(None, (quote["make"], quote["model"]))),
            "registration": quote["registration"],
            "services": [{"name": row["service"], "pence": row["amount_pence"]}
                         for row in lines],
            "total_pence": quote["approved_total_pence"],
            "public_note": quote["public_note"],
            "status": quote["work_status"],
        }


def _money(pence):
    pounds, pennies = divmod(pence, 100)
    return "GBP {:,}.{:02d}".format(pounds, pennies)


def render_quote_pdf(snapshot):
    """Render small, printable A4 PDF. All dynamic text is escaped."""
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=(210*mm, 297*mm), leftMargin=19*mm,
                            rightMargin=19*mm, topMargin=18*mm,
                            bottomMargin=19*mm, pageCompression=1,
                            title="Tommy Tints - Approved Service Quote",
                            author="Tommy Tints Ltd")
    title = ParagraphStyle("brand", fontName="Helvetica-Bold", fontSize=19,
                           leading=23, textColor=colors.HexColor("#b5192e"))
    heading = ParagraphStyle("heading", fontName="Helvetica-Bold", fontSize=13,
                             leading=18)
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=15)
    cell = ParagraphStyle("cell", parent=body, spaceAfter=0)
    footer = ParagraphStyle("foot", parent=body, fontSize=9, leading=13,
                            textColor=colors.HexColor("#474747"))
    esc = lambda value: escape(str(value or ""))
    story = [
        Paragraph("TOMMY TINTS", title),
        Spacer(1, 3*mm),
        Paragraph("Approved service quote - not an invoice", heading),
        Paragraph("Reference: " + esc(snapshot["reference"]) + " | Enquiry date: "
                  + esc(snapshot["date"]), body),
        HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#b5192e")),
        Spacer(1, 5*mm),
        Paragraph("<b>Customer:</b> " + esc(snapshot["customer_name"]), body),
        Paragraph("<b>Vehicle:</b> " + esc(snapshot["vehicle"]) +
                  " | <b>Registration:</b> " + esc(snapshot["registration"]) , body),
        Spacer(1, 6*mm),
        Paragraph("Approved services", heading),
    ]
    rows = [[Paragraph("<b>Service</b>", cell), Paragraph("<b>Approved amount</b>", cell)]]
    for item in snapshot["services"]:
        rows.append([Paragraph(esc(item["name"]), cell),
                     Paragraph(esc(_money(item["pence"])), cell)])
    rows.append([Paragraph("<b>Total approved quote</b>", cell),
                 Paragraph("<b>" + esc(_money(snapshot["total_pence"])) + "</b>", cell)])
    table = Table(rows, colWidths=[115*mm, 57*mm], hAlign="LEFT", repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#edf0f4")),
        ("LINEBELOW", (0,0), (-1,0), .7, colors.HexColor("#b5192e")),
        ("LINEABOVE", (0,-1), (-1,-1), .7, colors.HexColor("#b5192e")),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
    ]))
    story.extend([table, Spacer(1, 7*mm)])
    if snapshot["public_note"]:
        story.extend([Paragraph("Owner's customer note", heading),
                      Paragraph(esc(snapshot["public_note"]).replace("\n", "<br/>"), body),
                      Spacer(1, 7*mm)])
    story.extend([
        HRFlowable(width="100%", thickness=0.5, color=colors.grey),
        Spacer(1, 3*mm),
        Paragraph("This document records an owner-approved service quotation only. "
                  "It is not an invoice or receipt. It does not confirm an appointment, "
                  "payment, deposit, delivery date or stock reservation.", footer),
    ])
    doc.build(story)
    return out.getvalue()


def _deliver(ident, customer_id=None):
    try:
        snapshot = approved_quote_snapshot(ident, customer_id=customer_id)
    except LookupError:
        abort(404)
    except QuoteNotPrintable:
        # Avoid exposing information about another customer's draft/enquiry.
        abort(404 if customer_id is not None else 409)
    except (sqlite3.Error, OSError, RuntimeError):
        current_app.logger.exception("Private service quote PDF unavailable")
        abort(503)
    response = send_file(io.BytesIO(render_quote_pdf(snapshot)),
                         mimetype="application/pdf",
                         as_attachment=True,
                         download_name="TommyTints-Quote-" + snapshot["reference"] + ".pdf")
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@service_quote_pdfs.get("/hq/service-enquiries/<ident>/quote.pdf")
@required
def owner_download(ident):
    return _deliver(ident)


@service_quote_pdfs.get("/customer/service-enquiries/<ident>/quote.pdf")
def customer_download(ident):
    customer_id = active_customer()
    if not customer_id:
        return redirect("/customer/login")
    return _deliver(ident, customer_id=customer_id)
