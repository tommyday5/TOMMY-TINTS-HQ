"""Owner-reported invoice payment allocations. These are NOT verified bank receipts."""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone

from flask import jsonify, render_template, request, session
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_receivables import HEX, _day, tables as receivable_tables

METHODS = ("bank_reported", "card_reported", "cash_reported", "other_reported")
STATUSES = ("reported", "reversed")


class StaleAllocation(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def tables(db):
    receivable_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_receivable_allocations (
        id TEXT PRIMARY KEY,
        invoice_id TEXT NOT NULL REFERENCES hq_receivables(id),
        amount_pence INTEGER NOT NULL CHECK(amount_pence > 0),
        received_on TEXT NOT NULL,
        method TEXT NOT NULL CHECK(method IN
            ('bank_reported','card_reported','cash_reported','other_reported')),
        evidence_ref TEXT NOT NULL,
        memo TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('reported','reversed')),
        revision INTEGER NOT NULL CHECK(revision > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_allocations_invoice
        ON hq_receivable_allocations(invoice_id,status)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_receivable_allocation_events (
        event_id TEXT PRIMARY KEY,
        allocation_id TEXT NOT NULL REFERENCES hq_receivable_allocations(id),
        revision INTEGER NOT NULL,
        snapshot TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(allocation_id,revision)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_receivable_allocation_operations (
        operation_id TEXT PRIMARY KEY,
        allocation_id TEXT NOT NULL REFERENCES hq_receivable_allocations(id),
        fingerprint TEXT NOT NULL,
        result_revision INTEGER NOT NULL
    )""")


def validated(data):
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    for key in ("id", "invoice_id", "operation_id"):
        if not isinstance(data.get(key), str) or not HEX.fullmatch(data[key]):
            raise ValueError("Invalid allocation, invoice or operation ID")
    revision = data.get("revision")
    if type(revision) is not int or not 0 <= revision < 2**63 - 1:
        raise ValueError("Valid revision required")
    amount = data.get("amount_pence")
    if type(amount) is not int or not 1 <= amount <= 1000000000:
        raise ValueError("Enter an amount in integer pence")
    received_on = _day(data.get("received_on"))
    method = data.get("method")
    if method not in METHODS:
        raise ValueError("Select a reported payment method")
    evidence_ref = data.get("evidence_ref", "")
    memo = data.get("memo", "")
    if not isinstance(evidence_ref, str) or len(evidence_ref.strip()) > 120:
        raise ValueError("Evidence reference must be at most 120 characters")
    if not isinstance(memo, str) or len(memo.strip()) > 500:
        raise ValueError("Private note must be at most 500 characters")
    status = data.get("status")
    if status not in STATUSES:
        raise ValueError("Invalid allocation state")
    return dict(id=data["id"], invoice_id=data["invoice_id"],
                operation_id=data["operation_id"], revision=revision,
                amount_pence=amount, received_on=received_on,
                method=method, evidence_ref=evidence_ref.strip(),
                memo=memo.strip(), status=status)


def save(data):
    """Atomic, idempotent owner record. No bank/provider verification is implied."""
    if not storage_ready():
        raise ValueError("Persistent HQ storage required")
    fields = validated(data)
    operation_id = fields.pop("operation_id")
    fingerprint = hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    stamp = now()
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        tables(db)
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("""SELECT allocation_id,fingerprint,result_revision
            FROM hq_receivable_allocation_operations WHERE operation_id=?""", (operation_id,)).fetchone()
        if prior:
            if prior["allocation_id"] != fields["id"] or not hmac.compare_digest(prior["fingerprint"], fingerprint):
                raise ValueError("Submission token reused for different values")
            return prior["result_revision"]
        existing = db.execute("SELECT * FROM hq_receivable_allocations WHERE id=?", (fields["id"],)).fetchone()
        if (existing and existing["revision"] != fields["revision"]) or (not existing and fields["revision"] != 0):
            raise StaleAllocation("Allocation changed elsewhere; reload before editing")
        invoice = db.execute("SELECT amount_pence,status FROM hq_receivables WHERE id=?", (fields["invoice_id"],)).fetchone()
        if not invoice:
            raise ValueError("Choose an existing HQ invoice")
        if not existing and invoice["status"] not in ("issued", "disputed"):
            raise ValueError("Issue the invoice before recording a payment")
        if existing:
            if existing["invoice_id"] != fields["invoice_id"] or existing["amount_pence"] != fields["amount_pence"]:
                raise ValueError("Allocation amount and invoice cannot be altered; reverse and add a correction")
            if existing["status"] == "reversed":
                raise ValueError("Reversed allocations cannot be edited")
        elif fields["status"] != "reported":
            raise ValueError("New allocations must start as owner-reported")
        # Preserve the immutable source link and prevent manual reuse of its evidence.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_monzo_reported_links'").fetchone():
            link = db.execute("SELECT * FROM hq_monzo_reported_links WHERE allocation_id=?",
                              (fields["id"],)).fetchone()
            if link:
                if fields["status"] != "reversed" or any(fields[k] != existing[k]
                    for k in ("received_on","method","evidence_ref","memo")):
                    raise ValueError("Source-linked allocations can only be reversed without changing their evidence")
            elif fields["status"] == "reported" and fields["evidence_ref"] and db.execute("""
                SELECT 1 FROM hq_monzo_reported_links l JOIN hq_receivable_allocations a
                ON a.id=l.allocation_id WHERE a.status='reported'
                AND (l.evidence_ref=? COLLATE NOCASE OR l.source_id=? COLLATE NOCASE)""",
                (fields["evidence_ref"],fields["evidence_ref"])).fetchone():
                raise ValueError("Evidence already linked to a Monzo source; use its reconciliation workflow")
        # Exact Square observations are historical evidence, never another receipt.
        # Reserve provider IDs even after a reversal until a separately reviewed
        # correction workflow exists; never silently reassign a confirmed ID.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_square_invoice_observations'").fetchone():
            observed = db.execute("SELECT * FROM hq_square_invoice_observations WHERE allocation_id=?",
                                  (fields["id"],)).fetchone()
            if observed:
                if fields["status"] != "reversed" or any(fields[k] != existing[k]
                        for k in ("received_on","method","evidence_ref","memo")):
                    raise ValueError("Provider-observed allocations can only be reversed without changing evidence")
            elif fields["status"] == "reported" and fields["evidence_ref"] and db.execute("""
                SELECT 1 FROM hq_square_invoice_observations WHERE payment_id=? COLLATE NOCASE""",
                (fields["evidence_ref"],)).fetchone():
                raise ValueError("Square payment reference already has retained provider evidence")
        counted = db.execute("""SELECT COALESCE(SUM(amount_pence),0) FROM hq_receivable_allocations
            WHERE invoice_id=? AND status='reported' AND id<>?""",
            (fields["invoice_id"], fields["id"])).fetchone()[0]
        active = fields["amount_pence"] if fields["status"] == "reported" else 0
        if counted + active > invoice["amount_pence"]:
            raise ValueError("Reported allocations exceed this invoice's face amount")
        revision = fields["revision"] + 1
        if existing:
            db.execute("""UPDATE hq_receivable_allocations SET received_on=?,method=?,
                evidence_ref=?,memo=?,status=?,revision=?,updated_at=? WHERE id=?""",
                (fields["received_on"], fields["method"], fields["evidence_ref"],
                 fields["memo"], fields["status"], revision, stamp, fields["id"]))
        else:
            db.execute("""INSERT INTO hq_receivable_allocations
                (id,invoice_id,amount_pence,received_on,method,evidence_ref,memo,
                 status,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (fields["id"], fields["invoice_id"], fields["amount_pence"],
                 fields["received_on"], fields["method"], fields["evidence_ref"],
                 fields["memo"], fields["status"], revision, stamp, stamp))
        db.execute("INSERT INTO hq_receivable_allocation_events VALUES(?,?,?,?,?)",
                   (uuid.uuid4().hex, fields["id"], revision,
                    json.dumps({**fields, "revision": revision}, sort_keys=True), stamp))
        db.execute("INSERT INTO hq_receivable_allocation_operations VALUES(?,?,?,?)",
                   (operation_id, fields["id"], fingerprint, revision))
    return revision


def snapshot():
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        tables(db)
        invoices = [dict(row) for row in db.execute("""SELECT r.id,r.invoice_ref,r.amount_pence,r.status,
            r.archived,c.name AS customer_name,
            COALESCE(SUM(CASE WHEN a.status='reported' THEN a.amount_pence ELSE 0 END),0)
                AS owner_reported_allocated_pence
            FROM hq_receivables r JOIN hq_customers c ON c.id=r.customer_id
            LEFT JOIN hq_receivable_allocations a ON a.invoice_id=r.id
            GROUP BY r.id ORDER BY r.updated_at DESC LIMIT 500""")]
        entries = [dict(row) for row in db.execute("""SELECT a.*,r.invoice_ref
            FROM hq_receivable_allocations a JOIN hq_receivables r ON a.invoice_id=r.id
            ORDER BY a.updated_at DESC,a.id DESC LIMIT 500""")]
        history = [dict(row) for row in db.execute("""SELECT allocation_id,revision,
            snapshot,occurred_at FROM hq_receivable_allocation_events
            ORDER BY occurred_at DESC,event_id DESC LIMIT 200""")]
    for invoice in invoices:
        invoice["unallocated_owner_reported_pence"] = (
            invoice["amount_pence"] - invoice["owner_reported_allocated_pence"])
    return dict(invoices=invoices, allocations=entries, history=history,
                bank_verified=False, receipts_verified=False,
                outstanding_verified=False, collected_income=None,
                source="Owner-reported payment claims, not matched bank evidence")


def register(bp):
    from hq_square_invoice_evidence import register as register_square_evidence
    register_square_evidence(bp)

    @bp.get("/hq/finance/receipt-allocations")
    @required
    def receipt_allocations_page():
        return render_template("hq_receipt_allocations.html", csrf=session.get("csrf", "")), 200, {"Cache-Control": "no-store"}

    @bp.route("/api/hq/finance/receipt-allocations", methods=["GET", "POST"])
    @required
    def receipt_allocations_api():
        if request.method == "GET":
            return jsonify(snapshot()), 200, {"Cache-Control": "no-store"}
        if not secrets.compare_digest(request.headers.get("X-HQ-CSRF", ""), session.get("csrf", "missing")):
            return jsonify(error="Invalid security token"), 403
        if not request.is_json or not request.content_length or request.content_length > 4096:
            return jsonify(error="Invalid JSON request"), 400
        try:
            revision = save(request.get_json(silent=True))
            return jsonify(saved=True, revision=revision, bank_verified=False), 200, {"Cache-Control": "no-store"}
        except StaleAllocation as exc:
            return jsonify(error=str(exc)), 409
        except sqlite3.IntegrityError:
            return jsonify(error="Allocation conflict; reload before saving"), 409
        except (ValueError, TypeError, OverflowError) as exc:
            return jsonify(error=str(exc)), 400
        except sqlite3.Error:
            return jsonify(error="Storage unavailable; nothing confirmed saved"), 503
