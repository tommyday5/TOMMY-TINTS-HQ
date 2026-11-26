"""Owner-reviewed monthly commitment schedules; never an automatic payment."""
import calendar
from contextlib import closing
from datetime import datetime, timezone
import json
import hashlib
import re
import secrets
import sqlite3
import uuid

from flask import abort, redirect, render_template, request, session, url_for
from customer_request_store import connect, storage_ready
from hq_auth import required

HEX = re.compile(r"[0-9a-f]{32}\Z")
MONTH = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])\Z")


class Conflict(ValueError):
    pass


def tables(db):
    from hq_native_finance import schema
    schema(db)
    db.execute("""CREATE TABLE IF NOT EXISTS finance_monthly_schedules (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, amount INTEGER NOT NULL,
        due_day INTEGER NOT NULL CHECK(due_day BETWEEN 1 AND 31),
        archived INTEGER NOT NULL CHECK(archived IN (0,1)), revision INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS finance_monthly_schedule_events (
        schedule_id TEXT NOT NULL, revision INTEGER NOT NULL, snapshot TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(schedule_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS finance_monthly_occurrences (
        schedule_id TEXT NOT NULL, month TEXT NOT NULL, schedule_revision INTEGER NOT NULL,
        bill_id TEXT NOT NULL UNIQUE, snapshot TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(schedule_id,month))""")


def stamp():
    return datetime.now(timezone.utc).isoformat()


def identifier(raw):
    if not isinstance(raw,str) or not HEX.fullmatch(raw):
        raise ValueError("Invalid schedule ID.")
    return raw


def revision(raw):
    if not isinstance(raw,str) or not re.fullmatch(r"[0-9]{1,18}",raw):
        raise ValueError("Invalid revision.")
    return int(raw)


def pence(raw):
    if not isinstance(raw,str) or not re.fullmatch(r"[0-9]{1,8}(\.[0-9]{1,2})?",raw):
        raise ValueError("Enter a positive amount in pounds and pence.")
    whole, _, fractional = raw.partition(".")
    amount = int(whole)*100 + int(fractional.ljust(2,"0"))
    if not 1 <= amount <= 1000000000:
        raise ValueError("Amount outside permitted range.")
    return amount


def due_date(month, due_day):
    if not isinstance(month,str) or not MONTH.fullmatch(month) or month[:4] == "0000":
        raise ValueError("Choose a valid calendar month.")
    year, number = map(int,month.split("-"))
    return f"{month}-{min(due_day,calendar.monthrange(year,number)[1]):02d}"


def normalized(name):
    return " ".join(name.split()).casefold()


def save_schedule(db,form):
    from hq_native_finance import text, audit
    ref = identifier(form.get("id"))
    rev = revision(form.get("revision"))
    name = text(form.get("name"))
    amount = pence(form.get("amount"))
    raw_day = form.get("due_day","")
    if not re.fullmatch(r"[0-9]{1,2}",raw_day) or not 1 <= int(raw_day) <= 31:
        raise ValueError("Due day must be between 1 and 31.")
    archive = form.get("archived","0")
    if archive not in ("0","1"):
        raise ValueError("Choose active or archived.")
    row = db.execute("SELECT * FROM finance_monthly_schedules WHERE id=?",(ref,)).fetchone()
    if (row and row["revision"] != rev) or (not row and rev != 0):
        raise Conflict("Schedule changed elsewhere. Reload before saving.")
    if not row and archive == "1":
        raise ValueError("New schedules must be active.")
    if archive == "0":
        for other in db.execute("SELECT id,name FROM finance_monthly_schedules WHERE archived=0 AND id<>?",(ref,)):
            if normalized(other["name"]) == normalized(name):
                raise Conflict("An active schedule with this name already exists.")
    data = dict(id=ref,name=name,amount=amount,due_day=int(raw_day),archived=int(archive),revision=rev+1)
    db.execute("""INSERT INTO finance_monthly_schedules VALUES(?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET name=excluded.name,amount=excluded.amount,
        due_day=excluded.due_day,archived=excluded.archived,revision=excluded.revision""",
        tuple(data[k] for k in ("id","name","amount","due_day","archived","revision")))
    db.execute("INSERT INTO finance_monthly_schedule_events VALUES(?,?,?,?)",
        (ref,rev+1,json.dumps(data,sort_keys=True),stamp()))
    audit(db,"monthly_schedule",ref,rev+1)


def preview(db,form):
    ref = identifier(form.get("id"))
    rev = revision(form.get("revision"))
    month = form.get("month")
    row = db.execute("SELECT * FROM finance_monthly_schedules WHERE id=?",(ref,)).fetchone()
    if not row or row["revision"] != rev or row["archived"]:
        raise Conflict("Schedule unavailable or changed. Reload before generating.")
    due = due_date(month,row["due_day"])
    # The original occurrence remains reserved even if the native bill is edited
    # or archived. No replay can recreate a cancelled/archived obligation.
    if db.execute("SELECT 1 FROM finance_monthly_occurrences WHERE schedule_id=? AND month=?",(ref,month)).fetchone():
        raise Conflict("A bill has already been generated for this schedule and month.")
    # Refuse a same-name bill in that month, including archived or cancelled ones.
    # This is a conservative duplicate check, not historical migration matching.
    for bill in db.execute("SELECT name FROM finance_bills WHERE due BETWEEN ? AND ?",(month+"-01",month+"-31")):
        if normalized(bill["name"]) == normalized(row["name"]):
            raise Conflict("A same-name native bill already exists in this month. Review it instead.")
    return dict(schedule_id=ref,schedule_revision=rev,month=month,name=row["name"],
                amount=row["amount"],due=due)


def generate(db,form):
    from hq_native_finance import audit
    proposal = preview(db,form)
    if form.get("approved") != "yes":
        raise ValueError("Confirm the exact planned bill first.")
    # Compare the shown amount and date as well as the revision.
    if form.get("due") != proposal["due"] or form.get("amount_pence") != str(proposal["amount"]):
        raise Conflict("Preview changed. Review again.")
    ref = uuid.uuid4().hex
    data = dict(name=proposal["name"],amount=proposal["amount"],due=proposal["due"],
                status="planned",status_date=None,archived=False)
    db.execute("""INSERT INTO finance_bills
        (id,name,amount,due,revision,status,status_date,archived) VALUES(?,?,?,?,1,'planned',NULL,0)""",
        (ref,data["name"],data["amount"],data["due"]))
    db.execute("INSERT INTO finance_bill_history VALUES(?,?,?,?,?)",
        (uuid.uuid4().hex,ref,1,json.dumps(data,sort_keys=True),stamp()))
    db.execute("INSERT INTO finance_monthly_occurrences VALUES(?,?,?,?,?,?)",
        (proposal["schedule_id"],proposal["month"],proposal["schedule_revision"],ref,
         json.dumps(proposal,sort_keys=True),stamp()))
    audit(db,"bill",ref,1)



def batch_preview(db, month):
    # Validate even when there are no schedules.
    due_date(month, 1)
    rows = db.execute("""SELECT * FROM finance_monthly_schedules
        WHERE archived=0 ORDER BY id LIMIT 26""").fetchall()
    if len(rows) > 25:
        raise ValueError("More than 25 active schedules; use individual previews.")
    eligible, blocked = [], []
    for row in rows:
        try:
            eligible.append(preview(db, dict(id=row["id"],
                revision=str(row["revision"]), month=month)))
        except Conflict as exc:
            blocked.append(dict(schedule_id=row["id"], revision=row["revision"],
                name=row["name"], reason=str(exc)))
    manifest = dict(month=month, eligible=eligible, blocked=blocked,
                    total=sum(p["amount"] for p in eligible))
    manifest["digest"] = hashlib.sha256(json.dumps(manifest,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return manifest


def generate_batch(db, form):
    manifest = batch_preview(db, form.get("month"))
    if form.get("approved") != "yes":
        raise ValueError("Confirm the exact batch first.")
    if not secrets.compare_digest(form.get("digest", ""), manifest["digest"]):
        raise Conflict("Monthly batch changed. Preview again before approving.")
    if not manifest["eligible"]:
        raise Conflict("No eligible monthly commitments remain.")
    # Caller holds BEGIN IMMEDIATE; all bills and provenance commit or roll
    # back together. Reuse the single-item duplicate and revision checks.
    for p in manifest["eligible"]:
        generate(db, dict(id=p["schedule_id"], revision=str(p["schedule_revision"]),
            month=p["month"], due=p["due"], amount_pence=str(p["amount"]),
            approved="yes"))


def register(bp):
    @bp.route("/hq/finance/monthly-commitments",methods=["GET","POST"])
    @required
    def monthly_commitments():
        if not storage_ready():
            abort(503)
        error, status, proposal, batch = None, 200, None, None
        if request.method == "POST":
            request.max_content_length = 4096
            token = session.get("csrf")
            if not token or not secrets.compare_digest(request.form.get("csrf",""),token):
                abort(403)
            try:
                with closing(connect()) as db, db:
                    tables(db)
                    db.execute("BEGIN IMMEDIATE")
                    action = request.form.get("action")
                    if action == "save":
                        save_schedule(db,request.form)
                    elif action == "preview":
                        proposal = preview(db,request.form)
                    elif action == "generate":
                        generate(db,request.form)
                    elif action == "batch_preview":
                        batch = batch_preview(db, request.form.get("month"))
                    elif action == "batch_generate":
                        generate_batch(db, request.form)
                    else:
                        raise ValueError("Choose a valid action.")
                if action not in ("preview", "batch_preview"):
                    return redirect(url_for("native_finance.monthly_commitments"),code=303)
            except Conflict as exc:
                error,status=str(exc),409
            except ValueError as exc:
                error,status=str(exc),400
            except (sqlite3.Error,OSError):
                abort(503)
        try:
            with closing(connect()) as db, db:
                tables(db)
                schedules=[dict(r) for r in db.execute(
                    "SELECT * FROM finance_monthly_schedules ORDER BY archived,name,id")]
                occurrences=[dict(r) for r in db.execute("""SELECT o.*, b.name AS current_name,
                    b.amount AS current_amount,b.due AS current_due,b.status AS current_status,
                    b.archived AS current_archived FROM finance_monthly_occurrences o
                    LEFT JOIN finance_bills b ON b.id=o.bill_id ORDER BY o.month DESC,o.created_at DESC LIMIT 200""")]
                history=[dict(r) for r in db.execute("""SELECT * FROM finance_monthly_schedule_events
                    ORDER BY created_at DESC,schedule_id,revision DESC LIMIT 200""")]
            for event in history:
                event["values"]=json.loads(event["snapshot"])
            for event in occurrences:
                event["values"]=json.loads(event["snapshot"])
        except (sqlite3.Error,OSError):
            abort(503)
        return render_template("finance_monthly_commitments.html",schedules=schedules,
            occurrences=occurrences,history=history,new_id=uuid.uuid4().hex,
            month=datetime.now(timezone.utc).strftime("%Y-%m"),proposal=proposal,
            batch=batch,error=error,csrf=session.get("csrf","")),status,{"Cache-Control":"no-store"}
