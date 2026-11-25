"""Explicit customer-facing invoice summaries, not payment verification.

Publication is revision-bound and private to the canonical customer. A change
to invoice, allocation or retained provider evidence hides the summary until
the owner reviews it again. Never creates a receipt or sends a message.
"""
import hashlib
import json
import secrets
from contextlib import closing
from datetime import datetime, timezone

from customer_request_store import connect, storage_ready
from hq_receivables import HEX
from hq_square_invoice_evidence import recheck_tables


def tables(db):
    recheck_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customer_invoice_publications (
        operation_id TEXT PRIMARY KEY,
        invoice_id TEXT NOT NULL REFERENCES hq_receivables(id),
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        revision INTEGER NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('publish','withdraw')),
        digest TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(invoice_id,revision)
    )""")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _latest(db, invoice_id):
    return db.execute("""SELECT * FROM hq_customer_invoice_publications
        WHERE invoice_id=? ORDER BY revision DESC LIMIT 1""", (invoice_id,)).fetchone()


def _context(db, customer_id, invoice_id):
    invoice = db.execute("""SELECT r.id,r.customer_id,r.work_id,r.invoice_ref,r.issue_date,
        r.due_date,r.amount_pence,r.revision,w.status AS work_status
        FROM hq_receivables r JOIN hq_work_records w
        ON w.id=r.work_id AND w.customer_id=r.customer_id
        WHERE r.id=? AND r.customer_id=? AND r.status='issued' AND r.archived=0
        AND w.status IN ('approved','in_progress','completed')
        AND NOT EXISTS (SELECT 1 FROM hq_work_archive_state a
            WHERE a.work_id=w.id AND a.is_archived=1)""", (invoice_id, customer_id)).fetchone()
    if not invoice:
        return None
    allocations = [dict(a) for a in db.execute("""SELECT id,amount_pence,status,revision
        FROM hq_receivable_allocations WHERE invoice_id=? ORDER BY id""", (invoice_id,))]
    # Bind to all retained provider evidence, including unapplied refund observations.
    evidence = [dict(a) for a in db.execute("""SELECT o.payment_id,o.manifest,
        r.id AS recheck_id,r.evidence_digest FROM hq_square_invoice_observations o
        LEFT JOIN hq_square_invoice_rechecks r ON r.payment_id=o.payment_id
        WHERE o.invoice_id=? ORDER BY o.payment_id,r.id""", (invoice_id,))]
    total = sum(a["amount_pence"] for a in allocations if a["status"] == "reported")
    if not 0 <= total <= invoice["amount_pence"]:
        return None
    public = {k: invoice[k] for k in ("invoice_ref","issue_date","due_date","amount_pence")}
    # Keep payment evidence private; publication only shows invoice face value.
    # Reported allocations are not independently verified receipts.
    return public, _digest([dict(invoice), allocations, evidence])


def _rows(db, customer_id, owner):
    ids = db.execute("""SELECT id FROM hq_receivables WHERE customer_id=?
        ORDER BY updated_at DESC,id DESC LIMIT 500""", (customer_id,)).fetchall()
    result = []
    for entry in ids:
        invoice_id = entry["id"]
        prior = _latest(db, invoice_id)
        context = _context(db, customer_id, invoice_id)
        published = bool(context and prior and prior["customer_id"] == customer_id
                         and prior["action"] == "publish"
                         and secrets.compare_digest(prior["digest"], context[1]))
        if owner:
            if context or (prior and prior["action"] == "publish"):
                result.append(dict(invoice_id=invoice_id, summary=context[0] if context else None,
                                   digest=context[1] if context else "", published=published,
                                   revision=prior["revision"] if prior else 0,
                                   can_withdraw=bool(prior and prior["action"] == "publish"),
                                   operation_id=secrets.token_hex(16)))
        elif published:
            result.append(context[0])
    return result


def summaries(customer_id, *, owner=False):
    if not HEX.fullmatch(customer_id) or not storage_ready():
        raise ValueError("Persistent customer storage required")
    with closing(connect()) as db, db:
        tables(db)
        db.execute("BEGIN")
        return _rows(db, customer_id, owner)


def change(customer_id, invoice_id, action, revision, digest, operation_id):
    if not storage_ready() or any(not isinstance(v,str) or not HEX.fullmatch(v)
                                    for v in (customer_id,invoice_id,operation_id)):
        raise ValueError("Invalid publication identifiers")
    if action not in ("publish","withdraw") or type(revision) is not int or revision < 0:
        raise ValueError("Invalid publication action")
    if action == "publish" and (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
        raise ValueError("Review the exact invoice first")
    if action == "withdraw":
        digest = ""
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        tables(db)
        db.execute("BEGIN IMMEDIATE")
        invoice = db.execute("SELECT customer_id FROM hq_receivables WHERE id=?", (invoice_id,)).fetchone()
        if not invoice or invoice["customer_id"] != customer_id:
            raise ValueError("Invoice not available for this customer")
        replay = db.execute("SELECT * FROM hq_customer_invoice_publications WHERE operation_id=?",
                            (operation_id,)).fetchone()
        if replay:
            if (replay["customer_id"],replay["invoice_id"],replay["action"],replay["revision"],replay["digest"]) == (
                    customer_id,invoice_id,action,revision+1,digest):
                return
            raise ValueError("Submission already used")
        prior = _latest(db, invoice_id)
        if revision != (prior["revision"] if prior else 0):
            raise ValueError("Publication changed; reload before saving")
        if action == "publish":
            context = _context(db, customer_id, invoice_id)
            if not context or not secrets.compare_digest(context[1], digest):
                raise ValueError("Invoice or payment evidence changed; review again")
        elif not prior or prior["action"] != "publish":
            raise ValueError("No publication to withdraw")
        db.execute("INSERT INTO hq_customer_invoice_publications VALUES(?,?,?,?,?,?,?)",
                   (operation_id,invoice_id,customer_id,revision+1,action,digest,
                    datetime.now(timezone.utc).isoformat()))
