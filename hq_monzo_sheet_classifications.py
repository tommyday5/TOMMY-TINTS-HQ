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
from hq_finance_splits import categories
from hq_work_progress import _actor_fingerprint

SOURCE = "'Monzo Live Feed'!A7:I10000"
HEADER = ["Transaction ID", "Date", "Time", "Type", "Name", "Emoji",
          "Category", "Amount", "Currency"]
ID = re.compile(r"[A-Za-z0-9_:-]{1,160}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
PAGE = 40


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
                       for v in (kind, name, category))):
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


def read_feed(token):
    response = requests.get("https://sheets.googleapis.com/v4/spreadsheets/"
        + SPREADSHEET_ID + "/values/" + SOURCE,
        params={"valueRenderOption":"UNFORMATTED_VALUE",
                "dateTimeRenderOption":"SERIAL_NUMBER","majorDimension":"ROWS"},
        headers={"Authorization":"Bearer "+token},timeout=(3,15))
    response.raise_for_status()
    return parse_feed(response.json())


def tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS monzo_sheet_classification_events (
        source_id TEXT NOT NULL, revision INTEGER NOT NULL,
        evidence TEXT NOT NULL, category TEXT NOT NULL,
        operation TEXT NOT NULL UNIQUE, actor TEXT NOT NULL,
        created TEXT NOT NULL, PRIMARY KEY(source_id,revision))""")


def histories(db):
    rows = db.execute("""SELECT * FROM monzo_sheet_classification_events
        ORDER BY source_id,revision DESC""")
    history = {}
    for row in rows:
        events = history.setdefault(row["source_id"], [])
        if len(events) < 10:
            events.append(dict(row))
    return history


def save(entry, evidence, revision, category, actor):
    if not storage_ready():
        raise ValueError("Persistent HQ storage required.")
    if (not isinstance(evidence, str) or not DIGEST.fullmatch(evidence)
            or not secrets.compare_digest(entry["evidence"], evidence)):
        raise Conflict("Source transaction changed. Reload and review.")
    if (type(revision) is not int or not 0 <= revision < 2**63-1
            or category not in categories()):
        raise ValueError("Invalid review revision or category.")
    if category == "customer_income" and entry["pence"] <= 0:
        raise ValueError("Customer income candidates require a positive amount.")
    actor = _actor_fingerprint(actor)
    ref = entry["ref"]
    operation = fingerprint([ref, evidence, revision, category, actor])
    with closing(connect()) as db, db:
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
        return revision+1


def register(bp):
    @bp.route("/hq/finance/monzo-classifications", methods=["GET","POST"])
    @required
    def monzo_classifications():
        if not storage_ready():
            abort(503)
        entries, error, notice, status, offset, count = None, None, None, 200, 0, 0
        if request.method == "POST":
            request.max_content_length = 4096
            csrf = session.get("csrf")
            if not csrf or not secrets.compare_digest(request.form.get("csrf",""),csrf):
                abort(403)
            action = request.form.get("action")
            try:
                raw_offset = request.form.get("offset","0")
                if not re.fullmatch(r"[0-9]{1,5}",raw_offset) or int(raw_offset)>10000:
                    raise ValueError("Invalid page.")
                offset = int(raw_offset)
                if action not in ("load","save"):
                    raise ValueError("Invalid action.")
                if action == "save":
                    ref = request.form.get("ref","")
                    revision = request.form.get("revision","")
                    evidence = request.form.get("evidence","")
                    category = request.form.get("category","")
                    if (not ID.fullmatch(ref) or not re.fullmatch(r"[0-9]{1,19}",revision)
                            or not DIGEST.fullmatch(evidence) or category not in categories()
                            or request.form.get("confirm") != "yes"):
                        raise ValueError("Explicit classification confirmation required.")
                provider = current_app.extensions["hq_sheets_connection"]()
                if not provider or provider[1] <= time.time():
                    error,status = "Connect Google Sheets with read-only permission first.",401
                else:
                    feed = read_feed(provider[0])
                    if action == "save":
                        if ref not in feed:
                            raise Conflict("Original transaction not found in current source.")
                        save(feed[ref],evidence,int(revision),category,session.get("google_sub",""))
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
            except Conflict as exc:
                error,status=str(exc),409
            except (ValueError,TypeError):
                error,status="Review withheld: check source schema, GBP units, unique IDs and form values.",400
            except (requests.RequestException,sqlite3.Error,OSError,KeyError):
                error,status="Read-only source or HQ storage unavailable; no new save confirmed.",503
        return render_template("finance_monzo_classifications.html",entries=entries,error=error,
            notice=notice,csrf=session.get("csrf",""),categories=categories(),offset=offset,
            count=count,page=PAGE),status,{"Cache-Control":"no-store"}
