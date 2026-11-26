"""Private single and batched settlement evidence; never posts bank cash."""
from contextlib import closing
from datetime import datetime, timezone
import json
import re
import secrets
import sqlite3
import uuid

import requests
from flask import abort, render_template, request, session, url_for
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_receivables import HEX
from hq_square_invoice_evidence import digest, load_allocation, observation, recheck_history, recheck_tables
from hq_monzo_invoice_matches import (current_entry, source_digest, active_candidates,
                                      reported_links, tables as match_tables)
from hq_monzo_sheet_classifications import ID
from hq_work_progress import _actor_fingerprint

def tables(db):
    recheck_tables(db)
    match_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS hq_square_settlement_reviews (
        id INTEGER PRIMARY KEY, allocation_id TEXT NOT NULL,
        source_id TEXT NOT NULL, manifest TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('proposed','withdrawn')),
        evidence TEXT NOT NULL, actor TEXT NOT NULL,
        created TEXT NOT NULL, operation TEXT NOT NULL UNIQUE)""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_settlement_review_lookup
        ON hq_square_settlement_reviews(allocation_id, id)""")

    db.execute("""CREATE TABLE IF NOT EXISTS hq_square_batch_reviews (
        id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, manifest TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('proposed','withdrawn')),
        evidence TEXT NOT NULL, actor TEXT NOT NULL, created TEXT NOT NULL,
        operation TEXT NOT NULL UNIQUE)""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_square_batch_source
        ON hq_square_batch_reviews(source_id,id)""")

def active_batches(db):
    return [dict(r) for r in db.execute("""SELECT e.* FROM hq_square_batch_reviews e
        WHERE state='proposed' AND NOT EXISTS (SELECT 1 FROM hq_square_batch_reviews n
            WHERE n.source_id=e.source_id AND n.id>e.id)""")]

def check_batch_conflict(db, source, allocations):
    for batch in active_batches(db):
        members = json.loads(batch["evidence"])["members"]
        if batch["source_id"] == source or set(allocations).intersection(
                member["allocation_id"] for member in members):
            raise ValueError("Withdraw the existing batch settlement proposal first.")

def active(db):
    return [dict(r) for r in db.execute("""SELECT e.* FROM hq_square_settlement_reviews e
        WHERE state='proposed' AND NOT EXISTS (SELECT 1 FROM hq_square_settlement_reviews n
            WHERE n.allocation_id=e.allocation_id AND n.id>e.id)""")]

def payment_member(db, allocation_id, entry):
    if not HEX.fullmatch(allocation_id) or not ID.fullmatch(entry["ref"]):
        raise ValueError("Choose exact allocation and source IDs.")
    row = load_allocation(db, allocation_id)
    original = observation(db, allocation_id)
    if not original or row["status"] != "reported" or row["method"] != "card_reported" or row["invoice_status"] != "issued" or row["invoice_archived"]:
        raise ValueError("An active issued invoice with retained Square payment evidence is required.")
    if (row["revision"] != original["allocation_revision"] or
            row["invoice_revision"] != original["invoice_revision"]):
        raise ValueError("Invoice or allocation changed after the original observation.")
    checks = recheck_history(db, original["payment_id"])
    latest = checks[0] if checks else None
    evidence = latest["evidence"] if latest else json.loads(original["provider_snapshot"])
    gross = row["amount_pence"]
    if (evidence["id"] != original["payment_id"] or evidence["currency"] != "GBP" or
            evidence["status"] != "COMPLETED" or type(evidence["refunded_minor"]) is not int or
            evidence["refunded_minor"] != 0 or evidence["total_minor"] != gross):
        raise ValueError("Only an unchanged completed GBP payment with explicit zero refunds is supported.")
    if entry["day"] < datetime.fromisoformat(evidence["created_at"]).date().isoformat():
        raise ValueError("Deposit predates the payment observation.")
    return dict(allocation_id=allocation_id, invoice_ref=row["invoice_ref"],
                allocation_revision=row["revision"], invoice_revision=row["invoice_revision"],
                payment_id=original["payment_id"], observation_manifest=original["manifest"],
                recheck_id=latest["id"] if latest else None, gross=gross)

def check_source(db, entry, allocations):
    from hq_creditor_transaction_matches import require_no_match
    require_no_match(db, entry["ref"])
    if dna_active(db, source=entry["ref"]):
        raise ValueError("Withdraw the existing DNA payout proposal first.")
    if active_candidates(db, entry["ref"]) or reported_links(db, entry["ref"]):
        raise ValueError("This source already has invoice match or reported allocation evidence.")
    if any(r["allocation_id"] in allocations or r["source_id"] == entry["ref"] for r in active(db)):
        raise ValueError("Withdraw the existing settlement proposal first.")
    check_batch_conflict(db, entry["ref"], allocations)

def amount_evidence(entry, gross, fee, reference):
    if type(fee) is not int or fee < 0:
        raise ValueError("Enter a known non-negative fee in exact pennies.")
    if not isinstance(reference, str) or not 1 <= len(reference.strip()) <= 160:
        raise ValueError("Enter the independent settlement statement reference.")
    if fee >= gross or entry["pence"] != gross-fee or entry["pence"] <= 0:
        raise ValueError("Gross less the stated fee must exactly equal the positive worksheet deposit.")
    return dict(source_id=entry["ref"], source_digest=source_digest(entry),
                source_date=entry["day"], gross=gross, fee=fee, net=entry["pence"],
                reference=reference.strip())

def preview(db, allocation_id, entry, fee_pence, reference):
    member = payment_member(db, allocation_id, entry)
    data = {**member, **amount_evidence(entry, member["gross"], fee_pence, reference)}
    check_source(db, entry, [allocation_id])
    return data, digest(data)

def batch_ids(raw):
    ids = raw.split()
    if not 2 <= len(ids) <= 20 or len(set(ids)) != len(ids) or any(not HEX.fullmatch(i) for i in ids):
        raise ValueError("Enter 2 to 20 distinct exact HQ allocation IDs, one per line.")
    return sorted(ids)

def batch_preview(db, allocations, entry, fee, reference):
    members = [payment_member(db, i, entry) for i in allocations]
    if len({m["payment_id"] for m in members}) != len(members):
        raise ValueError("A provider payment cannot appear twice.")
    data = amount_evidence(entry, sum(m["gross"] for m in members), fee, reference)
    check_source(db, entry, allocations)
    data["members"] = members
    return data, digest(data)

def batch_save(db, allocations, entry, fee, reference, manifest, operation, actor):
    old = db.execute("SELECT * FROM hq_square_batch_reviews WHERE operation=?", (operation,)).fetchone()
    if old:
        data = json.loads(old["evidence"])
        if (old["state"] == "proposed" and old["actor"] == actor and
                old["manifest"] == manifest and old["source_id"] == entry["ref"] and
                data.get("source_digest") == source_digest(entry) and data.get("fee") == fee and
                data.get("reference") == reference.strip() and
                [m["allocation_id"] for m in data.get("members", [])] == allocations):
            return
        raise ValueError("Operation already used with different evidence.")
    data, expected = batch_preview(db, allocations, entry, fee, reference)
    if not secrets.compare_digest(expected, manifest):
        raise ValueError("Evidence changed; prepare a fresh preview.")
    db.execute("""INSERT INTO hq_square_batch_reviews
        (source_id,manifest,state,evidence,actor,created,operation)
        VALUES(?,?,'proposed',?,?,?,?)""",
        (entry["ref"],manifest,json.dumps(data,sort_keys=True),actor,
         datetime.now(timezone.utc).isoformat(),operation))

def batch_withdraw(db, event_id, operation, actor):
    old = db.execute("SELECT * FROM hq_square_batch_reviews WHERE operation=?", (operation,)).fetchone()
    if old:
        if (old["state"] == "withdrawn" and old["actor"] == actor and
                json.loads(old["evidence"]).get("withdraws") == event_id):
            return
        raise ValueError("Operation already used.")
    row = next((r for r in active_batches(db) if r["id"] == event_id), None)
    if not row:
        raise ValueError("Proposal changed; reload before withdrawing.")
    db.execute("""INSERT INTO hq_square_batch_reviews
        (source_id,manifest,state,evidence,actor,created,operation)
        VALUES(?,?,'withdrawn',?,?,?,?)""",
        (row["source_id"],row["manifest"],json.dumps({"withdraws":event_id}),
         actor,datetime.now(timezone.utc).isoformat(),operation))

def save(db, allocation_id, entry, fee, reference, manifest, operation, actor):
    old = db.execute("SELECT * FROM hq_square_settlement_reviews WHERE operation=?", (operation,)).fetchone()
    if old:
        previous = json.loads(old["evidence"])
        if (old["allocation_id"] == allocation_id and old["manifest"] == manifest
                and old["actor"] == actor and old["state"] == "proposed"
                and previous.get("source_id") == entry["ref"]
                and previous.get("source_digest") == source_digest(entry)
                and previous.get("fee") == fee and previous.get("reference") == reference.strip()):
            return
        raise ValueError("Operation already used with different evidence.")
    data, expected = preview(db, allocation_id, entry, fee, reference)
    if not secrets.compare_digest(expected, manifest):
        raise ValueError("Evidence changed; prepare a fresh preview.")
    db.execute("""INSERT INTO hq_square_settlement_reviews
        (allocation_id,source_id,manifest,state,evidence,actor,created,operation)
        VALUES(?,?,?,'proposed',?,?,?,?)""",
        (allocation_id, entry["ref"], manifest, json.dumps(data,sort_keys=True),
         actor, datetime.now(timezone.utc).isoformat(), operation))

def withdraw(db, allocation_id, event_id, operation, actor):
    old = db.execute("SELECT * FROM hq_square_settlement_reviews WHERE operation=?", (operation,)).fetchone()
    if old:
        if (old["state"] == "withdrawn" and old["allocation_id"] == allocation_id
                and old["actor"] == actor and json.loads(old["evidence"]).get("withdraws") == event_id):
            return
        raise ValueError("Operation already used.")
    row = db.execute("SELECT * FROM hq_square_settlement_reviews WHERE allocation_id=? ORDER BY id DESC LIMIT 1",
                     (allocation_id,)).fetchone()
    if not row or row["id"] != event_id or row["state"] != "proposed":
        raise ValueError("Proposal changed; reload before withdrawing.")
    db.execute("""INSERT INTO hq_square_settlement_reviews
        (allocation_id,source_id,manifest,state,evidence,actor,created,operation)
        VALUES(?,?,?,'withdrawn',?,?,?,?)""",
        (allocation_id,row["source_id"],row["manifest"],json.dumps({"withdraws":event_id}),
         actor,datetime.now(timezone.utc).isoformat(),operation))

def register(bp):
    @bp.route("/hq/finance/settlement-review", methods=["GET","POST"])
    @required
    def settlement_review():
        if not storage_ready():
            return "Persistent storage required.", 503, {"Cache-Control":"no-store"}
        error, proposed, code = None, None, 200
        form = request.form if request.method == "POST" else request.args
        allocation_id = form.get("allocation", "")
        source = form.get("source", "")
        fee_raw = form.get("fee", "")
        reference = form.get("reference", "")
        try:
            if request.method == "POST":
                if request.content_length is None or request.content_length > 8192:
                    abort(413)
                csrf = session.get("csrf")
                if not csrf or not secrets.compare_digest(form.get("csrf",""),csrf):
                    abort(403)
                action, operation = form.get("action"), form.get("operation","")
                if not HEX.fullmatch(allocation_id) or not HEX.fullmatch(operation):
                    raise ValueError("Invalid allocation or operation ID.")
                actor = _actor_fingerprint(session.get("google_sub"))
                if action == "withdraw":
                    if form.get("confirm") != "yes":
                        raise ValueError("Confirm withdrawal.")
                    with closing(connect()) as db, db:
                        tables(db)
                        db.execute("BEGIN IMMEDIATE")
                        withdraw(db,allocation_id,int(form.get("event","")),operation,actor)
                    return "",303,{"Location":url_for(".settlement_review"),"Cache-Control":"no-store"}
                if action not in ("preview","save") or not ID.fullmatch(source):
                    raise ValueError("Choose an exact worksheet transaction ID.")
                if not re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,2})?",fee_raw):
                    raise ValueError("Enter the known fee in pounds, including zero only if evidenced.")
                whole, _, fraction = fee_raw.partition(".")
                fee = int(whole)*100+int(fraction.ljust(2,"0"))
                entry = current_entry(source)
                with closing(connect()) as db, db:
                    tables(db)
                    if action == "preview":
                        proposed = preview(db,allocation_id,entry,fee,reference)
                    else:
                        if form.get("confirm") != "yes" or not re.fullmatch(r"[0-9a-f]{64}",form.get("manifest","")):
                            raise ValueError("Confirm the exact evidence preview first.")
                        db.execute("BEGIN IMMEDIATE")
                        save(db,allocation_id,entry,fee,reference,form["manifest"],operation,actor)
                        return "",303,{"Location":url_for(".settlement_review"),"Cache-Control":"no-store"}
        except ValueError as exc:
            error, code = str(exc), 409
        except (requests.RequestException, sqlite3.Error, OSError, RuntimeError, KeyError):
            error, code = "Source or storage unavailable; no save confirmed.", 503
        try:
            with closing(connect()) as db, db:
                tables(db)
                rows = [dict(r) for r in db.execute("SELECT * FROM hq_square_settlement_reviews ORDER BY id DESC LIMIT 50")]
                current = {r["id"] for r in active(db)}
                for row in rows:
                    row["details"] = json.loads(row["evidence"])
        except (sqlite3.Error,OSError,RuntimeError):
            return "Persistent storage unavailable.",503,{"Cache-Control":"no-store"}
        return render_template("hq_settlement_review.html", error=error, proposed=proposed,
            allocation=allocation_id, source=source, fee=fee_raw, reference=reference,
            rows=rows,current=current,csrf=session.get("csrf",""),operation=uuid.uuid4().hex),code,{"Cache-Control":"no-store"}


    @bp.route("/hq/finance/settlement-batches", methods=["GET","POST"])
    @required
    def settlement_batches():
        if not storage_ready():
            return "Persistent storage required.",503,{"Cache-Control":"no-store"}
        error, proposed, code = None, None, 200
        form = request.form if request.method == "POST" else request.args
        raw, source = form.get("allocations",""), form.get("source","")
        fee_raw, reference = form.get("fee",""), form.get("reference","")
        try:
            if request.method == "POST":
                if request.content_length is None or request.content_length > 8192:
                    abort(413)
                csrf = session.get("csrf")
                if not csrf or not secrets.compare_digest(form.get("csrf",""),csrf):
                    abort(403)
                action, operation = form.get("action"), form.get("operation","")
                if not HEX.fullmatch(operation):
                    raise ValueError("Invalid operation ID.")
                actor = _actor_fingerprint(session.get("google_sub"))
                if action == "withdraw":
                    if form.get("confirm") != "yes":
                        raise ValueError("Confirm withdrawal.")
                    with closing(connect()) as db, db:
                        tables(db)
                        db.execute("BEGIN IMMEDIATE")
                        batch_withdraw(db,int(form.get("event","")),operation,actor)
                    return "",303,{"Location":url_for(".settlement_batches"),"Cache-Control":"no-store"}
                if action not in ("preview","save") or not ID.fullmatch(source):
                    raise ValueError("Choose an exact worksheet transaction ID.")
                allocations = batch_ids(raw)
                if not re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,2})?",fee_raw):
                    raise ValueError("Enter the known total fee in pounds, including zero only if evidenced.")
                whole, _, fraction = fee_raw.partition(".")
                fee = int(whole)*100+int(fraction.ljust(2,"0"))
                entry = current_entry(source)
                if entry["ref"] != source:
                    raise ValueError("Source ID mismatch.")
                with closing(connect()) as db, db:
                    tables(db)
                    if action == "preview":
                        proposed = batch_preview(db,allocations,entry,fee,reference)
                    else:
                        if form.get("confirm") != "yes" or not re.fullmatch(r"[0-9a-f]{64}",form.get("manifest","")):
                            raise ValueError("Confirm the exact evidence preview first.")
                        db.execute("BEGIN IMMEDIATE")
                        batch_save(db,allocations,entry,fee,reference,form["manifest"],operation,actor)
                        return "",303,{"Location":url_for(".settlement_batches"),"Cache-Control":"no-store"}
        except ValueError as exc:
            error, code = str(exc),409
        except (requests.RequestException, sqlite3.Error, OSError, RuntimeError, KeyError):
            error, code = "Source or storage unavailable; no save confirmed.",503
        try:
            with closing(connect()) as db, db:
                tables(db)
                rows = [dict(r) for r in db.execute("SELECT * FROM hq_square_batch_reviews ORDER BY id DESC LIMIT 50")]
                current = {r["id"] for r in active_batches(db)}
                for row in rows:
                    row["details"] = json.loads(row["evidence"])
                    row["undo_operation"] = uuid.uuid4().hex
        except (sqlite3.Error,OSError,RuntimeError):
            return "Persistent storage unavailable.",503,{"Cache-Control":"no-store"}
        return render_template("hq_settlement_batches.html",error=error,proposed=proposed,
            allocations=raw,source=source,fee=fee_raw,reference=reference,rows=rows,
            current=current,csrf=session.get("csrf",""),operation=uuid.uuid4().hex),code,{"Cache-Control":"no-store"}


    @bp.route("/hq/finance/dna-payout-review", methods=["GET", "POST"])
    @required
    def dna_payout_review():
        if not storage_ready():
            return "Persistent storage required.", 503, {"Cache-Control": "no-store"}
        form = request.form if request.method == "POST" else request.args
        values = {k: form.get(k, "") for k in ("source", "payout", "gross", "fee", "reference")}
        proposed, error, status = None, None, 200
        operation = uuid.uuid4().hex
        try:
            if request.method == "POST":
                if request.content_length is None or request.content_length > 8192:
                    abort(413)
                csrf = session.get("csrf")
                if not csrf or not secrets.compare_digest(form.get("csrf", ""), csrf):
                    abort(403)
                operation = form.get("operation", "")
                action = form.get("action")
                if not HEX.fullmatch(operation):
                    raise ValueError("Invalid operation.")
                actor = _actor_fingerprint(session.get("google_sub"))
                if action == "withdraw":
                    if form.get("confirm") != "yes":
                        raise ValueError("Confirm withdrawal.")
                    with closing(connect()) as db, db:
                        dna_tables(db)
                        db.execute("BEGIN IMMEDIATE")
                        dna_withdraw(db, int(form.get("event", "")), operation, actor)
                elif action in ("preview", "save"):
                    if not ID.fullmatch(values["source"]):
                        raise ValueError("Choose the exact Monzo transaction ID.")
                    entry = current_entry(values["source"])
                    if entry["ref"] != values["source"]:
                        raise ValueError("Source ID mismatch.")
                    gross, fee = (dna_money(values[k]) for k in ("gross", "fee"))
                    with closing(connect()) as db, db:
                        tables(db)
                        dna_tables(db)
                        db.execute("BEGIN IMMEDIATE")
                        if action == "preview":
                            proposed = dna_preview(db, entry, values["payout"], gross, fee, values["reference"])
                        else:
                            stamp = form.get("manifest", "")
                            if form.get("confirm") != "yes" or not re.fullmatch(r"[0-9a-f]{64}", stamp):
                                raise ValueError("Confirm the exact evidence preview first.")
                            dna_save(db, entry, values["payout"], gross, fee, values["reference"], stamp, operation, actor)
                else:
                    abort(400)
                if action != "preview":
                    return "", 303, {"Location": url_for(".dna_payout_review"), "Cache-Control": "no-store"}
        except ValueError as exc:
            error, status = str(exc), 409
        except (requests.RequestException, sqlite3.Error, OSError, RuntimeError, KeyError):
            error, status = "Source or storage unavailable; no save confirmed.", 503
        try:
            with closing(connect()) as db, db:
                dna_tables(db)
                rows = [dict(r) for r in db.execute("SELECT * FROM hq_dna_payout_reviews ORDER BY id DESC LIMIT 50")]
                current = {r["id"] for r in dna_active(db)}
                for row in rows:
                    row["details"] = json.loads(row["evidence"])
                    row["undo_operation"] = uuid.uuid4().hex
        except (sqlite3.Error, OSError, RuntimeError):
            return "Persistent storage unavailable.", 503, {"Cache-Control": "no-store"}
        return render_template("hq_dna_payout_review.html", values=values, proposed=proposed,
            error=error, rows=rows, current=current, operation=operation, csrf=session.get("csrf", "")), status, {"Cache-Control": "no-store"}


def dna_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_dna_payout_reviews (
        id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, payout_id TEXT NOT NULL,
        manifest TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('proposed','withdrawn')),
        evidence TEXT NOT NULL, actor TEXT NOT NULL, created TEXT NOT NULL,
        operation TEXT NOT NULL UNIQUE)""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_dna_payout_lookup
        ON hq_dna_payout_reviews(payout_id,id)""")


def dna_active(db, source=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_dna_payout_reviews'").fetchone():
        return []
    return [dict(r) for r in db.execute("""SELECT e.* FROM hq_dna_payout_reviews e
        WHERE e.state='proposed' AND (? IS NULL OR e.source_id=?)
        AND NOT EXISTS (SELECT 1 FROM hq_dna_payout_reviews n
            WHERE n.payout_id=e.payout_id AND n.id>e.id)""", (source, source))]


def dna_money(raw):
    if not re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,2})?", raw):
        raise ValueError("Enter a documented amount in pounds, including zero only if evidenced.")
    whole, _, fraction = raw.partition(".")
    result = int(whole)*100 + int(fraction.ljust(2, "0"))
    if result > 1000000000:
        raise ValueError("Amount outside supported bounds.")
    return result


def dna_evidence(entry, payout, gross, fee, reference):
    # Exact provider statement ID; no inference from a bank merchant label.
    payout = payout.strip()
    if not ID.fullmatch(payout) or not ID.fullmatch(entry["ref"]):
        raise ValueError("Enter the exact DNA payout and bank transaction IDs.")
    if type(gross) is not int or not 0 < gross <= 1000000000:
        raise ValueError("Enter a positive documented gross payout.")
    data = amount_evidence(entry, gross, fee, reference)
    return dict(data, provider="DNA Payments", payout_id=payout)


def dna_preview(db, entry, payout, gross, fee, reference):
    data = dna_evidence(entry, payout, gross, fee, reference)
    check_source(db, entry, [])
    if any(r["payout_id"].casefold() == data["payout_id"].casefold() for r in dna_active(db)):
        raise ValueError("Withdraw the existing DNA payout proposal first.")
    return data, digest(data)


def dna_save(db, entry, payout, gross, fee, reference, stamp, operation, actor):
    data = dna_evidence(entry, payout, gross, fee, reference)
    old = db.execute("SELECT * FROM hq_dna_payout_reviews WHERE operation=?", (operation,)).fetchone()
    if old:
        if (old["state"] == "proposed" and old["actor"] == actor
                and old["manifest"] == stamp == digest(data)
                and any(r["id"] == old["id"] for r in dna_active(db))):
            return
        raise ValueError("Operation already used or proposal withdrawn.")
    data, expected = dna_preview(db, entry, payout, gross, fee, reference)
    if not secrets.compare_digest(expected, stamp):
        raise ValueError("Evidence changed; prepare a fresh preview.")
    db.execute("""INSERT INTO hq_dna_payout_reviews
        (source_id,payout_id,manifest,state,evidence,actor,created,operation)
        VALUES(?,?,?,'proposed',?,?,?,?)""",
        (data["source_id"],data["payout_id"],stamp,json.dumps(data,sort_keys=True),
         actor,datetime.now(timezone.utc).isoformat(),operation))


def dna_withdraw(db, event, operation, actor):
    old = db.execute("SELECT * FROM hq_dna_payout_reviews WHERE operation=?", (operation,)).fetchone()
    if old:
        if old["state"] == "withdrawn" and old["actor"] == actor and json.loads(old["evidence"]).get("withdraws") == event:
            return
        raise ValueError("Operation already used.")
    row = next((r for r in dna_active(db) if r["id"] == event), None)
    if not row:
        raise ValueError("Proposal changed; reload before withdrawing.")
    db.execute("""INSERT INTO hq_dna_payout_reviews
        (source_id,payout_id,manifest,state,evidence,actor,created,operation)
        VALUES(?,?,?,'withdrawn',?,?,?,?)""",
        (row["source_id"],row["payout_id"],row["manifest"],json.dumps({"withdraws":event}),
         actor,datetime.now(timezone.utc).isoformat(),operation))
