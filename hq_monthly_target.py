"""Native month-to-date planning; manual evidence is never verified bank cash."""
import calendar
from contextlib import closing
from datetime import datetime
import sqlite3
from zoneinfo import ZoneInfo

from flask import abort, render_template
from customer_request_store import connect, storage_ready
from hq_auth import required


def current_month_report(today):
    from hq_native_finance import schema
    from hq_finance_splits import tables, classification_period
    start = today.replace(day=1).isoformat()
    end = today.replace(day=calendar.monthrange(today.year, today.month)[1]).isoformat()
    days = calendar.monthrange(today.year, today.month)[1] - today.day + 1
    with closing(connect()) as db, db:
        schema(db)
        tables(db)
        db.commit()
        db.execute("BEGIN")
        plan = db.execute("SELECT target,revision FROM finance_plan WHERE id=1").fetchone()
        target = plan["target"] if plan else None
        if target is not None and (type(target) is not int or target < 0):
            raise ValueError("Invalid planning target.")
        ledger = classification_period(db, start, today.isoformat())
        bills = db.execute("SELECT amount,due,status,archived FROM finance_bills WHERE due<=?",
                           (end,)).fetchall()
    incoming = refunds = pending = ambiguous = included = 0
    for entry in ledger["entries"]:
        # A saved split is an explicit classification overlay; never add the parent.
        if not entry["reviewed"] and not entry["split"]:
            pending += 1
            continue
        lines = entry["lines"]
        if any(line["category"] in ("unclassified", "refund", "settlement")
               or (line["category"] == "customer_income" and line["pence"] <= 0)
               for line in lines):
            ambiguous += 1
            continue
        for line in lines:
            cat, amount = line["category"], line["pence"]
            if cat == "customer_income":
                incoming += amount
                included += 1
            elif cat == "customer_refund":
                if amount >= 0:
                    raise ValueError("Invalid customer refund direction.")
                refunds -= amount
                included += 1
    net = incoming - refunds if included else None
    # This is a scenario against the saved target, not actual outstanding income.
    gap = max(target-net, 0) if target is not None and net is not None and not (pending or ambiguous) else None
    daily = (gap + days - 1)//days if gap is not None else None
    planned_month = overdue = reported_paid = 0
    month_count = overdue_count = paid_count = 0
    for bill in bills:
        if type(bill["amount"]) is not int or bill["amount"] < 0:
            raise ValueError("Invalid commitment amount.")
        if bill["archived"]:
            continue
        if bill["status"] == "planned":
            if bill["due"] < start:
                overdue += bill["amount"]
                overdue_count += 1
            else:
                planned_month += bill["amount"]
                month_count += 1
        elif bill["status"] == "reported_paid" and bill["due"] >= start:
            reported_paid += bill["amount"]
            paid_count += 1
    return dict(start=start, through=today.isoformat(), end=end, days=days,
                target=target, revision=plan["revision"] if plan else None,
                incoming=incoming if included else None, refunds=refunds if included else None,
                net=net, gap=gap, daily=daily, pending=pending, ambiguous=ambiguous,
                entries=len(ledger["entries"]), included=included,
                planned_month=planned_month if month_count else None,
                overdue=overdue if overdue_count else None,
                reported_paid=reported_paid if paid_count else None,
                bank_verified=False)


def register(bp):
    @bp.get("/hq/finance/monthly-target")
    @required
    def monthly_target():
        if not storage_ready():
            abort(503)
        try:
            report = current_month_report(datetime.now(ZoneInfo("Europe/London")).date())
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
            return render_template("finance_monthly_target.html", report=None), 503, {"Cache-Control":"no-store"}
        return render_template("finance_monthly_target.html", report=report), 200, {"Cache-Control":"no-store"}
