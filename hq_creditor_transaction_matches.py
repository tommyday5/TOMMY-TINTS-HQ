"""Private owner-reviewed creditor outflow evidence, never a payment posting."""
from contextlib import closing
from datetime import datetime, timezone
import json
import secrets
import sqlite3
import uuid

import requests
from flask import abort, render_template, request, session
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_creditor_cases import ID as CASE_ID, _tables as case_tables
from hq_monzo_sheet_classifications import ID, DIGEST, fingerprint, Conflict, tables as classification_tables
from hq_work_progress import _actor_fingerprint


def tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_creditor_transaction_events (
        id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, case_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('proposed','withdrawn')),
        manifest TEXT NOT NULL, payload TEXT NOT NULL, operation TEXT NOT NULL UNIQUE,
        actor TEXT NOT NULL, created TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_creditor_transaction_source
        ON hq_creditor_transaction_events(source_id,id)""")


def active(db, ref):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_creditor_transaction_events'").fetchone():
        return None
    row = db.execute("""SELECT * FROM hq_creditor_transaction_events
        WHERE source_id=? ORDER BY id DESC LIMIT 1""", (ref,)).fetchone()
    return dict(row) if row and row["state"] == "proposed" else None


def require_no_match(db, ref):
    if active(db, ref):
        raise Conflict("Withdraw the active creditor outflow proposal before reusing this source.")


def review(db, entry, case_id, evidence_ref):
    from hq_monzo_invoice_matches import active_candidates, reported_links, require_source_not_settled, source_digest
    if entry["pence"] >= 0:
        raise ValueError("Only negative worksheet outflows qualify.")
    if not CASE_ID.fullmatch(case_id):
        raise ValueError("Choose an exact native creditor case.")
    if not 1 <= len(evidence_ref.strip()) <= 160 or any(ord(c) < 32 or ord(c) == 127 for c in evidence_ref):
        raise ValueError("Enter a separate payment statement or creditor evidence reference.")
    require_no_match(db, entry["ref"])
    require_source_not_settled(db, entry["ref"])
    if active_candidates(db, entry["ref"]) or reported_links(db, entry["ref"]):
        raise Conflict("Existing invoice evidence conflicts with this source.")
    case = db.execute("SELECT * FROM hq_creditor_cases WHERE id=?", (case_id,)).fetchone()
    if not case or case["archived"]:
        raise ValueError("Choose an existing non-archived creditor case.")
    head = db.execute("""SELECT e.*,l.evidence AS legacy_evidence
        FROM monzo_sheet_classification_events e LEFT JOIN monzo_sheet_classification_legacy_evidence l
        ON e.source_id=l.source_id AND e.revision=l.revision
        WHERE e.source_id=? ORDER BY e.revision DESC LIMIT 1""", (entry["ref"],)).fetchone()
    if not head or head["category"] != "creditor_payment" or head["evidence"] != entry["evidence"] or head["legacy_evidence"] != entry["legacy_evidence"]:
        raise Conflict("Save a current whole-transaction creditor payment classification first. Split or stale proposals do not qualify.")
    state = json.loads(case["state"])
    previous = db.execute("SELECT COALESCE(MAX(id),0) FROM hq_creditor_transaction_events WHERE source_id=?", (entry["ref"],)).fetchone()[0]
    data = dict(source_id=entry["ref"], source_digest=source_digest(entry), source_date=entry["day"],
                source_name=entry["name"], pence=-entry["pence"], classification_revision=head["revision"],
                case_id=case_id, case_revision=case["revision"], creditor=state["creditor"],
                case_reference=state["reference"], scope=state["scope"], evidence_ref=evidence_ref.strip(),
                previous=previous)
    return data, fingerprint(data)


def append(db, source, case, state, manifest, payload, operation, actor):
    db.execute("""INSERT INTO hq_creditor_transaction_events
        (source_id,case_id,state,manifest,payload,operation,actor,created) VALUES(?,?,?,?,?,?,?,?)""",
        (source,case,state,manifest,json.dumps(payload,sort_keys=True),operation,actor,datetime.now(timezone.utc).isoformat()))


def register(bp):
    @bp.route("/hq/finance/creditor-transaction-match", methods=["GET","POST"])
    @required
    def creditor_transaction_match():
        from hq_monzo_invoice_matches import current_entry, tables as invoice_tables
        if not storage_ready():
            abort(503)
        if request.method == "POST":
            request.max_content_length = 4096
        form = request.form if request.method == "POST" else request.args
        ref, case_id = form.get("ref",""), form.get("case","")
        error = notice = preview = stamp = None
        status = 200
        evidence_ref = ""
        if request.method == "POST":
            csrf = session.get("csrf")
            if not csrf or not secrets.compare_digest(form.get("csrf",""),csrf):
                abort(403)
        if (ref and not ID.fullmatch(ref)) or (case_id and not CASE_ID.fullmatch(case_id)):
            abort(400)
        try:
            if request.method == "POST":
                action = form.get("action","")
                if not ref or action not in ("review","save","withdraw"):
                    raise ValueError("Enter the exact source transaction ID.")
                evidence_ref = form.get("evidence_ref","").strip()
                manifest = form.get("manifest","")
                operation = form.get("operation","")
                if action != "review" and (form.get("confirm")!="yes" or not DIGEST.fullmatch(manifest) or not CASE_ID.fullmatch(operation)):
                    raise ValueError("Explicit approval of the exact preview is required.")
                entry = current_entry(ref) if action != "withdraw" else None
                actor = _actor_fingerprint(session.get("google_sub",""))
                with closing(connect()) as db, db:
                    tables(db); case_tables(db); classification_tables(db); invoice_tables(db)
                    db.execute("BEGIN IMMEDIATE")
                    old = db.execute("SELECT * FROM hq_creditor_transaction_events WHERE operation=?", (operation,)).fetchone() if action != "review" else None
                    if old:
                        payload = json.loads(old["payload"])
                        expected = "proposed" if action == "save" else "withdrawn"
                        if (old["actor"],old["source_id"],old["case_id"],old["state"],old["manifest"]) != (actor,ref,case_id,expected,manifest):
                            raise Conflict("Operation already used with different details.")
                        if action == "save":
                            from hq_monzo_invoice_matches import source_digest
                            if payload["evidence_ref"] != evidence_ref or payload["source_digest"] != source_digest(entry):
                                raise Conflict("Source or evidence changed; review again.")
                        elif str(payload["withdraws"]) != form.get("event",""):
                            raise Conflict("Withdrawal operation already used with different details.")
                        notice = "This action was already recorded; no additional event saved."
                    elif action == "withdraw":
                        head = active(db,ref)
                        if not head or head["case_id"] != case_id or head["manifest"] != manifest or str(head["id"]) != form.get("event",""):
                            raise Conflict("Proposal changed; reload before withdrawing.")
                        append(db,ref,case_id,"withdrawn",manifest,{"withdraws":head["id"]},operation,actor)
                        notice = "Proposal withdrawn. No payment or case balance changed."
                    else:
                        data, expected = review(db,entry,case_id,evidence_ref)
                        if action == "review":
                            preview, stamp = data, expected
                        else:
                            if not secrets.compare_digest(expected,manifest):
                                raise Conflict("Source, classification, case or history changed; review again.")
                            append(db,ref,case_id,"proposed",manifest,data,operation,actor)
                            notice = "Creditor outflow proposal saved. No payment, balance or bank entry changed."
        except Conflict as exc:
            error,status = str(exc),409
        except ValueError as exc:
            error,status = str(exc),400
        except (sqlite3.Error,OSError,requests.RequestException):
            error,status = "Source or persistent storage unavailable; no save confirmed.",503
        try:
            with closing(connect()) as db, db:
                tables(db); case_tables(db)
                cases = [dict(id=row["id"], **json.loads(row["state"])) for row in db.execute(
                    "SELECT * FROM hq_creditor_cases WHERE archived=0 ORDER BY updated_at DESC LIMIT 250")]
                if ref:
                    rows = db.execute("SELECT * FROM hq_creditor_transaction_events WHERE source_id=? ORDER BY id DESC LIMIT 100",(ref,))
                elif case_id:
                    rows = db.execute("SELECT * FROM hq_creditor_transaction_events WHERE case_id=? ORDER BY id DESC LIMIT 100",(case_id,))
                else:
                    rows = db.execute("SELECT * FROM hq_creditor_transaction_events ORDER BY id DESC LIMIT 100")
                events = []
                for row in rows.fetchall():
                    item = dict(row)
                    item["details"] = json.loads(row["payload"])
                    head = active(db,row["source_id"])
                    item["active"] = bool(head and head["id"] == row["id"])
                    events.append(item)
        except (sqlite3.Error,OSError):
            abort(503)
        return render_template("hq_creditor_transaction_match.html",ref=ref,case_id=case_id,cases=cases,
            evidence_ref=evidence_ref,preview=preview,manifest=stamp,operation=uuid.uuid4().hex,
            events=events,error=error,notice=notice,csrf=session.get("csrf","")),status,{"Cache-Control":"no-store"}
