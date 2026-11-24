"""Owner-only classification proposals on the existing read-only Monzo sheet.

A proposal is not a bank import, verified receipt or change to source amounts.
The current worksheet is reread before every save; source IDs and evidence
fingerprints survive row reordering. No provider or spreadsheet writes.
"""
import hashlib
import json
import re
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests
from flask import abort, current_app, render_template, request, session
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_sheets_live import SPREADSHEET_ID
from hq_finance_splits import categories, parse_form, validate_lines
from hq_work_progress import _actor_fingerprint

SOURCE = "'Monzo Live Feed'!A7:I10000"
HEADER = ["Transaction ID", "Date", "Time", "Type", "Name", "Emoji",
          "Category", "Amount", "Currency"]
ID = re.compile(r"[A-Za-z0-9_:-]{1,160}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
PAGE = 40
LEGACY_SOURCE = "'Monzo Live Feed'!S7:U10000"
LEGACY_HEADER = ["Transaction ID", "HQ category override", "Category source"]


class Conflict(ValueError):
    pass


def fingerprint(parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True,
        separators=(",", ":"), allow_nan=False).encode("ascii")).hexdigest()


def parse_feed(payload):
    """Exact observed layout, numeric Sheets dates and GBP pounds; no inference."""
    if not isinstance(payload, dict) or payload.get("range") != SOURCE:
        raise ValueError("Unexpected Monzo worksheet range.")
    rows = payload.get("values")
    if not isinstance(rows, list) or not rows or rows[0] != HEADER or len(rows) > 9994:
        raise ValueError("Monzo headings or row bounds changed.")
    result = {}
    for row in rows[1:]:
        if not isinstance(row, list) or len(row) > 9:
            raise ValueError("Invalid source row.")
        if all(v is None or v == "" for v in row):
            continue
        if len(row) != 9:
            raise ValueError("Incomplete source row.")
        ref, day, clock, kind, name, _, category, amount, currency = row
        if (not isinstance(ref, str) or not ID.fullmatch(ref)
                or ref in result):
            raise ValueError("Missing or duplicate original transaction ID.")
        if (type(day) is not int or not 36526 <= day <= 90000
                or type(clock) not in (int, float) or not 0 <= clock < 1
                or type(amount) not in (int, float) or currency != "GBP"
                or any(not isinstance(v, str) or len(v) > 500
                       for v in (kind, name))
                or (category is not None and (not isinstance(category, str) or len(category)>500))):
            raise ValueError("Unsupported date, currency, amount or text in source.")
        try:
            value = Decimal(str(amount)) * 100
            if not value.is_finite() or value != value.to_integral_value() or abs(value) > 1000000000:
                raise ValueError("Source amounts must be exact GBP pennies.")
            pence = int(value)
        except InvalidOperation:
            raise ValueError("Invalid source amount.") from None
        date = (datetime(1899, 12, 30) + timedelta(days=day)).date().isoformat()
        evidence = fingerprint([SPREADSHEET_ID, SOURCE, ref, day, clock,
                                kind, name, category, pence, currency])
        result[ref] = dict(ref=ref, day=date, clock=clock, kind=kind,
                           name=name, category=category, pence=pence, evidence=evidence)
    # A populated final row may mean the configured scan truncated the source.
    if len(rows) == 9994 and any(v is not None and v != "" for v in rows[-1]):
        raise ValueError("Monzo worksheet exceeds the bounded review scan.")
    return result


def parse_legacy(payload):
    """Read the independent override table by ID, never by row position."""
    if not isinstance(payload, dict) or payload.get("range") != LEGACY_SOURCE:
        raise ValueError("Unexpected legacy override range.")
    rows = payload.get("values")
    if not isinstance(rows, list) or not rows or rows[0] != LEGACY_HEADER or len(rows) > 9994:
        raise ValueError("Legacy override headings or row bounds changed.")
    result = {}
    for row in rows[1:]:
        if not isinstance(row, list) or len(row) > 3:
            raise ValueError("Invalid legacy override row.")
        if all(v is None or v == "" for v in row):
            continue
        ref, category, source = (row + [None]*3)[:3]
        if (not isinstance(ref, str) or not ID.fullmatch(ref) or ref in result
                or any(v is not None and (not isinstance(v,str) or len(v)>2000)
                       for v in (category,source))):
            raise ValueError("Invalid or duplicate legacy override ID.")
        result[ref] = (category,source)
    if len(rows)==9994 and any(v is not None and v!="" for v in rows[-1]):
        raise ValueError("Legacy overrides exceed the bounded review scan.")
    return result


def attach_legacy(feed, legacy):
    for ref, entry in feed.items():
        category, source = legacy.get(ref,(None,None))
        entry.update(legacy_present=ref in legacy, legacy_category=category,
                     legacy_source=source, legacy_evidence=fingerprint(
                         [SPREADSHEET_ID,LEGACY_SOURCE,ref,ref in legacy,category,source]))
    return feed


def read_feed(token):
    response = requests.get("https://sheets.googleapis.com/v4/spreadsheets/"
        + SPREADSHEET_ID + "/values/" + SOURCE,
        params={"valueRenderOption":"UNFORMATTED_VALUE",
                "dateTimeRenderOption":"SERIAL_NUMBER","majorDimension":"ROWS"},
        headers={"Authorization":"Bearer "+token},timeout=(3,15))
    response.raise_for_status()
    feed = parse_feed(response.json())
    legacy_response = requests.get("https://sheets.googleapis.com/v4/spreadsheets/"
        + SPREADSHEET_ID + "/values/" + LEGACY_SOURCE,
        params={"valueRenderOption":"UNFORMATTED_VALUE","majorDimension":"ROWS"},
        headers={"Authorization":"Bearer "+token},timeout=(3,15))
    legacy_response.raise_for_status()
    return attach_legacy(feed,parse_legacy(legacy_response.json()))


def tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS monzo_sheet_classification_events (
        source_id TEXT NOT NULL, revision INTEGER NOT NULL,
        evidence TEXT NOT NULL, category TEXT NOT NULL,
        operation TEXT NOT NULL UNIQUE, actor TEXT NOT NULL,
        created TEXT NOT NULL, PRIMARY KEY(source_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS monzo_sheet_classification_legacy_evidence (
        source_id TEXT NOT NULL, revision INTEGER NOT NULL, evidence TEXT NOT NULL,
        category TEXT, PRIMARY KEY(source_id,revision))""")

    db.execute("""CREATE TABLE IF NOT EXISTS monzo_sheet_classification_split_lines (
        source_id TEXT NOT NULL, revision INTEGER NOT NULL, position INTEGER NOT NULL,
        category TEXT NOT NULL, pence INTEGER NOT NULL CHECK(pence != 0),
        PRIMARY KEY(source_id,revision,position),
        FOREIGN KEY(source_id,revision) REFERENCES monzo_sheet_classification_events(source_id,revision))""")


def histories(db):
    rows = db.execute("""SELECT e.*, l.evidence AS legacy_evidence, l.category AS legacy_category
        FROM monzo_sheet_classification_events e
        LEFT JOIN monzo_sheet_classification_legacy_evidence l
          ON e.source_id=l.source_id AND e.revision=l.revision
        ORDER BY e.source_id,e.revision DESC""")
    history = {}
    for row in rows:
        events = history.setdefault(row["source_id"], [])
        if len(events) < 10:
            event = dict(row)
            if event["category"] == "split":
                event["lines"] = [dict(line) for line in db.execute(
                    """SELECT category,pence FROM monzo_sheet_classification_split_lines
                    WHERE source_id=? AND revision=? ORDER BY position""",
                    (event["source_id"],event["revision"]))]
            events.append(event)
    return history


def save(entry, evidence, revision, category, actor, legacy_evidence, lines=None):
    if not storage_ready():
        raise ValueError("Persistent HQ storage required.")
    if (not isinstance(evidence, str) or not DIGEST.fullmatch(evidence)
            or not secrets.compare_digest(entry["evidence"], evidence)):
        raise Conflict("Source transaction changed. Reload and review.")
    if (not isinstance(legacy_evidence,str) or not DIGEST.fullmatch(legacy_evidence)
            or not secrets.compare_digest(entry["legacy_evidence"],legacy_evidence)):
        raise Conflict("Existing Sheets classification changed. Reload and review.")
    if (type(revision) is not int or not 0 <= revision < 2**63-1
            or category not in (*categories(), "split")):
        raise ValueError("Invalid review revision or category.")
    if category == "split":
        lines = validate_lines(lines, entry["pence"])
        if entry["pence"] < 0 and any(line["category"] == "customer_income" for line in lines):
            raise ValueError("Customer income candidates require a positive amount.")
    elif lines is not None:
        raise ValueError("Split lines require a split classification.")
    if category == "customer_income" and entry["pence"] <= 0:
        raise ValueError("Customer income candidates require a positive amount.")
    actor = _actor_fingerprint(actor)
    ref = entry["ref"]
    operation = fingerprint([ref, evidence, revision, category, actor, legacy_evidence])
    if category == "split":
        operation = fingerprint([operation, lines])
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        tables(db)
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT revision FROM monzo_sheet_classification_events WHERE operation=?", (operation,)).fetchone()
        if old:
            return old["revision"]
        head = db.execute("""SELECT * FROM monzo_sheet_classification_events
            WHERE source_id=? ORDER BY revision DESC LIMIT 1""",(ref,)).fetchone()
        if (head["revision"] if head else 0) != revision:
            raise Conflict("Another review was saved. Reload before editing.")
        if head and head["evidence"] != evidence:
            raise Conflict("Source differs from saved evidence; separate investigation required.")
        db.execute("INSERT INTO monzo_sheet_classification_events VALUES(?,?,?,?,?,?,?)",
            (ref,revision+1,evidence,category,operation,actor,datetime.now(timezone.utc).isoformat()))
        db.execute("INSERT INTO monzo_sheet_classification_legacy_evidence VALUES(?,?,?,?)",
            (ref,revision+1,legacy_evidence,entry["legacy_category"]))
        if category == "split":
            db.executemany("INSERT INTO monzo_sheet_classification_split_lines VALUES(?,?,?,?,?)",
                [(ref,revision+1,i,line["category"],line["pence"]) for i,line in enumerate(lines)])
        return revision+1


def register(bp):
    @bp.route("/hq/finance/monzo-classifications", methods=["GET","POST"])
    @required
    def monzo_classifications():
        if not storage_ready():
            abort(503)
        entries, error, notice, status, offset, count = None, None, None, 200, 0, 0
        if request.method == "POST":
            request.max_content_length = 8192
            csrf = session.get("csrf")
            if not csrf or not secrets.compare_digest(request.form.get("csrf",""),csrf):
                abort(403)
            action = request.form.get("action")
            try:
                raw_offset = request.form.get("offset","0")
                if not re.fullmatch(r"[0-9]{1,5}",raw_offset) or int(raw_offset)>10000:
                    raise ValueError("Invalid page.")
                offset = int(raw_offset)
                if action not in ("load","save","split"):
                    raise ValueError("Invalid action.")
                if action in ("save","split"):
                    ref = request.form.get("ref","")
                    revision = request.form.get("revision","")
                    evidence = request.form.get("evidence","")
                    category = "split" if action == "split" else request.form.get("category","")
                    legacy_evidence = request.form.get("legacy_evidence","")
                    if (not ID.fullmatch(ref) or not re.fullmatch(r"[0-9]{1,19}",revision)
                            or not DIGEST.fullmatch(evidence) or not DIGEST.fullmatch(legacy_evidence) or (action == "save" and category not in categories())
                            or request.form.get("confirm") != "yes"):
                        raise ValueError("Explicit classification confirmation required.")
                provider = current_app.extensions["hq_sheets_connection"]()
                if not provider or provider[1] <= time.time():
                    error,status = "Connect Google Sheets with read-only permission first.",401
                else:
                    feed = read_feed(provider[0])
                    if action in ("save","split"):
                        if ref not in feed:
                            raise Conflict("Original transaction not found in current source.")
                        save(feed[ref],evidence,int(revision),category,session.get("google_sub",""),legacy_evidence,
                             parse_form(request.form) if action == "split" else None)
                        notice = "Classification proposal saved. Source and financial balances unchanged."
                    with closing(connect()) as db, db:
                        tables(db)
                        history = histories(db)
                    ordered = sorted(feed.values(),key=lambda e:(e["day"],e["clock"],e["ref"]),reverse=True)
                    count = len(ordered)
                    entries = ordered[offset:offset+PAGE]
                    for entry in entries:
                        events = history.get(entry["ref"],[])
                        entry["history"] = events
                        entry["revision"] = events[0]["revision"] if events else 0
                        entry["stale"] = bool(events and events[0]["evidence"] != entry["evidence"])
                        entry["proposal"] = events[0]["category"] if events else "unclassified"
                        entry["split_lines"] = events[0].get("lines",[]) if events else []
                        entry["legacy_changed"] = bool(events and
                            events[0]["legacy_evidence"] != entry["legacy_evidence"])
            except Conflict as exc:
                error,status=str(exc),409
            except (ValueError,TypeError):
                error,status="Review withheld: check source schema, GBP units, unique IDs and form values.",400
            except (requests.RequestException,sqlite3.Error,OSError,KeyError):
                error,status="Read-only source or HQ storage unavailable; no new save confirmed.",503
        return render_template("finance_monzo_classifications.html",entries=entries,error=error,
            notice=notice,csrf=session.get("csrf",""),categories=categories(),offset=offset,
            count=count,page=PAGE),status,{"Cache-Control":"no-store"}
