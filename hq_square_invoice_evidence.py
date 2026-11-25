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


def recheck_tables(db):
    tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_square_invoice_rechecks (
        id INTEGER PRIMARY KEY,
        payment_id TEXT NOT NULL REFERENCES hq_square_invoice_observations(payment_id),
        allocation_revision INTEGER NOT NULL,
        invoice_revision INTEGER NOT NULL,
        provider_snapshot TEXT NOT NULL,
        evidence_digest TEXT NOT NULL,
        observed_at TEXT NOT NULL
    )""")


def correction_tables(db):
    recheck_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_square_full_refund_corrections (
        allocation_id TEXT PRIMARY KEY REFERENCES hq_receivable_allocations(id),
        payment_id TEXT NOT NULL UNIQUE REFERENCES hq_square_invoice_observations(payment_id),
        recheck_id INTEGER NOT NULL REFERENCES hq_square_invoice_rechecks(id),
        manifest TEXT NOT NULL UNIQUE,
        provider_snapshot TEXT NOT NULL,
        prior_revision INTEGER NOT NULL,
        result_revision INTEGER NOT NULL,
        corrected_at TEXT NOT NULL
    )""")


def partial_tables(db):
    correction_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_square_partial_refund_corrections (
        id INTEGER PRIMARY KEY,
        allocation_id TEXT NOT NULL REFERENCES hq_receivable_allocations(id),
        recheck_id INTEGER NOT NULL UNIQUE REFERENCES hq_square_invoice_rechecks(id),
        manifest TEXT NOT NULL UNIQUE,
        prior_amount INTEGER NOT NULL,
        result_amount INTEGER NOT NULL CHECK(result_amount > 0 AND result_amount < prior_amount),
        prior_revision INTEGER NOT NULL,
        result_revision INTEGER NOT NULL,
        corrected_at TEXT NOT NULL,
        UNIQUE(allocation_id,result_revision)
    )""")


def partial_history(db, allocation_id):
    return [dict(r) for r in db.execute("""SELECT * FROM hq_square_partial_refund_corrections
        WHERE allocation_id=? ORDER BY id DESC""", (allocation_id,))]


def refund_baseline(db, row, original):
    """Validate the original gross amount and every approved local reduction."""
    initial = json.loads(original["provider_snapshot"])
    gross = initial["total_minor"]
    amount = gross
    revision = original["allocation_revision"]
    for prior in reversed(partial_history(db, row["id"])):
        if (prior["prior_amount"] != amount or prior["prior_revision"] != revision
                or prior["result_revision"] != revision + 1
                or not 0 < prior["result_amount"] < amount):
            raise EvidenceConflict("Prior refund correction history conflicts with this allocation.")
        amount, revision = prior["result_amount"], prior["result_revision"]
    if (type(gross) is not int or gross <= 0 or row["amount_pence"] != amount
            or row["revision"] != revision):
        raise EvidenceConflict("Allocation changed outside the retained refund correction history.")
    return gross


def correction(db, allocation_id):
    row = db.execute("SELECT * FROM hq_square_full_refund_corrections WHERE allocation_id=?",
                     (allocation_id,)).fetchone()
    return dict(row) if row else None


def full_refund_context(db, allocation_id, partial=False):
    row = load_allocation(db, allocation_id)
    original = observation(db, allocation_id)
    if (not original or row["status"] != "reported" or row["method"] != "card_reported"
            or row["invoice_status"] != "issued" or row["invoice_archived"]
            or row["evidence_ref"] != original["payment_id"]
            or row["invoice_id"] != original["invoice_id"]):
        raise ValueError("Only an unchanged active Square-observed allocation on an issued invoice is eligible.")
    gross = refund_baseline(db, row, original)
    latest = recheck_history(db, original["payment_id"])
    if not latest:
        raise ValueError("Retain a fresh full refund observation first.")
    event = latest[0]
    evidence = event["evidence"]
    if (event["allocation_revision"] != row["revision"]
            or event["invoice_revision"] != row["invoice_revision"]
            or digest(evidence) != event["evidence_digest"]
            or evidence["id"] != original["payment_id"]
            or evidence["currency"] != "GBP" or evidence["status"] != "COMPLETED"
            or type(evidence["total_minor"]) is not int
            or type(evidence["refunded_minor"]) is not int
            or evidence["total_minor"] != gross):
        raise ValueError("Only exact retained completed GBP refund evidence qualifies.")
    remaining = gross - evidence["refunded_minor"]
    if partial:
        if not 0 < remaining < row["amount_pence"]:
            raise ValueError("Only an increased cumulative partial refund qualifies.")
    elif remaining != 0:
        raise ValueError("Only a retained exact full refund qualifies; partial or unknown refunds require separate review.")
    return row, original, event


def full_refund_review(allocation_id, manifest=None):
    """Owner-approved local reversal only; never send a refund or claim bank settlement."""
    with closing(connect()) as db, db:
        partial_tables(db)
        prior = correction(db, allocation_id)
        if prior:
            if manifest and secrets.compare_digest(prior["manifest"], manifest):
                return None
            raise EvidenceConflict("A full refund correction is already recorded.")
        context = full_refund_context(db, allocation_id)
    row, original, event = context
    if not configured() or digest(os.environ["HQ_SQUARE_LOCATION_ID"]) != original["scope_digest"]:
        raise EvidenceConflict("Original Square location cannot be verified.")
    evidence = inspect_exact_payment(original["payment_id"])
    if (not configured() or digest(os.environ["HQ_SQUARE_LOCATION_ID"]) != original["scope_digest"]
            or evidence != event["evidence"]):
        raise EvidenceConflict("Provider evidence changed; retain a new recheck before review.")
    expected = digest(["square-full-refund-reversal-v1", row, original, event])
    if manifest is None:
        return {"manifest": expected, "amount_pence": row["amount_pence"],
                "payment_id": original["payment_id"], "recheck_id": event["id"]}
    if not secrets.compare_digest(expected, manifest):
        raise EvidenceConflict("Invoice, allocation or evidence changed; review again.")
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        partial_tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = correction(db, allocation_id)
        if prior:
            if secrets.compare_digest(prior["manifest"], manifest):
                return None
            raise EvidenceConflict("A different full refund correction was saved.")
        if (not configured() or digest(os.environ["HQ_SQUARE_LOCATION_ID"]) != original["scope_digest"]
                or full_refund_context(db, allocation_id) != context):
            raise EvidenceConflict("Invoice, allocation or evidence changed; review again.")
        stamp = datetime.now(timezone.utc).isoformat()
        revision = row["revision"] + 1
        db.execute("""UPDATE hq_receivable_allocations SET status='reversed',revision=?,updated_at=?
            WHERE id=?""", (revision, stamp, allocation_id))
        snapshot = {k: row[k] for k in ("id","invoice_id","amount_pence","received_on",
                                        "method","evidence_ref","memo")}
        snapshot.update(status="reversed", revision=revision,
                        correction_reason="square_full_refund", provider_recheck_id=event["id"])
        db.execute("INSERT INTO hq_receivable_allocation_events VALUES(?,?,?,?,?)",
                   (secrets.token_hex(16), allocation_id, revision, json.dumps(snapshot, sort_keys=True), stamp))
        db.execute("""INSERT INTO hq_square_full_refund_corrections VALUES(?,?,?,?,?,?,?,?)""",
                   (allocation_id, original["payment_id"], event["id"], manifest,
                    json.dumps(evidence, sort_keys=True), row["revision"], revision, stamp))
    return None


def partial_refund_review(allocation_id, manifest=None):
    """Apply only the incremental reduction implied by a fresh cumulative refund."""
    with closing(connect()) as db, db:
        partial_tables(db)
        if manifest and db.execute("""SELECT 1 FROM hq_square_partial_refund_corrections
                WHERE allocation_id=? AND manifest=?""", (allocation_id, manifest)).fetchone():
            return None
        context = full_refund_context(db, allocation_id, partial=True)
    row, original, event = context
    if not configured() or digest(os.environ["HQ_SQUARE_LOCATION_ID"]) != original["scope_digest"]:
        raise EvidenceConflict("Original Square location cannot be verified.")
    evidence = inspect_exact_payment(original["payment_id"])
    if (not configured() or digest(os.environ["HQ_SQUARE_LOCATION_ID"]) != original["scope_digest"]
            or evidence != event["evidence"]):
        raise EvidenceConflict("Provider evidence changed; retain a new recheck before review.")
    remaining = evidence["total_minor"] - evidence["refunded_minor"]
    expected = digest(["square-partial-refund-reduction-v1", row, original, event])
    if manifest is None:
        return {"manifest": expected, "prior_amount": row["amount_pence"],
                "remaining": remaining, "reduction": row["amount_pence"] - remaining,
                "cumulative_refund": evidence["refunded_minor"]}
    if not secrets.compare_digest(expected, manifest):
        raise EvidenceConflict("Invoice, allocation or evidence changed; review again.")
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        partial_tables(db)
        db.execute("BEGIN IMMEDIATE")
        if db.execute("""SELECT 1 FROM hq_square_partial_refund_corrections
                WHERE allocation_id=? AND manifest=?""", (allocation_id, manifest)).fetchone():
            return None
        if (not configured() or digest(os.environ["HQ_SQUARE_LOCATION_ID"]) != original["scope_digest"]
                or full_refund_context(db, allocation_id, partial=True) != context):
            raise EvidenceConflict("Invoice, allocation or evidence changed; review again.")
        stamp = datetime.now(timezone.utc).isoformat()
        revision = row["revision"] + 1
        db.execute("""UPDATE hq_receivable_allocations SET amount_pence=?,revision=?,updated_at=?
            WHERE id=?""", (remaining, revision, stamp, allocation_id))
        snapshot = {k: row[k] for k in ("id","invoice_id","received_on","method","evidence_ref","memo")}
        snapshot.update(status="reported", revision=revision, amount_pence=remaining,
                        prior_amount_pence=row["amount_pence"], correction_reason="square_partial_refund",
                        provider_recheck_id=event["id"], cumulative_refund_pence=evidence["refunded_minor"])
        db.execute("INSERT INTO hq_receivable_allocation_events VALUES(?,?,?,?,?)",
                   (secrets.token_hex(16), allocation_id, revision, json.dumps(snapshot, sort_keys=True), stamp))
        db.execute("""INSERT INTO hq_square_partial_refund_corrections
            (allocation_id,recheck_id,manifest,prior_amount,result_amount,prior_revision,result_revision,corrected_at)
            VALUES(?,?,?,?,?,?,?,?)""", (allocation_id, event["id"], manifest, row["amount_pence"],
            remaining, row["revision"], revision, stamp))
    return None


def recheck_history(db, payment_id):
    return [dict(r) | {"evidence": json.loads(r["provider_snapshot"])}
            for r in db.execute("""SELECT * FROM hq_square_invoice_rechecks
                WHERE payment_id=? ORDER BY id DESC LIMIT 50""", (payment_id,))]


def recheck_saved(allocation_id):
    """Explicit owner action: append changed provider evidence only, never cash."""
    with closing(connect()) as db, db:
        recheck_tables(db)
        original = observation(db, allocation_id)
        if not original:
            raise ValueError("Save the initial exact Square observation first.")
        row = load_allocation(db, allocation_id)
        history = recheck_history(db, original["payment_id"])
    if not configured() or digest(os.environ["HQ_SQUARE_LOCATION_ID"]) != original["scope_digest"]:
        raise EvidenceConflict("The original Square location cannot be verified.")
    evidence = inspect_exact_payment(original["payment_id"])
    if (evidence["id"] != original["payment_id"] or evidence["currency"] != "GBP"
            or type(evidence["total_minor"]) is not int or evidence["total_minor"] < 0
            or evidence["status"] not in ("APPROVED", "PENDING", "COMPLETED", "CANCELED", "FAILED")
            or (evidence["refunded_minor"] is not None and
                (type(evidence["refunded_minor"]) is not int
                 or not 0 <= evidence["refunded_minor"] <= evidence["total_minor"]))):
        raise ValueError("Invalid provider recheck evidence.")
    previous = history[0]["evidence"] if history else json.loads(original["provider_snapshot"])
    if (evidence["created_at"] != previous["created_at"]
            or datetime.fromisoformat(evidence["updated_at"]) <
               datetime.fromisoformat(previous["updated_at"])):
        raise EvidenceConflict("Provider identity or update timestamp conflicts with retained evidence.")
    fingerprint = digest(evidence)
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        recheck_tables(db)
        db.execute("BEGIN IMMEDIATE")
        if (not configured() or digest(os.environ["HQ_SQUARE_LOCATION_ID"]) != original["scope_digest"]
                or observation(db, allocation_id) != original or load_allocation(db, allocation_id) != row):
            raise EvidenceConflict("Location, invoice or allocation changed; recheck again.")
        latest = recheck_history(db, original["payment_id"])
        if latest and latest[0]["evidence_digest"] == fingerprint:
            return "Provider evidence is unchanged; no duplicate observation or receipt was created."
        if (latest[0]["id"] if latest else None) != (history[0]["id"] if history else None):
            raise EvidenceConflict("Another provider recheck was saved; recheck again.")
        if fingerprint == digest(previous):
            return "Provider evidence is unchanged; no duplicate observation or receipt was created."
        db.execute("""INSERT INTO hq_square_invoice_rechecks
            (payment_id,allocation_revision,invoice_revision,provider_snapshot,evidence_digest,observed_at)
            VALUES(?,?,?,?,?,?)""", (original["payment_id"], row["revision"], row["invoice_revision"],
            json.dumps(evidence, sort_keys=True), fingerprint, datetime.now(timezone.utc).isoformat()))
    return "Changed provider observation retained. Review any refund or status change; no allocation or cash was changed."


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
        row = saved = review = notice = error = refund_review = refund_correction = None
        history = partial_corrections = []
        partial_review = gross = None
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
                    partial_tables(db)
                    partial_corrections = partial_history(db, allocation_id)
                    refund_correction = correction(db, allocation_id)
                    row = load_allocation(db, allocation_id)
                    saved = observation(db, allocation_id)
                    if saved:
                        gross = json.loads(saved["provider_snapshot"])["total_minor"]
                        history = recheck_history(db, saved["payment_id"])
            if request.method == "POST":
                action = request.form.get("action")
                if action in ("partial-review", "partial-save") and allocation_id:
                    if action == "partial-save":
                        manifest = request.form.get("manifest", "")
                        if not DIGEST.fullmatch(manifest) or request.form.get("confirm") != "yes":
                            raise ValueError("Explicit approval of the exact partial refund correction is required.")
                        partial_refund_review(allocation_id, manifest)
                        notice = "Partial refund correction retained. Reported allocation reduced; no refund sent or bank settlement inferred."
                        with closing(connect()) as db, db:
                            row = load_allocation(db, allocation_id)
                            partial_corrections = partial_history(db, allocation_id)
                            history = recheck_history(db, saved["payment_id"])
                    else:
                        partial_review = partial_refund_review(allocation_id)
                    return render_template("hq_square_invoice_evidence.html", allocation_id=allocation_id,
                        row=row, saved=saved, review=None, notice=notice, error=None,
                        history=history, refund_review=None, refund_correction=refund_correction,
                        partial_review=partial_review, partial_corrections=partial_corrections, gross=gross,
                        csrf=session.get("csrf", "")), 200, {"Cache-Control": "no-store"}
                if action in ("refund-review", "refund-save") and allocation_id:
                    if action == "refund-save":
                        manifest = request.form.get("manifest", "")
                        if not DIGEST.fullmatch(manifest) or request.form.get("confirm") != "yes":
                            raise ValueError("Explicit approval of the exact full refund correction is required.")
                        full_refund_review(allocation_id, manifest)
                        notice = "Full refund correction retained. The reported allocation is reversed; no refund was sent or bank settlement inferred."
                        with closing(connect()) as db, db:
                            row = load_allocation(db, allocation_id)
                            refund_correction = correction(db, allocation_id)
                            saved = observation(db, allocation_id)
                            history = recheck_history(db, saved["payment_id"])
                    else:
                        refund_review = full_refund_review(allocation_id)
                    return render_template("hq_square_invoice_evidence.html", allocation_id=allocation_id,
                        row=row, saved=saved, review=None, notice=notice, error=None,
                        history=history, refund_review=refund_review, refund_correction=refund_correction,
                        partial_review=partial_review, partial_corrections=partial_corrections, gross=gross,
                        csrf=session.get("csrf", "")), 200, {"Cache-Control": "no-store"}
                if action == "recheck" and allocation_id:
                    if request.form.get("confirm") != "yes":
                        raise ValueError("Explicit confirmation is required to retain a new observation.")
                    notice = recheck_saved(allocation_id)
                    with closing(connect()) as db, db:
                        row = load_allocation(db, allocation_id)
                        saved = observation(db, allocation_id)
                        history = recheck_history(db, saved["payment_id"])
                    return render_template("hq_square_invoice_evidence.html", allocation_id=allocation_id,
                        row=row, saved=saved, review=None, notice=notice, error=None,
                        history=history, refund_review=refund_review, refund_correction=refund_correction,
                        partial_review=partial_review, partial_corrections=partial_corrections, gross=gross,
                        csrf=session.get("csrf", "")), 200, {"Cache-Control": "no-store"}
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
        if saved and gross is None:
            gross = json.loads(saved["provider_snapshot"])["total_minor"]
        return render_template("hq_square_invoice_evidence.html", allocation_id=allocation_id,
            row=row, saved=saved, review=review, notice=notice, error=error,
            history=history, refund_review=refund_review, refund_correction=refund_correction,
            partial_review=partial_review, partial_corrections=partial_corrections, gross=gross,
            csrf=session.get("csrf", "")), status, {"Cache-Control": "no-store"}
