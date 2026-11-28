"""Owner-approved provider-confirmed allocation register, not bank cash.

Recognition is a classification of the existing allocation, never a new receipt.
Provider status is point-in-time and cannot establish bank settlement.
"""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
import secrets
import sqlite3

import requests
from flask import abort, redirect, render_template, request, session, url_for
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_receivables import HEX
from hq_square_invoice_evidence import (
    DIGEST, digest, load_allocation, observation, provider_check, recheck_history,
    recheck_tables, configured, EvidenceConflict,
)
from hq_work_progress import _actor_fingerprint
from hq_square_payments import inspect_exact_payment, _processing_fees


def tables(db):
    recheck_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_square_receipt_recognition (
        allocation_id TEXT NOT NULL REFERENCES hq_receivable_allocations(id),
        revision INTEGER NOT NULL, payment_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('recognised','withdrawn')),
        context_digest TEXT NOT NULL, manifest TEXT NOT NULL UNIQUE,
        snapshot TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(allocation_id,revision))""")


def latest(db, allocation):
    row = db.execute("""SELECT * FROM hq_square_receipt_recognition
        WHERE allocation_id=? ORDER BY revision DESC LIMIT 1""", (allocation,)).fetchone()
    return dict(row) if row else None


def context(db, allocation):
    row = load_allocation(db, allocation)
    original = observation(db, allocation)
    if (not original or row["status"] != "reported" or row["method"] != "card_reported"
            or row["invoice_status"] != "issued" or row["invoice_archived"]
            or row["revision"] != original["allocation_revision"]
            or row["invoice_revision"] != original["invoice_revision"]
            or row["invoice_id"] != original["invoice_id"]
            or row["evidence_ref"] != original["payment_id"]):
        raise EvidenceConflict("Original invoice or allocation changed; review required.")
    if db.execute("""SELECT 1 FROM hq_receivable_allocations WHERE id<>?
        AND status='reported' AND evidence_ref=? COLLATE NOCASE""",
        (allocation, original["payment_id"])).fetchone():
        raise EvidenceConflict("Another allocation uses this payment reference.")
    history = recheck_history(db, original["payment_id"])
    event = history[0] if history else None
    evidence = event["evidence"] if event else json.loads(original["provider_snapshot"])
    if event and (event["allocation_revision"] != row["revision"]
            or event["invoice_revision"] != row["invoice_revision"]
            or digest(evidence) != event["evidence_digest"]):
        raise EvidenceConflict("Retained provider recheck does not match the current allocation.")
    if (evidence["id"] != original["payment_id"] or evidence["status"] != "COMPLETED"
            or evidence["currency"] != "GBP" or type(evidence["total_minor"]) is not int
            or evidence["total_minor"] != row["amount_pence"]
            or type(evidence["refunded_minor"]) is not int or evidence["refunded_minor"] != 0):
        raise EvidenceConflict("Only unchanged completed GBP payments with explicit zero refunds qualify.")
    return dict(allocation_id=allocation, invoice_id=row["invoice_id"],
                invoice_ref=row["invoice_ref"], allocation_revision=row["revision"],
                invoice_revision=row["invoice_revision"], payment_id=original["payment_id"],
                scope=original["scope_digest"], observation_manifest=original["manifest"],
                recheck_id=event["id"] if event else None, amount_pence=row["amount_pence"],
                evidence=evidence)


def scope_matches(ctx):
    return configured() and digest(os.environ["HQ_SQUARE_LOCATION_ID"]) == ctx["scope"]


def review(allocation, manifest=None, owner=None):
    with closing(connect()) as db, db:
        tables(db)
        ctx = context(db, allocation)
        prior = latest(db, allocation)
        if prior and prior["state"] == "recognised":
            raise EvidenceConflict("Already recognised. Withdraw the previous classification before another approval.")
    if not scope_matches(ctx):
        raise EvidenceConflict("Original Square location cannot be verified.")
    scope, evidence = provider_check(ctx["payment_id"])
    if scope != ctx["scope"] or evidence != ctx["evidence"]:
        raise EvidenceConflict("Provider evidence changed. Retain a new provider recheck before approval.")
    fingerprint = digest(ctx)
    revision = (prior["revision"] if prior else 0) + 1
    exact = digest(["square-receipt-recognition-v1", fingerprint, revision])
    if manifest is None:
        return dict(ctx, manifest=exact)
    if not secrets.compare_digest(manifest, exact):
        raise EvidenceConflict("Approval changed; preview again.")
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        tables(db)
        db.execute("BEGIN IMMEDIATE")
        if not scope_matches(ctx) or context(db, allocation) != ctx or latest(db, allocation) != prior:
            raise EvidenceConflict("Invoice, allocation or recognition changed; preview again.")
        db.execute("""INSERT INTO hq_square_receipt_recognition VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (allocation, revision, ctx["payment_id"], "recognised", fingerprint, exact,
             json.dumps(ctx, sort_keys=True), "", _actor_fingerprint(owner),
             datetime.now(timezone.utc).isoformat()))
    return None


def withdraw(allocation, revision, reason, owner):
    reason = reason.strip()
    if not 1 <= len(reason) <= 250:
        raise ValueError("Enter a withdrawal reason of 1–250 characters.")
    with closing(connect()) as db, db:
        tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = latest(db, allocation)
        if not prior or prior["state"] != "recognised" or str(prior["revision"]) != revision:
            raise EvidenceConflict("Recognition changed; reload before withdrawing.")
        manifest = digest(["square-receipt-withdrawal-v1", prior["manifest"], reason])
        db.execute("INSERT INTO hq_square_receipt_recognition VALUES(?,?,?,?,?,?,?,?,?,?)",
            (allocation, prior["revision"]+1, prior["payment_id"], "withdrawn",
             prior["context_digest"], manifest, prior["snapshot"], reason,
             _actor_fingerprint(owner), datetime.now(timezone.utc).isoformat()))


def listing(db, allocation=None):
    # Bounded display, not a complete ledger total. No provider call on GET.
    rows = db.execute("""SELECT e.* FROM hq_square_receipt_recognition e
        WHERE NOT EXISTS (SELECT 1 FROM hq_square_receipt_recognition n
            WHERE n.allocation_id=e.allocation_id AND n.revision>e.revision)
        AND (? IS NULL OR e.allocation_id=?)
        ORDER BY e.created_at DESC,e.allocation_id LIMIT 100""", (allocation, allocation)).fetchall()
    result = []
    for raw in rows:
        item = dict(raw)
        item["details"] = json.loads(item["snapshot"])
        item["eligible"] = False
        if item["state"] == "recognised":
            try:
                ctx = context(db, item["allocation_id"])
                item["eligible"] = scope_matches(ctx) and digest(ctx) == item["context_digest"]
            except ValueError:
                pass
        result.append(item)
    return result



def fee_tables(db):
    tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_square_fee_reviews (
        allocation_id TEXT NOT NULL REFERENCES hq_receivable_allocations(id),
        revision INTEGER NOT NULL, recognition_manifest TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('recorded','withdrawn')),
        manifest TEXT NOT NULL UNIQUE, snapshot TEXT NOT NULL,
        reason TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(allocation_id,revision))""")


def latest_fee(db, allocation):
    row = db.execute("""SELECT * FROM hq_square_fee_reviews
        WHERE allocation_id=? ORDER BY revision DESC LIMIT 1""", (allocation,)).fetchone()
    return dict(row) if row else None


def fee_context(db, allocation):
    ctx = context(db, allocation)
    receipt = latest(db, allocation)
    if (not receipt or receipt["state"] != "recognised"
            or not scope_matches(ctx) or digest(ctx) != receipt["context_digest"]):
        raise EvidenceConflict("An unchanged provider-confirmed receipt is required before fee review.")
    return ctx, receipt["manifest"]


def review_fee(allocation, manifest=None, owner=None):
    with closing(connect()) as db, db:
        fee_tables(db)
        db.execute("BEGIN")
        ctx, recognition = fee_context(db, allocation)
        prior = latest_fee(db, allocation)
        if prior and prior["state"] == "recorded":
            raise EvidenceConflict("Withdraw the existing fee review before approving another.")
    # Read the fee and payment fields from the same authenticated response.
    evidence, fees = inspect_exact_payment(ctx["payment_id"], include_fees=True)
    if not scope_matches(ctx) or evidence != ctx["evidence"]:
        raise EvidenceConflict("Provider payment changed; recheck and review the receipt first.")
    total = sum(row["amount_pence"] for row in fees)
    snapshot = dict(allocation_id=allocation, invoice_ref=ctx["invoice_ref"],
                    payment_id=ctx["payment_id"], gross_pence=ctx["amount_pence"],
                    fee_pence=total, remainder_pence=ctx["amount_pence"]-total,
                    fees=fees, provider_evidence=evidence)
    revision = (prior["revision"] if prior else 0) + 1
    exact = digest(["square-processing-fee-v1", recognition, revision, snapshot])
    if manifest is None:
        return dict(snapshot, manifest=exact)
    if not secrets.compare_digest(manifest, exact):
        raise EvidenceConflict("Fee evidence changed; preview the exact amount again.")
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        fee_tables(db)
        db.execute("BEGIN IMMEDIATE")
        if fee_context(db, allocation) != (ctx, recognition) or latest_fee(db, allocation) != prior:
            raise EvidenceConflict("Receipt or fee revision changed; preview again.")
        db.execute("INSERT INTO hq_square_fee_reviews VALUES(?,?,?,?,?,?,?,?,?)",
                   (allocation, revision, recognition, "recorded", exact,
                    json.dumps(snapshot, sort_keys=True), "", _actor_fingerprint(owner),
                    datetime.now(timezone.utc).isoformat()))


def withdraw_fee(allocation, revision, reason, owner):
    reason = reason.strip()
    if not 1 <= len(reason) <= 250:
        raise ValueError("Enter a withdrawal reason of 1–250 characters.")
    with closing(connect()) as db, db:
        fee_tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = latest_fee(db, allocation)
        if not prior or prior["state"] != "recorded" or str(prior["revision"]) != revision:
            raise EvidenceConflict("Fee review changed; reload before withdrawing.")
        exact = digest(["square-fee-withdrawal-v1", prior["manifest"], reason])
        db.execute("INSERT INTO hq_square_fee_reviews VALUES(?,?,?,?,?,?,?,?,?)",
                   (allocation, prior["revision"]+1, prior["recognition_manifest"],
                    "withdrawn", exact, prior["snapshot"], reason, _actor_fingerprint(owner),
                    datetime.now(timezone.utc).isoformat()))


def fee_listing(db, allocation=None):
    rows = db.execute("""SELECT f.* FROM hq_square_fee_reviews f
        WHERE NOT EXISTS (SELECT 1 FROM hq_square_fee_reviews n
            WHERE n.allocation_id=f.allocation_id AND n.revision>f.revision)
        AND (? IS NULL OR f.allocation_id=?)
        ORDER BY f.created_at DESC,f.allocation_id LIMIT 100""", (allocation, allocation)).fetchall()
    result = []
    for raw in rows:
        item = dict(raw)
        item["details"] = json.loads(item["snapshot"])
        item["eligible"] = False
        if item["state"] == "recorded":
            try:
                _, recognition = fee_context(db, item["allocation_id"])
                item["eligible"] = recognition == item["recognition_manifest"]
            except ValueError:
                pass
        result.append(item)
    return result



def monthly_receipt_report(db, month):
    """Payment-month receipts and separate signed fee-effective-month evidence.

    The entire bounded register is examined: an adjustment may be effective
    in a month after its original payment. Neither view posts bank cash.
    """
    parsed = datetime.strptime(month, "%Y-%m")
    if parsed.strftime("%Y-%m") != month:
        raise ValueError("Choose a valid payment month.")
    records = db.execute("""SELECT e.* FROM hq_square_receipt_recognition e
        WHERE NOT EXISTS (SELECT 1 FROM hq_square_receipt_recognition n
            WHERE n.allocation_id=e.allocation_id AND n.revision>e.revision)
        ORDER BY e.allocation_id LIMIT 5001""").fetchall()
    if len(records) > 5000:
        raise ValueError("Evidence register exceeds the safe report limit; no partial totals shown.")
    items, seen = [], set()
    effective_rows = []
    unknown_fee_dates = 0
    for raw in records:
        row = dict(raw)
        snap = json.loads(row["snapshot"])
        created = datetime.fromisoformat(snap["evidence"]["created_at"])
        if created.tzinfo is None:
            raise ValueError("Stored payment date has no timezone; report withheld.")
        payment_in_month = created.astimezone(timezone.utc).strftime("%Y-%m") == month
        item = dict(invoice_ref=snap["invoice_ref"], payment_id=row["payment_id"],
                    allocation_id=row["allocation_id"], day=created.astimezone(timezone.utc).date().isoformat(),
                    gross=None, fee=None, state="withdrawn" if row["state"] == "withdrawn" else "review required")
        if payment_in_month:
            items.append(item)
        if row["state"] != "recognised":
            unknown_fee_dates += 1
            continue
        try:
            ctx, recognition = fee_context(db, row["allocation_id"])
            if (recognition != row["manifest"] or ctx != snap
                    or digest(["square-receipt-recognition-v1", digest(ctx), row["revision"]]) != recognition
                    or row["payment_id"] != ctx["payment_id"]
                    or type(ctx["amount_pence"]) is not int or ctx["amount_pence"] <= 0):
                unknown_fee_dates += 1
                continue
        except ValueError:
            unknown_fee_dates += 1
            continue
        key = ctx["payment_id"].casefold()
        if key in seen:
            raise ValueError("Duplicate payment evidence; report withheld.")
        seen.add(key)
        item.update(gross=ctx["amount_pence"], state="recognised")
        fee = latest_fee(db, row["allocation_id"])
        if fee and fee["state"] == "recorded" and fee["recognition_manifest"] == recognition:
            data = json.loads(fee["snapshot"])
            amount = data.get("fee_pence")
            if (data.get("allocation_id") == row["allocation_id"]
                    and data.get("payment_id") == ctx["payment_id"]
                    and data.get("gross_pence") == item["gross"]
                    and data.get("provider_evidence") == ctx["evidence"]
                    and type(amount) is int and 0 <= amount <= item["gross"]
                    and digest(["square-processing-fee-v1", recognition, fee["revision"], data]) == fee["manifest"]):
                # Revalidate every retained fee line before using its effective date.
                lines = data.get("fees")
                if not isinstance(lines, list):
                    raise ValueError("Missing fee lines; report withheld.")
                validated = _processing_fees([
                    dict(type=line["type"], effective_at=line["effective_at"],
                         amount_money=dict(currency="GBP", amount=line["amount_pence"]))
                    for line in lines
                ], "GBP", ctx["evidence"]["created_at"], item["gross"])
                if validated != lines or sum(line["amount_pence"] for line in validated) != amount:
                    raise ValueError("Inconsistent fee detail; report withheld.")
                item["fee"] = amount
                for line in validated:
                    stamp = datetime.fromisoformat(line["effective_at"]).astimezone(timezone.utc)
                    if stamp.strftime("%Y-%m") == month:
                        effective_rows.append(dict(
                            invoice_ref=item["invoice_ref"], payment_id=item["payment_id"],
                            allocation_id=item["allocation_id"], payment_day=item["day"],
                            day=stamp.date().isoformat(), kind=line["type"],
                            amount=line["amount_pence"]))
        if item["fee"] is None:
            unknown_fee_dates += 1
    included = [i for i in items if i["state"] == "recognised"]
    covered = [i for i in included if i["fee"] is not None]
    gross = sum(i["gross"] for i in included) if included else None
    known_fees = sum(i["fee"] for i in covered) if covered else None
    complete = bool(included) and len(covered) == len(included)
    return dict(month=month, rows=sorted(items, key=lambda i: (i["day"], i["allocation_id"])),
                gross=gross, known_fees=known_fees,
                net=gross-known_fees if complete else None, included=len(included),
                missing=len(included)-len(covered), excluded=len(items)-len(included),
                effective_rows=sorted(effective_rows, key=lambda r: (r["day"], r["allocation_id"], r["kind"])),
                effective_total=sum(r["amount"] for r in effective_rows) if effective_rows else None,
                unknown_fee_dates=unknown_fee_dates)

def register(bp):

    @bp.route("/hq/finance/provider-receipt-month", methods=["GET"])
    @required
    def provider_receipt_month():
        if not storage_ready():
            abort(503)
        month = request.args.get("month", datetime.now(timezone.utc).strftime("%Y-%m"))
        report, error, status = None, None, 200
        try:
            with closing(connect()) as db, db:
                fee_tables(db)
                db.execute("BEGIN")
                report = monthly_receipt_report(db, month)
        except (ValueError, KeyError, TypeError, OverflowError):
            error, status = "Invalid month or inconsistent/oversized evidence. No partial totals shown.", 409
        except (sqlite3.Error, OSError, RuntimeError):
            error, status = "Persistent storage unavailable; no totals shown.", 503
        return render_template("finance_provider_receipt_month.html", month=month,
            report=report, error=error), status, {"Cache-Control": "no-store"}

    @bp.route("/hq/finance/provider-confirmed-receipts", methods=["GET","POST"])
    @required
    def provider_confirmed_receipts():
        if not storage_ready():
            abort(503)
        error, proposal, fee_proposal, status = None, None, None, 200
        # Exact opaque allocation ID only; no customer names or payment references in URLs.
        selected = request.args.get("allocation") or None
        if selected and not HEX.fullmatch(selected):
            abort(400)
        if request.method == "POST":
            request.max_content_length = 4096
            token = session.get("csrf")
            if not token or not secrets.compare_digest(request.form.get("csrf",""), token):
                abort(403)
            try:
                allocation = request.form.get("allocation","")
                if not HEX.fullmatch(allocation):
                    raise ValueError("Choose a valid allocation ID.")
                selected = allocation
                action = request.form.get("action")
                if action == "preview":
                    proposal = review(allocation)
                elif action == "save":
                    manifest = request.form.get("manifest","")
                    if request.form.get("confirm") != "yes" or not DIGEST.fullmatch(manifest):
                        raise ValueError("Explicit approval of the exact receipt classification is required.")
                    review(allocation, manifest, session.get("google_sub"))
                elif action == "fee-preview":
                    fee_proposal = review_fee(allocation)
                elif action == "fee-save":
                    manifest = request.form.get("manifest","")
                    if request.form.get("confirm") != "yes" or not DIGEST.fullmatch(manifest):
                        raise ValueError("Explicit approval of exact fee evidence is required.")
                    review_fee(allocation, manifest, session.get("google_sub"))
                elif action == "fee-withdraw":
                    if request.form.get("confirm") != "yes":
                        raise ValueError("Explicit withdrawal confirmation required.")
                    withdraw_fee(allocation, request.form.get("revision",""),
                                 request.form.get("reason",""), session.get("google_sub"))
                elif action == "withdraw":
                    if request.form.get("confirm") != "yes":
                        raise ValueError("Explicit withdrawal confirmation required.")
                    withdraw(allocation, request.form.get("revision",""),
                             request.form.get("reason",""), session.get("google_sub"))
                else:
                    raise ValueError("Choose a valid action.")
                if action not in ("preview", "fee-preview"):
                    return redirect(url_for(".provider_confirmed_receipts", allocation=allocation), code=303)
            except EvidenceConflict as exc:
                error, status = str(exc), 409
            except ValueError as exc:
                error, status = str(exc), 400
            except (RuntimeError, requests.RequestException, sqlite3.Error, OSError):
                error, status = "Provider or persistent storage unavailable. Save not confirmed.", 503
        try:
            with closing(connect()) as db, db:
                fee_tables(db)
                db.execute("BEGIN")
                rows = listing(db, selected)
                fee_rows = fee_listing(db, selected)
                fee_history = [dict(r) for r in db.execute("""SELECT allocation_id,revision,state,reason,created_at,snapshot
                    FROM hq_square_fee_reviews WHERE (? IS NULL OR allocation_id=?)
                    ORDER BY created_at DESC,allocation_id LIMIT 200""", (selected, selected))]
                for event in fee_history:
                    event["details"] = json.loads(event.pop("snapshot"))
                candidates = [dict(r) for r in db.execute("""SELECT o.allocation_id,r.invoice_ref
                    FROM hq_square_invoice_observations o JOIN hq_receivables r ON r.id=o.invoice_id
                    WHERE (? IS NULL OR o.allocation_id=?)
                    ORDER BY o.observed_at DESC,o.allocation_id LIMIT 100""", (selected, selected))]
                history = [dict(r) for r in db.execute("""SELECT allocation_id,revision,state,reason,created_at
                    FROM hq_square_receipt_recognition WHERE (? IS NULL OR allocation_id=?)
                    ORDER BY created_at DESC,allocation_id LIMIT 200""", (selected, selected))]
        except (sqlite3.Error, OSError, RuntimeError):
            abort(503)
        return render_template("finance_provider_receipts.html", rows=rows, candidates=candidates,
            history=history, proposal=proposal, fee_proposal=fee_proposal, fee_rows=fee_rows,
            fee_history=fee_history, selected=selected, error=error, csrf=session.get("csrf","")), status, {"Cache-Control":"no-store"}
