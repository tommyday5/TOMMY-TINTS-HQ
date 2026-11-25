"""Owner-reviewed Monzo worksheet to invoice candidates, never receipts."""
import re
import json
import uuid
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal

import requests
from flask import abort, current_app, render_template, request, session
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_receivable_payments import tables as invoice_tables
from hq_receivables import REF
from hq_monzo_sheet_classifications import ID, DIGEST, fingerprint, Conflict
from hq_work_progress import _actor_fingerprint


def tables(db):
    invoice_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_monzo_invoice_match_events(
        source_id TEXT NOT NULL, revision INTEGER NOT NULL, invoice_id TEXT NOT NULL,
        invoice_ref TEXT NOT NULL, amount_pence INTEGER NOT NULL,
        manifest TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('proposed','withdrawn')),
        operation TEXT NOT NULL UNIQUE, actor TEXT NOT NULL, created TEXT NOT NULL,
        PRIMARY KEY(source_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_monzo_invoice_match_source_evidence(
        source_id TEXT NOT NULL, revision INTEGER NOT NULL, source_digest TEXT NOT NULL,
        PRIMARY KEY(source_id,revision),
        FOREIGN KEY(source_id,revision) REFERENCES hq_monzo_invoice_match_events(source_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_monzo_reported_links(
        source_id TEXT NOT NULL, candidate_revision INTEGER NOT NULL,
        source_digest TEXT NOT NULL, allocation_id TEXT NOT NULL UNIQUE
          REFERENCES hq_receivable_allocations(id), evidence_ref TEXT NOT NULL,
        PRIMARY KEY(source_id,candidate_revision))""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_monzo_match_pair_revision
        ON hq_monzo_invoice_match_events(source_id,invoice_id,revision)""")


def active_candidates(db, ref):
    """Latest state per source/invoice pair; older single-candidate history is compatible."""
    return [dict(r) for r in db.execute("""SELECT e.*,s.source_digest
        FROM hq_monzo_invoice_match_events e
        LEFT JOIN hq_monzo_invoice_match_source_evidence s
          ON s.source_id=e.source_id AND s.revision=e.revision
        WHERE e.source_id=? AND e.status='proposed'
        AND NOT EXISTS (SELECT 1 FROM hq_monzo_invoice_match_events n
            WHERE n.source_id=e.source_id AND n.invoice_id=e.invoice_id AND n.revision>e.revision)
        ORDER BY e.revision DESC""", (ref,))]


def reported_links(db, ref):
    return [dict(r) for r in db.execute("""SELECT l.*,a.amount_pence,a.status,a.invoice_id
        FROM hq_monzo_reported_links l JOIN hq_receivable_allocations a
        ON a.id=l.allocation_id WHERE l.source_id=? AND a.status='reported'""",(ref,))]



def require_source_not_settled(db, ref):
    """Prevent a payout deposit being reused as an independent invoice receipt.

    Read existing settlement tables only, so older databases without the
    additive settlement schema remain compatible. Caller holds the same
    transaction used for candidate review/save or reported allocation.
    """
    for table, identity in (("hq_square_settlement_reviews", "allocation_id"),
                            ("hq_square_batch_reviews", "source_id")):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                          (table,)).fetchone():
            continue
        # Identifiers are fixed internal constants, never request input.
        if db.execute(f"""SELECT 1 FROM {table} e
            WHERE e.source_id=? AND e.state='proposed'
            AND NOT EXISTS (SELECT 1 FROM {table} n
                WHERE n.{identity}=e.{identity} AND n.id>e.id) LIMIT 1""",
                (ref,)).fetchone():
            raise Conflict("This bank deposit has an active Square settlement proposal. "
                           "Withdraw it in Settlement and fee review before allocating this deposit directly.")


def source_digest(entry):
    return fingerprint([entry["ref"],entry["evidence"],entry["legacy_evidence"],entry["pence"]])


def history(db, ref):
    return [dict(r) for r in db.execute("""SELECT * FROM hq_monzo_invoice_match_events
        WHERE source_id=? ORDER BY revision DESC LIMIT 50""", (ref,))]


def parse_candidate_amount(raw):
    """Blank retains the existing full-source candidate behaviour."""
    if raw == "":
        return None
    if not isinstance(raw,str) or not re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,2})?",raw):
        raise ValueError("Enter positive pounds with at most two decimal places.")
    amount = int(Decimal(raw)*100)
    if not 1 <= amount <= 1000000000:
        raise ValueError("Candidate amount must be positive and within the supported limit.")
    return amount


def review(db, entry, invoice_ref, amount=None):
    require_source_not_settled(db, entry["ref"])
    invoice = db.execute("""SELECT r.*,c.name AS customer_name,w.service AS work_service
        FROM hq_receivables r JOIN hq_customers c ON r.customer_id=c.id
        LEFT JOIN hq_work_records w ON r.work_id=w.id
        WHERE r.invoice_ref=? COLLATE NOCASE""", (invoice_ref,)).fetchone()
    if not invoice or invoice["archived"] or invoice["status"] != "issued":
        raise ValueError("Choose an existing, issued, non-archived invoice.")
    if entry["pence"] <= 0:
        raise ValueError("Only positive worksheet transactions can be proposed against invoices.")
    events = history(db, entry["ref"])
    active = active_candidates(db, entry["ref"])
    if any(e["invoice_id"] == invoice["id"] for e in active):
        raise Conflict("Withdraw the existing candidate for this invoice before proposing another.")
    digest = source_digest(entry)
    if any(e["source_digest"] != digest for e in active):
        raise Conflict("An existing candidate has older or changed source evidence. Withdraw and re-review it first.")
    linked = reported_links(db, entry["ref"])
    if any(e["source_digest"] != digest for e in linked):
        raise Conflict("Previously reported source evidence changed; review those allocations first.")
    used = sum(e["amount_pence"] for e in active + linked)
    if len(active) >= 20:
        raise ValueError("A source can have at most 20 active invoice candidates.")
    amount = entry["pence"] - used if amount is None else amount
    if type(amount) is not int or not 1 <= amount <= entry["pence"] - used:
        raise ValueError("Candidate cannot exceed the unmatched positive source transaction.")
    reported = db.execute("""SELECT COALESCE(SUM(amount_pence),0)
        FROM hq_receivable_allocations WHERE invoice_id=? AND status='reported'""",
        (invoice["id"],)).fetchone()[0]
    proposed = db.execute("""SELECT COALESCE(SUM(e.amount_pence),0)
        FROM hq_monzo_invoice_match_events e WHERE e.invoice_id=? AND e.status='proposed'
        AND NOT EXISTS (SELECT 1 FROM hq_monzo_invoice_match_events n
          WHERE n.source_id=e.source_id AND n.invoice_id=e.invoice_id AND n.revision>e.revision)""",
        (invoice["id"],)).fetchone()[0]
    remaining = invoice["amount_pence"] - reported - proposed
    if amount > remaining:
        raise ValueError("Candidate exceeds remaining invoice capacity. Enter a smaller explicit amount.")
    rev = events[0]["revision"] if events else 0
    manifest = fingerprint([entry["ref"],entry["evidence"],entry["legacy_evidence"],
        invoice["id"],invoice["revision"],invoice["invoice_ref"],invoice["customer_id"],
        invoice["work_id"],invoice["customer_name"],invoice["work_service"],
        invoice["amount_pence"],reported,proposed,entry["pence"],rev,amount,used,digest])
    return dict(invoice=dict(invoice), entry=entry, revision=rev, manifest=manifest,
                reported=reported, proposed=proposed, remaining=remaining, amount=amount,
                unallocated_source=entry["pence"]-used-amount, source_used=used, source_digest=digest)


def report_preview(db, entry, invoice_ref, rev, evidence_ref):
    """Owner attestation only; worksheet data cannot independently verify a receipt."""
    require_source_not_settled(db, entry["ref"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._:/-]{2,119}", evidence_ref):
        raise ValueError("Enter the separate bank statement or receipt reference (3–120 characters).")
    if evidence_ref.casefold() == entry["ref"].casefold():
        raise ValueError("Use separate evidence, not the worksheet transaction ID.")
    candidate = next((e for e in active_candidates(db,entry["ref"])
                      if e["revision"] == rev and e["invoice_ref"] == invoice_ref),None)
    digest = source_digest(entry)
    if not candidate or candidate["source_digest"] != digest:
        raise Conflict("Candidate or source changed; withdraw and review again.")
    invoice = db.execute("""SELECT r.*,c.name AS customer_name FROM hq_receivables r
        JOIN hq_customers c ON c.id=r.customer_id WHERE r.id=?""",(candidate["invoice_id"],)).fetchone()
    if not invoice or invoice["status"] != "issued" or invoice["archived"] or invoice["invoice_ref"] != invoice_ref:
        raise Conflict("Invoice is no longer eligible; review again.")
    active = active_candidates(db,entry["ref"])
    linked = reported_links(db,entry["ref"])
    if any(e["source_digest"] != digest for e in active + linked) or sum(e["amount_pence"] for e in active + linked) > entry["pence"]:
        raise Conflict("Source capacity or evidence changed.")
    if any(e["evidence_ref"].casefold() != evidence_ref.casefold() for e in linked):
        raise Conflict("Use the same independent evidence reference for this source.")
    if db.execute("""SELECT 1 FROM hq_receivable_allocations a
        LEFT JOIN hq_monzo_reported_links l ON l.allocation_id=a.id
        WHERE a.status='reported' AND (a.evidence_ref=? COLLATE NOCASE OR a.evidence_ref=? COLLATE NOCASE)
        AND (l.source_id IS NULL OR l.source_id<>?)""",(evidence_ref,entry["ref"],entry["ref"])).fetchone():
        raise Conflict("Evidence reference is already used by another active allocation; reconcile it first.")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_square_invoice_observations'").fetchone():
        if db.execute("""SELECT 1 FROM hq_square_invoice_observations
            WHERE payment_id=? COLLATE NOCASE OR payment_id=? COLLATE NOCASE""",
            (evidence_ref, entry["ref"])).fetchone():
            raise Conflict("Evidence is already retained for a Square card payment; do not record it again as a bank receipt.")
    allocated = db.execute("""SELECT COALESCE(SUM(amount_pence),0)
        FROM hq_receivable_allocations WHERE invoice_id=? AND status='reported'""",(invoice["id"],)).fetchone()[0]
    reserved = db.execute("""SELECT COALESCE(SUM(e.amount_pence),0) FROM hq_monzo_invoice_match_events e
        WHERE e.invoice_id=? AND e.status='proposed' AND NOT EXISTS (
        SELECT 1 FROM hq_monzo_invoice_match_events n WHERE n.source_id=e.source_id
        AND n.invoice_id=e.invoice_id AND n.revision>e.revision)""",(invoice["id"],)).fetchone()[0]
    if allocated + reserved > invoice["amount_pence"]:
        raise Conflict("Invoice capacity changed; reconcile existing allocations first.")
    manifest = fingerprint(["owner_report", candidate["manifest"], digest, rev, evidence_ref,
        invoice["id"],invoice["revision"],invoice["customer_id"],invoice["customer_name"],
        invoice["amount_pence"],allocated,reserved,[(e["revision"],e["amount_pence"]) for e in active],
        sorted((e["allocation_id"],e["amount_pence"]) for e in linked)])
    return dict(candidate=candidate,invoice=dict(invoice),entry=entry,evidence_ref=evidence_ref,manifest=manifest)


def record_report(db, preview, actor, operation):
    """Caller holds BEGIN IMMEDIATE and has rechecked the exact approval manifest."""
    candidate,entry = preview["candidate"],preview["entry"]
    stamp = datetime.now(timezone.utc).isoformat()
    identifier = uuid.uuid4().hex
    evidence = preview["evidence_ref"]
    fields = dict(id=identifier,invoice_id=candidate["invoice_id"],amount_pence=candidate["amount_pence"],
        received_on=entry["day"],method="bank_reported",evidence_ref=evidence,
        memo="Owner attested separate receipt evidence; provider verification outstanding.",
        status="reported",revision=1)
    db.execute("""INSERT INTO hq_receivable_allocations
        (id,invoice_id,amount_pence,received_on,method,evidence_ref,memo,status,revision,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(*fields.values(),stamp,stamp))
    db.execute("INSERT INTO hq_receivable_allocation_events VALUES(?,?,?,?,?)",
        (uuid.uuid4().hex,identifier,1,json.dumps(fields,sort_keys=True),stamp))
    db.execute("INSERT INTO hq_monzo_reported_links VALUES(?,?,?,?,?)",
        (entry["ref"],candidate["revision"],source_digest(entry),identifier,evidence))
    next_revision = history(db,entry["ref"])[0]["revision"]+1
    db.execute("INSERT INTO hq_monzo_invoice_match_events VALUES(?,?,?,?,?,?,?,?,?,?)",
        (entry["ref"],next_revision,candidate["invoice_id"],candidate["invoice_ref"],candidate["amount_pence"],
         preview["manifest"],"withdrawn",operation,actor,stamp))


def current_entry(ref):
    from hq_monzo_sheet_classifications import read_feed
    provider = current_app.extensions["hq_sheets_connection"]()
    if not provider or provider[1] <= time.time():
        raise ValueError("Connect Google Sheets read-only first.")
    entry = read_feed(provider[0]).get(ref)
    if not entry:
        raise Conflict("Source transaction is no longer present.")
    return entry


def register(bp):
    @bp.route("/hq/finance/monzo-invoice-match", methods=["GET","POST"])
    @required
    def monzo_invoice_match():
        if not storage_ready():
            abort(503)
        ref = request.args.get("ref","") if request.method == "GET" else ""
        invoice_ref, preview, events, notice, error, status = "", None, [], None, None, 200
        raw_amount = ""
        active = []
        linked = []
        report = None
        try:
            if request.method == "POST":
                request.max_content_length = 4096
                csrf = session.get("csrf")
                if not csrf or not secrets.compare_digest(request.form.get("csrf",""),csrf):
                    abort(403)
                ref = request.form.get("ref","")
                invoice_ref = request.form.get("invoice_ref","")
                action = request.form.get("action","")
                if not ID.fullmatch(ref) or action not in ("review","save","withdraw","report_review","report"):
                    raise ValueError("Enter a valid source transaction ID.")
                if not REF.fullmatch(invoice_ref):
                    raise ValueError("Enter the exact HQ invoice reference.")
                actor = _actor_fingerprint(session.get("google_sub",""))
                revision = request.form.get("revision","")
                manifest = request.form.get("manifest","")
                if action not in ("review","report_review") and (not re.fullmatch(r"[0-9]{1,18}",revision)
                        or not DIGEST.fullmatch(manifest) or request.form.get("confirm")!="yes"):
                    raise ValueError("Explicit confirmation of the reviewed candidate is required.")
                raw_amount = request.form.get("candidate_amount","")
                amount = parse_candidate_amount(raw_amount) if action in ("review","save") else None
                entry = current_entry(ref) if action != "withdraw" else None
                with closing(connect()) as db, db:
                    tables(db)
                    db.execute("BEGIN IMMEDIATE" if action not in ("review","report_review") else "BEGIN")
                    if action == "review":
                        preview = review(db,entry,invoice_ref,amount)
                    elif action in ("report_review","report"):
                        if not re.fullmatch(r"[0-9]{1,18}",revision):
                            raise ValueError("Choose a saved candidate revision.")
                        rev = int(revision)
                        evidence_ref = request.form.get("evidence_ref","").strip()
                        operation = fingerprint([ref,invoice_ref,action,rev,manifest,actor,evidence_ref])
                        prior = db.execute("SELECT revision FROM hq_monzo_invoice_match_events WHERE operation=?",
                                           (operation,)).fetchone()
                        if prior:
                            notice = "This owner-reported allocation was already recorded."
                        else:
                            report = report_preview(db,entry,invoice_ref,rev,evidence_ref)
                            if action == "report":
                                if not secrets.compare_digest(report["manifest"],manifest):
                                    raise Conflict("Source, invoice or allocation changed. Review again.")
                                record_report(db,report,actor,operation)
                                report = None
                                notice = "Owner-reported allocation saved. Not provider verified; no cash entry created."
                    else:
                        rev = int(revision)
                        operation = fingerprint([ref,invoice_ref,action,rev,manifest,actor])
                        if action == "save" and amount is not None:
                            operation = fingerprint([operation,amount])
                        prior = db.execute("SELECT revision FROM hq_monzo_invoice_match_events WHERE operation=?",
                                           (operation,)).fetchone()
                        if prior:
                            notice = "This candidate action was already recorded."
                        elif action == "save":
                            candidate = review(db,entry,invoice_ref,amount)
                            if candidate["revision"] != rev or not secrets.compare_digest(candidate["manifest"],manifest):
                                raise Conflict("Source, invoice or candidate capacity changed. Review again.")
                            inv = candidate["invoice"]
                            db.execute("INSERT INTO hq_monzo_invoice_match_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (ref,rev+1,inv["id"],inv["invoice_ref"],candidate["amount"],manifest,
                                 "proposed",operation,actor,datetime.now(timezone.utc).isoformat()))
                            db.execute("INSERT INTO hq_monzo_invoice_match_source_evidence VALUES(?,?,?)",
                                       (ref,rev+1,candidate["source_digest"]))
                            notice = "Private invoice match candidate saved. No payment or balance changed."
                        else:
                            old = history(db,ref)
                            prev = next((e for e in active_candidates(db,ref)
                                         if e["invoice_ref"] == invoice_ref and e["revision"] == rev
                                         and e["manifest"] == manifest),None)
                            if not prev:
                                raise Conflict("Candidate changed. Reload before withdrawing.")
                            db.execute("INSERT INTO hq_monzo_invoice_match_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (ref,old[0]["revision"]+1,prev["invoice_id"],prev["invoice_ref"],prev["amount_pence"],manifest,
                                 "withdrawn",operation,actor,datetime.now(timezone.utc).isoformat()))
                            notice = "Candidate withdrawn. Audit history preserved."
                    events = history(db,ref)
                    active = active_candidates(db,ref)
                    linked = reported_links(db,ref)
            elif ref:
                if not ID.fullmatch(ref):
                    raise ValueError("Invalid source transaction ID.")
                with closing(connect()) as db, db:
                    tables(db)
                    events = history(db,ref)
                    active = active_candidates(db,ref)
                    linked = reported_links(db,ref)
        except Conflict as exc:
            error,status = str(exc),409
        except (ValueError,TypeError) as exc:
            error,status = str(exc),400
        except (requests.RequestException,sqlite3.Error,OSError,KeyError):
            error,status = "Read-only source or HQ storage unavailable; no new save confirmed.",503
        return render_template("finance_monzo_invoice_match.html",ref=ref,invoice_ref=invoice_ref,
            preview=preview,events=events,active=active,linked=linked,report=report,notice=notice,error=error,raw_amount=raw_amount,csrf=session.get("csrf","")),status,{"Cache-Control":"no-store"}
