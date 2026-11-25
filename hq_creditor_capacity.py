"""Owner-entered affordability scenarios; no bank or creditor writes.

Cash Engine B10:E11/H11 and Creditor Affordability B15/E16 formula subset.
Inputs are monthly GBP observations, not reconciled bank-derived amounts.
"""
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_FLOOR
import json
import re
import secrets
import sqlite3
import uuid

from flask import abort, render_template, request, session, url_for
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_creditor_cases import _amount, _tables, _record, ID
from hq_work_progress import _actor_fingerprint

MONEY = {
    "trading": "Genuine monthly trading receipts",
    "opex": "Essential monthly operating expenditure",
    "growth": "Monthly growth / stock / capex",
    "observed_debt": "Observed monthly debt outflow",
    "registered_debt": "All registered monthly commitments (including outside HQ)",
    "director_funding": "Monthly director funding received",
    "director_allowance": "Monthly director sustainability allowance",
    "reserve_target": "Minimum cash reserve",
    "cash": "Reported cash available",
    "reserve_min": "Minimum unallocated creditor contingency",
}
RATES = {"reserve_rate": "Creditor contingency (%)", "safe_rate": "Safe capacity factor (%)",
         "max_rate": "Temporary maximum factor (%)"}


def _fields(form):
    data = {}
    for key in MONEY:
        value = _amount(form.get(key, ""))
        if value is None:
            raise ValueError("All scenario amounts are required; enter zero only when known.")
        data[key] = value
    for key in RATES:
        value = _amount(form.get(key, ""))
        if value is None or not 0 <= value <= 10000:
            raise ValueError("Percentage must be between 0 and 100.")
        data[key] = value
    if data["safe_rate"] > data["max_rate"]:
        raise ValueError("Safe factor must not exceed temporary maximum.")
    data["mode"] = form.get("mode", "")
    if data["mode"] not in ("conservative", "exclude_growth"):
        raise ValueError("Choose the growth treatment.")
    raw = form.get("as_of", "")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw) or date.fromisoformat(raw) > date.today():
        raise ValueError("Enter a valid non-future evidence date.")
    data["as_of"] = raw
    return data


def calculate(d):
    # Integer pence inputs, Decimal intermediate arithmetic. Floor capacity
    # only at display boundary, never round an affordable offer upwards.
    growth = d["growth"] if d["mode"] == "conservative" else 0
    used = max(d["observed_debt"], d["registered_debt"])
    rebuild = max(Decimal(0), Decimal(d["reserve_target"] - d["cash"]) / 6)
    free = max(Decimal(0), d["trading"] - d["opex"] - growth - used
               - d["director_allowance"] - rebuild)
    reserve = min(free, max(Decimal(d["reserve_min"]), free * d["reserve_rate"] / 10000))
    critical = d["director_funding"] > 0 and d["trading"] - d["opex"] - d["observed_debt"] - growth < 0
    pool = 0 if critical else free - reserve
    floor = lambda v: int(Decimal(v).to_integral_value(rounding=ROUND_FLOOR))
    return dict(debt_used=used, rebuild=floor(rebuild), free=floor(free),
                reserve=floor(reserve), critical=critical,
                safe=floor(Decimal(pool) * d["safe_rate"] / 10000),
                maximum=floor(Decimal(pool) * d["max_rate"] / 10000))


def case_summary(records):
    result = dict(included=0, outside=0, archived=0, unresolved=0,
                  committed_known=0, incremental_known=0, incomplete=0)
    for case in records:
        if case["archived"]:
            result["archived"] += 1
        elif case["scope"] == "personal" or case["inclusion"] == "exclude":
            result["outside"] += 1
        elif case["scope"] != "business" or case["inclusion"] != "include":
            result["unresolved"] += 1
        else:
            result["included"] += 1
            committed, proposed = case["committed_monthly"], case["proposed_monthly"]
            if committed is not None:
                result["committed_known"] += committed
            if committed is None or proposed is None:
                result["incomplete"] += 1
            else:
                result["incremental_known"] += max(0, proposed - committed)
    return result


def _scenario_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_creditor_capacity_scenarios (
        revision INTEGER PRIMARY KEY, operation TEXT UNIQUE NOT NULL,
        actor TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL)""")


def save(form, owner):
    if not storage_ready():
        raise RuntimeError("Persistent storage required.")
    op, rev = form.get("operation", ""), form.get("revision", "")
    if not ID.fullmatch(op) or not re.fullmatch(r"[0-9]{1,18}", rev):
        raise ValueError("Invalid revision or operation.")
    rev = int(rev)
    state = json.dumps(_fields(form), sort_keys=True)
    actor = _actor_fingerprint(owner)
    with closing(connect()) as db, db:
        _scenario_table(db)
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT * FROM hq_creditor_capacity_scenarios WHERE operation=?", (op,)).fetchone()
        if previous:
            if (previous["revision"], previous["state"], previous["actor"]) != (rev+1, state, actor):
                raise ValueError("Operation already used with different details.")
            return
        latest = db.execute("SELECT COALESCE(MAX(revision),0) FROM hq_creditor_capacity_scenarios").fetchone()[0]
        if latest != rev:
            raise ValueError("Scenario changed elsewhere. Reload before saving.")
        db.execute("INSERT INTO hq_creditor_capacity_scenarios VALUES(?,?,?,?,?)",
                   (rev+1, op, actor, state, datetime.now(timezone.utc).isoformat()))


def snapshot():
    if not storage_ready():
        raise RuntimeError("Persistent storage required.")
    with closing(connect()) as db, db:
        _tables(db)
        _scenario_table(db)
        db.execute("BEGIN")
        rows = db.execute("SELECT * FROM hq_creditor_capacity_scenarios ORDER BY revision DESC LIMIT 20").fetchall()
        history = [dict(revision=r["revision"], state=json.loads(r["state"]), created_at=r["created_at"]) for r in rows]
        # All native cases, not the truncated creditor-list view.
        summary = case_summary(_record(r) for r in db.execute("SELECT * FROM hq_creditor_cases"))
        return history, summary


def register(bp):
    @bp.route("/hq/finance/creditor-capacity", methods=["GET", "POST"])
    @required
    def creditor_capacity():
        error, code = None, 200
        if request.method == "POST":
            if request.content_length is None or request.content_length > 8192:
                abort(413)
            csrf = session.get("csrf")
            if not csrf or not secrets.compare_digest(request.form.get("csrf", ""), csrf):
                abort(403)
            if request.form.get("confirm") != "yes":
                error, code = "Confirm these are unverified planning inputs.", 400
            else:
                try:
                    save(request.form, session.get("google_sub"))
                    return "", 303, {"Location": url_for(".creditor_capacity"), "Cache-Control": "no-store"}
                except ValueError as exc:
                    error, code = str(exc), 409
                except (RuntimeError, sqlite3.Error, OSError):
                    error, code = "Persistent storage unavailable; save not confirmed.", 503
        try:
            history, summary = snapshot()
        except (RuntimeError, sqlite3.Error, OSError):
            return "Persistent finance storage unavailable.", 503, {"Cache-Control": "no-store"}
        latest = history[0] if history else None
        data = latest["state"] if latest else {}
        result = calculate(data) if latest else None
        # Current register comparison is not historical or complete evidence.
        warning = bool(latest and (summary["committed_known"] > data["registered_debt"]
                                  or summary["unresolved"] or summary["incomplete"]
                                  or summary["archived"]))
        return render_template("hq_creditor_capacity.html", money=MONEY, rates=RATES, data=data,
                               revision=latest["revision"] if latest else 0, history=history,
                               result=result, summary=summary, warning=warning, error=error,
                               csrf=session.get("csrf", ""), operation=uuid.uuid4().hex), code, {"Cache-Control":"no-store"}
