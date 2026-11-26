"""Owner-entered affordability scenarios; no bank or creditor writes.

Cash Engine B10:E11/H11 and Creditor Affordability B15/E16 formula subset.
Inputs are monthly GBP observations, not reconciled bank-derived amounts.
"""
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
import json
import hashlib
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
OPTIONAL_MONEY = {
    "director_drawings": "Observed monthly director remuneration / drawings",
    "requested_monthly": "Additional monthly creditor payment requested",
}
RATES = {"reserve_rate": "Creditor contingency (%)", "safe_rate": "Safe capacity factor (%)",
         "max_rate": "Temporary maximum factor (%)"}

PERIOD_MONEY = {
    "trading": "Trading receipts",
    "opex": "Essential operating expenditure",
    "growth": "Growth / stock / capex",
    "observed_debt": "Observed debt outflow",
    "director_funding": "Director funding received",
}
PERIOD_OPTIONAL = {"director_drawings": "Director remuneration / drawings"}


def _evidence_date(raw):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
        raise ValueError("Enter a valid evidence date.")
    value = date.fromisoformat(raw)
    if value > date.today():
        raise ValueError("Evidence dates cannot be in the future.")
    return value


def _period_fields(form, end):
    """Cash Engine B7/E7/H7/B8/H8/B9: explicit inclusive observation window.

    Inputs are owner-reviewed totals, not an automatically complete bank
    transaction feed. Round monthly estimates once to the nearest penny;
    the existing shared affordability model still floors capacity.
    """
    start = _evidence_date(form.get("period_start", ""))
    days = (end - start).days + 1
    if not 1 <= days <= 366:
        raise ValueError("Observation period must contain 1 to 366 inclusive days.")
    totals, monthly = {}, {}
    for key in (*PERIOD_MONEY, *PERIOD_OPTIONAL):
        value = _amount(form.get("period_" + key, ""))
        if value is None:
            if key in PERIOD_MONEY:
                raise ValueError("All required period totals must be entered; use zero only when known.")
            continue
        totals[key] = value
        monthly[key] = int((Decimal(value) * Decimal("30.4375") / days).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP))
    return dict(start=start.isoformat(), end=end.isoformat(), days=days, totals=totals), monthly


# Conservative explicit mapping only. Ambiguous categories and wrong
# directions remain excluded; no inference from descriptions or bank data.
NATIVE_PERIOD_MAP = {
    "customer_income": ("trading", 1),
    "fuel": ("opex", -1), "insurance": ("opex", -1),
    "rent_utilities": ("opex", -1), "subscriptions": ("opex", -1),
    "materials": ("growth", -1), "equipment": ("growth", -1),
    "creditor_payment": ("observed_debt", -1),
    "director_drawings": ("director_drawings", -1),
    "director_funding": ("director_funding", 1),
    "loan_repayment": ("observed_debt", -1),
}


def native_period_review(db, start, end):
    from hq_finance_splits import classification_period
    first, last = _evidence_date(start), _evidence_date(end)
    if not 1 <= (last-first).days+1 <= 366:
        raise ValueError("Observation period must contain 1 to 366 inclusive days.")
    report = classification_period(db, start, end, include_revisions=True)
    mapped, excluded = {}, {}
    evidence = []
    for row in report["entries"]:
        evidence.append({key:row[key] for key in
                         ("id","day","revision","amount","reviewed","lines")})
        for line in row["lines"]:
            cat, amount = line["category"], line["pence"]
            rule = NATIVE_PERIOD_MAP.get(cat)
            if row["reviewed"] and rule and amount*rule[1]>0:
                key = rule[0]
                mapped[key] = mapped.get(key,0)+abs(amount)
            else:
                reason = "unreviewed" if not row["reviewed"] else cat
                bucket = excluded.setdefault(reason, dict(count=0, incoming=0, outgoing=0))
                bucket["count"] += 1
                bucket["incoming"] += max(amount,0)
                bucket["outgoing"] += max(-amount,0)
    summary = dict(start=start,end=end,rows=len(evidence),mapped=mapped,
                   excluded=excluded,archived=report["archived_count"],
                   mapping_version=2,bank_verified=False)
    summary["digest"] = hashlib.sha256(json.dumps(
        [summary,evidence],sort_keys=True,separators=(",",":")).encode()).hexdigest()
    return summary


def preview_native_period(start, end):
    from hq_native_finance import schema
    from hq_finance_splits import tables
    with closing(connect()) as db, db:
        schema(db)
        tables(db)
        db.commit()
        db.execute("BEGIN")
        return native_period_review(db,start,end)


def _fields(form):
    data = {}
    raw = form.get("as_of", "")
    end = _evidence_date(raw)
    basis = form.get("input_basis", "monthly")
    if basis not in ("monthly", "period"):
        raise ValueError("Choose monthly observations or period totals.")
    if basis == "period":
        data["period"], monthly = _period_fields(form, end)
        data.update(monthly)
    for key in MONEY:
        if basis == "period" and key in PERIOD_MONEY:
            continue
        value = _amount(form.get(key, ""))
        if value is None:
            raise ValueError("All scenario amounts are required; enter zero only when known.")
        data[key] = value
    # Blank is unknown, not zero. Omitting absent fields also preserves
    # idempotency for saved scenarios from before this additive extension.
    for key in OPTIONAL_MONEY:
        if basis == "period" and key in PERIOD_OPTIONAL:
            continue
        value = _amount(form.get(key, ""))
        if value is not None:
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


def business_affordability(d):
    """Business Affordability H12/H18/H19/H25/H27/H29, planning inputs only.

    H19 includes growth regardless of the selected capacity model's mode.
    Drawings and director funding are disclosed separately; neither changes
    assessable trading income. H27 uses the conservative rounded-down
    existing safe-capacity calculation, not a live or verified bank value.
    """
    outgoings = d["opex"] + d["growth"] + d["observed_debt"]
    safe = calculate(d)["safe"]
    requested = d.get("requested_monthly")
    return dict(trading=d["trading"], director_funding=d["director_funding"],
                outgoings=outgoings, net_self_funded=d["trading"]-outgoings,
                director_drawings=d.get("director_drawings"),
                reserve_shortfall=max(d["reserve_target"]-d["cash"], 0),
                safe_capacity=safe, requested_monthly=requested,
                requested_excess=None if requested is None else max(requested-safe, 0),
                request_within_capacity=None if requested is None else requested<=safe)


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
    data = _fields(form)
    digest = form.get("native_review_digest", "")
    if digest and (not re.fullmatch(r"[0-9a-f]{64}",digest) or "period" not in data
                   or form.get("native_review_confirm") != "yes"):
        raise ValueError("Confirm the partial native transaction review in period mode.")
    actor = _actor_fingerprint(owner)
    with closing(connect()) as db, db:
        _scenario_table(db)
        if digest:
            from hq_native_finance import schema
            from hq_finance_splits import tables
            schema(db)
            tables(db)
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT * FROM hq_creditor_capacity_scenarios WHERE operation=?", (op,)).fetchone()
        if digest:
            if previous:
                # Exact replay uses retained provenance, never today's source.
                review = json.loads(previous["state"]).get("native_review")
                if not review or review["digest"] != digest:
                    raise ValueError("Operation already used with different review.")
            else:
                review = native_period_review(db,data["period"]["start"],data["period"]["end"])
                if review["digest"] != digest:
                    raise ValueError("Native transactions changed. Prepare the review again.")
            data["native_review"] = review
        state = json.dumps(data, sort_keys=True)
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
        review = None
        if request.method == "GET" and ("review_start" in request.args or "review_end" in request.args):
            try:
                review = preview_native_period(request.args.get("review_start",""),request.args.get("review_end",""))
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                error, code = str(exc), 400
            except (sqlite3.Error, OSError):
                error, code = "Native transaction review unavailable.", 503
        return render_template("hq_creditor_capacity.html", native_review=review, money=MONEY, rates=RATES, data=data,
                               revision=latest["revision"] if latest else 0, history=history,
                               result=result, affordability=business_affordability(data) if latest else None,
                               optional_money=OPTIONAL_MONEY, period_money=PERIOD_MONEY, period_optional=PERIOD_OPTIONAL,
                               summary=summary, warning=warning, error=error,
                               csrf=session.get("csrf", ""), operation=uuid.uuid4().hex), code, {"Cache-Control":"no-store"}
