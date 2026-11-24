"""Owner-reviewed Monzo worksheet to invoice candidates, never receipts."""
import re
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone

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


def history(db, ref):
    return [dict(r) for r in db.execute("""SELECT * FROM hq_monzo_invoice_match_events
        WHERE source_id=? ORDER BY revision DESC LIMIT 50""", (ref,))]


def review(db, entry, invoice_ref):
    invoice = db.execute("""SELECT r.*,c.name AS customer_name,w.service AS work_service
        FROM hq_receivables r JOIN hq_customers c ON r.customer_id=c.id
        LEFT JOIN hq_work_records w ON r.work_id=w.id
        WHERE r.invoice_ref=? COLLATE NOCASE""", (invoice_ref,)).fetchone()
    if not invoice or invoice["archived"] or invoice["status"] != "issued":
        raise ValueError("Choose an existing, issued, non-archived invoice.")
    if entry["pence"] <= 0:
        raise ValueError("Only positive worksheet transactions can be proposed against invoices.")
    events = history(db, entry["ref"])
    if events and events[0]["status"] == "proposed":
        raise Conflict("Withdraw the existing candidate before proposing another.")
    reported = db.execute("""SELECT COALESCE(SUM(amount_pence),0)
        FROM hq_receivable_allocations WHERE invoice_id=? AND status='reported'""",
        (invoice["id"],)).fetchone()[0]
    proposed = db.execute("""SELECT COALESCE(SUM(e.amount_pence),0)
        FROM hq_monzo_invoice_match_events e WHERE e.invoice_id=? AND e.status='proposed'
        AND NOT EXISTS (SELECT 1 FROM hq_monzo_invoice_match_events n
          WHERE n.source_id=e.source_id AND n.revision>e.revision)""",
        (invoice["id"],)).fetchone()[0]
    remaining = invoice["amount_pence"] - reported - proposed
    if entry["pence"] > remaining:
        raise ValueError("Source exceeds the remaining unallocated candidate capacity. No split or overpayment is inferred.")
    rev = events[0]["revision"] if events else 0
    manifest = fingerprint([entry["ref"],entry["evidence"],entry["legacy_evidence"],
        invoice["id"],invoice["revision"],invoice["invoice_ref"],invoice["customer_id"],
        invoice["work_id"],invoice["customer_name"],invoice["work_service"],
        invoice["amount_pence"],reported,proposed,entry["pence"],rev])
    return dict(invoice=dict(invoice), entry=entry, revision=rev, manifest=manifest,
                reported=reported, proposed=proposed, remaining=remaining)


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
        try:
            if request.method == "POST":
                request.max_content_length = 4096
                csrf = session.get("csrf")
                if not csrf or not secrets.compare_digest(request.form.get("csrf",""),csrf):
                    abort(403)
                ref = request.form.get("ref","")
                invoice_ref = request.form.get("invoice_ref","")
                action = request.form.get("action","")
                if not ID.fullmatch(ref) or action not in ("review","save","withdraw"):
                    raise ValueError("Enter a valid source transaction ID.")
                if action != "withdraw" and not REF.fullmatch(invoice_ref):
                    raise ValueError("Enter the exact HQ invoice reference.")
                actor = _actor_fingerprint(session.get("google_sub",""))
                revision = request.form.get("revision","")
                manifest = request.form.get("manifest","")
                if action != "review" and (not re.fullmatch(r"[0-9]{1,18}",revision)
                        or not DIGEST.fullmatch(manifest) or request.form.get("confirm")!="yes"):
                    raise ValueError("Explicit confirmation of the reviewed candidate is required.")
                entry = current_entry(ref) if action != "withdraw" else None
                with closing(connect()) as db, db:
                    tables(db)
                    db.execute("BEGIN IMMEDIATE" if action != "review" else "BEGIN")
                    if action == "review":
                        preview = review(db,entry,invoice_ref)
                    else:
                        rev = int(revision)
                        operation = fingerprint([ref,invoice_ref,action,rev,manifest,actor])
                        prior = db.execute("SELECT revision FROM hq_monzo_invoice_match_events WHERE operation=?",
                                           (operation,)).fetchone()
                        if prior:
                            notice = "This candidate action was already recorded."
                        elif action == "save":
                            candidate = review(db,entry,invoice_ref)
                            if candidate["revision"] != rev or not secrets.compare_digest(candidate["manifest"],manifest):
                                raise Conflict("Source, invoice or candidate capacity changed. Review again.")
                            inv = candidate["invoice"]
                            db.execute("INSERT INTO hq_monzo_invoice_match_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (ref,rev+1,inv["id"],inv["invoice_ref"],entry["pence"],manifest,
                                 "proposed",operation,actor,datetime.now(timezone.utc).isoformat()))
                            notice = "Private invoice match candidate saved. No payment or balance changed."
                        else:
                            old = history(db,ref)
                            if not old or old[0]["status"]!="proposed" or old[0]["revision"]!=rev or old[0]["manifest"]!=manifest:
                                raise Conflict("Candidate changed. Reload before withdrawing.")
                            prev = old[0]
                            db.execute("INSERT INTO hq_monzo_invoice_match_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (ref,rev+1,prev["invoice_id"],prev["invoice_ref"],prev["amount_pence"],manifest,
                                 "withdrawn",operation,actor,datetime.now(timezone.utc).isoformat()))
                            notice = "Candidate withdrawn. Audit history preserved."
                    events = history(db,ref)
            elif ref:
                if not ID.fullmatch(ref):
                    raise ValueError("Invalid source transaction ID.")
                with closing(connect()) as db, db:
                    tables(db)
                    events = history(db,ref)
        except Conflict as exc:
            error,status = str(exc),409
        except (ValueError,TypeError) as exc:
            error,status = str(exc),400
        except (requests.RequestException,sqlite3.Error,OSError,KeyError):
            error,status = "Read-only source or HQ storage unavailable; no new save confirmed.",503
        return render_template("finance_monzo_invoice_match.html",ref=ref,invoice_ref=invoice_ref,
            preview=preview,events=events,notice=notice,error=error,csrf=session.get("csrf","")),status,{"Cache-Control":"no-store"}
