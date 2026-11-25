"""Owner-approved exact Square evidence for an existing reported card allocation.

A fresh authenticated GetPayment confirms provider status/amount at observation
time. The owner, not Square, confirms invoice identity. Never infer bank cash,
publish payment status, create a second allocation or charge a card.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import secrets
import sqlite3

import requests
from flask import render_template, request, session

from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_receivables import HEX
from hq_receivable_payments import tables as allocation_tables
from hq_square_payments import configured, inspect_exact_payment

PAYMENT_ID = re.compile(r"[A-Za-z0-9_-]{1,120}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class EvidenceConflict(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def tables(db):
    allocation_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_square_invoice_observations (
        payment_id TEXT PRIMARY KEY,
        allocation_id TEXT NOT NULL UNIQUE REFERENCES hq_receivable_allocations(id),
        allocation_revision INTEGER NOT NULL,
        invoice_id TEXT NOT NULL REFERENCES hq_receivables(id),
        invoice_revision INTEGER NOT NULL,
        scope_digest TEXT NOT NULL,
        provider_snapshot TEXT NOT NULL,
        manifest TEXT NOT NULL UNIQUE,
        observed_at TEXT NOT NULL
    )""")


def observation(db, allocation_id):
    row = db.execute("""SELECT * FROM hq_square_invoice_observations
        WHERE allocation_id=?""", (allocation_id,)).fetchone()
    return dict(row) if row else None


def load_allocation(db, allocation_id):
    row = db.execute("""SELECT a.*,r.invoice_ref,r.revision AS invoice_revision,
        r.status AS invoice_status,r.archived AS invoice_archived,r.amount_pence AS invoice_amount,
        c.name AS customer_name
        FROM hq_receivable_allocations a JOIN hq_receivables r ON a.invoice_id=r.id
        JOIN hq_customers c ON r.customer_id=c.id WHERE a.id=?""",
        (allocation_id,)).fetchone()
    if not row:
        raise ValueError("Choose an existing reported HQ allocation.")
    return dict(row)


def eligible(db, allocation_id):
    row = load_allocation(db, allocation_id)
    if row["status"] != "reported" or row["method"] != "card_reported":
        raise ValueError("Only active owner-reported card allocations are eligible.")
    if row["invoice_status"] != "issued" or row["invoice_archived"]:
        raise ValueError("The allocation must belong to an active issued invoice.")
    if not PAYMENT_ID.fullmatch(row["evidence_ref"]):
        raise ValueError("The reported evidence reference must be the exact Square payment ID.")
    if db.execute("""SELECT 1 FROM hq_receivable_allocations WHERE id<>?
        AND status='reported' AND evidence_ref=? COLLATE NOCASE""",
        (allocation_id, row["evidence_ref"])).fetchone():
        raise EvidenceConflict("Another active allocation uses this reference; reconcile it first.")
    if db.execute("""SELECT 1 FROM hq_square_invoice_observations
        WHERE payment_id=? COLLATE NOCASE OR allocation_id=?""",
        (row["evidence_ref"], allocation_id)).fetchone():
        raise EvidenceConflict("This payment or allocation already has retained provider evidence.")
    return row


def provider_check(ref):
    if not configured():
        raise RuntimeError("Square payment read access is not configured.")
    scope = digest(os.environ["HQ_SQUARE_LOCATION_ID"])
    evidence = inspect_exact_payment(ref)
    if not configured() or scope != digest(os.environ["HQ_SQUARE_LOCATION_ID"]):
        raise EvidenceConflict("Square location changed during review.")
    # Unknown refunds cannot be treated as zero. Only an explicitly unrefunded
    # completed GBP payment for the entire single allocation is supported.
    if (evidence["id"] != ref or evidence["status"] != "COMPLETED"
            or evidence["currency"] != "GBP" or type(evidence["total_minor"]) is not int
            or type(evidence["refunded_minor"]) is not int or evidence["refunded_minor"] != 0):
        raise ValueError("Only completed GBP payments with explicitly zero refunds are eligible.")
    return scope, evidence


def preview(row, scope, evidence):
    if evidence["total_minor"] != row["amount_pence"]:
        raise ValueError("Square amount must exactly equal this allocation; split or net settlements are not supported.")
    return digest([row, scope, evidence])


def register(bp):
    @bp.route("/hq/finance/square-invoice-evidence", methods=["GET", "POST"])
    @required
    def square_invoice_evidence():
        if not storage_ready():
            return "Persistent HQ storage unavailable", 503, {"Cache-Control": "no-store"}
        allocation_id = request.args.get("allocation", "")
        row = saved = review = notice = error = None
        status = 200
        try:
            if request.method == "POST":
                request.max_content_length = 4096
                if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "missing")):
                    return "Invalid security token", 403, {"Cache-Control": "no-store"}
                allocation_id = request.form.get("allocation", "")
            if allocation_id and not HEX.fullmatch(allocation_id):
                raise ValueError("Invalid allocation ID.")
            if allocation_id:
                with closing(connect()) as db, db:
                    tables(db)
                    row = load_allocation(db, allocation_id)
                    saved = observation(db, allocation_id)
            if request.method == "POST":
                action = request.form.get("action")
                if action not in ("review", "save") or not allocation_id:
                    raise ValueError("Choose an allocation and review its evidence.")
                manifest = request.form.get("manifest", "")
                if action == "save" and (not DIGEST.fullmatch(manifest)
                        or request.form.get("confirm") != "yes"):
                    raise ValueError("Explicit confirmation of the reviewed provider evidence is required.")
                if saved and action == "save" and secrets.compare_digest(saved["manifest"], manifest):
                    notice = "This provider observation was already saved; no new receipt was created."
                else:
                    with closing(connect()) as db, db:
                        tables(db)
                        row = eligible(db, allocation_id)
                    scope, evidence = provider_check(row["evidence_ref"])
                    expected = preview(row, scope, evidence)
                    if action == "review":
                        review = {"evidence": evidence, "manifest": expected}
                    else:
                        if not secrets.compare_digest(expected, manifest):
                            raise EvidenceConflict("Provider evidence or allocation changed; review again.")
                        with closing(connect()) as db, db:
                            db.execute("PRAGMA foreign_keys=ON")
                            tables(db)
                            db.execute("BEGIN IMMEDIATE")
                            previous = observation(db, allocation_id)
                            if previous and secrets.compare_digest(previous["manifest"], manifest):
                                saved = previous
                            else:
                                row = eligible(db, allocation_id)
                                if not configured() or scope != digest(os.environ["HQ_SQUARE_LOCATION_ID"]):
                                    raise EvidenceConflict("Square location changed; review again.")
                                if not secrets.compare_digest(preview(row, scope, evidence), manifest):
                                    raise EvidenceConflict("Allocation or invoice changed; review again.")
                                db.execute("""INSERT INTO hq_square_invoice_observations
                                    VALUES(?,?,?,?,?,?,?,?,?)""",
                                    (evidence["id"], allocation_id, row["revision"], row["invoice_id"],
                                     row["invoice_revision"], scope, json.dumps(evidence, sort_keys=True),
                                     manifest, datetime.now(timezone.utc).isoformat()))
                                saved = observation(db, allocation_id)
                        notice = "Provider observation saved against the existing allocation. No new cash or receipt entry."
        except EvidenceConflict as exc:
            error, status = str(exc), 409
        except (ValueError, TypeError, KeyError) as exc:
            error, status = "Invalid or ineligible evidence." if isinstance(exc, KeyError) else str(exc), 400
        except (requests.RequestException, RuntimeError, OSError, sqlite3.Error):
            error, status = "Provider read or HQ storage unavailable; no new save confirmed.", 503
        return render_template("hq_square_invoice_evidence.html", allocation_id=allocation_id,
            row=row, saved=saved, review=review, notice=notice, error=error,
            csrf=session.get("csrf", "")), status, {"Cache-Control": "no-store"}
