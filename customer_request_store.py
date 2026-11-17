"""Customer booking/quote requests: unconfirmed, owner-reviewed, no external actions."""
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

SERVICES = {"Window Tinting","Vehicle Wrapping","Bodywork & Paint","Wheel Refurbishment","Valeting & Detailing","Other / Bespoke Work","Interior & Leather Restoration"}
KINDS = {"booking","quote"}

def storage_ready():
    """Public submissions require a deliberately configured persistent storage path."""
    path = os.environ.get("HQ_DATA_DIR", "").strip()
    return bool(path) and Path(path).is_absolute() and not Path(path).resolve().is_relative_to(Path("/tmp"))

def connect():
    root = Path(os.environ.get("HQ_DATA_DIR", "/tmp/tommy-tints-hq"))
    root.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(str(root/"customer_requests.sqlite3"),timeout=10)
    db.row_factory=sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS customer_requests(
      id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
      email TEXT NOT NULL, phone TEXT NOT NULL, service TEXT NOT NULL,
      vehicle_make TEXT NOT NULL, vehicle_model TEXT NOT NULL,
      vehicle_year TEXT NOT NULL, registration TEXT NOT NULL,
      preferred_date TEXT NOT NULL, preferred_time TEXT NOT NULL,
      notes TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
      remote_fingerprint TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS request_fingerprint ON customer_requests(remote_fingerprint,created_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS customer_request_drafts(id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, draft_type TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(request_id) REFERENCES customer_requests(id))""")
    return db

def _text(value,limit,required=False):
    if not isinstance(value,str): raise ValueError("Invalid form field")
    value=value.strip()
    if len(value)>limit or (required and not value): raise ValueError("Missing or oversized form field")
    return value

def create_request(payload,fingerprint):
    if not isinstance(payload,dict): raise ValueError("Invalid request")
    if payload.get("website"): raise ValueError("Unable to accept request")
    kind=_text(payload.get("kind",""),12,True)
    service=_text(payload.get("service",""),80,True)
    if kind not in KINDS or service not in SERVICES: raise ValueError("Invalid request type or service")
    name=_text(payload.get("name",""),100,True)
    email=_text(payload.get("email",""),180,True)
    phone=_text(payload.get("phone",""),35,True)
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email): raise ValueError("Invalid email address")
    if not re.fullmatch(r"[+0-9 ()-]{7,35}",phone): raise ValueError("Invalid telephone number")
    make=_text(payload.get("make",""),60,True)
    model=_text(payload.get("model",""),60,True)
    year=_text(payload.get("year",""),4)
    reg=_text(payload.get("registration",""),20)
    date=_text(payload.get("date",""),10)
    time=_text(payload.get("time",""),5)
    notes=_text(payload.get("notes",""),1500)
    if kind=="booking":
        try:
            day=datetime.strptime(date,"%Y-%m-%d").date()
            if day<datetime.now(timezone.utc).date() or day>datetime.now(timezone.utc).date()+timedelta(days=365): raise ValueError()
        except ValueError: raise ValueError("Choose a valid future date")
        if time not in {"09:00","10:00","11:00","12:00","13:00","14:00","15:00"}: raise ValueError("Choose a preferred time")
    if not payload.get("privacy_acknowledged"): raise ValueError("Please acknowledge the privacy notice")
    now=datetime.now(timezone.utc).isoformat()
    identifier=uuid.uuid4().hex
    with connect() as db:
        # Serialize the limit check and insertion across concurrent web workers.
        # A read outside this transaction lets simultaneous requests all pass.
        db.execute("BEGIN IMMEDIATE")
        count=db.execute("SELECT COUNT(*) FROM customer_requests WHERE remote_fingerprint=? AND created_at>?",(fingerprint,(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())).fetchone()[0]
        if count>=3: raise ValueError("Too many requests. Please contact us directly.")
        db.execute("""INSERT INTO customer_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (identifier,kind,name,email,phone,service,make,model,year,reg,date,time,notes,"new",now,fingerprint))
    return identifier

def list_requests(limit=100):
    with connect() as db:
        rows=db.execute("""SELECT id,kind,name,email,phone,service,vehicle_make,vehicle_model,vehicle_year,registration,preferred_date,preferred_time,notes,status,created_at FROM customer_requests ORDER BY created_at DESC LIMIT ?""",(min(100,max(1,int(limit))),)).fetchall()
    return [dict(row) for row in rows]

VALID_STATUSES={"new","reviewing","quoted","declined","closed"}

def update_request_status(request_id,status):
    """Review a request without reopening closed or declined records."""
    if not isinstance(request_id,str) or not 1 <= len(request_id) <= 64:
        raise ValueError("Invalid request identifier")
    if not isinstance(status,str) or status not in VALID_STATUSES:
        raise ValueError("Invalid request status")
    with connect() as db:
        row=db.execute("SELECT status FROM customer_requests WHERE id=?",(request_id,)).fetchone()
        if row is None:
            raise LookupError("Customer request not found")
        previous=row["status"]
        if previous==status:
            return status
        if previous in {"closed","declined"}:
            raise ValueError("Closed or declined requests cannot be reopened")
        changed=db.execute(
            "UPDATE customer_requests SET status=? WHERE id=? AND status=?",
            (status,request_id,previous))
        if changed.rowcount!=1:
            raise ValueError("Request status changed concurrently; refresh and retry")
    return status

def create_review_draft(request_id,draft_type):
    """Idempotent internal draft. Never creates an actual job or sends a quote."""
    if not isinstance(request_id,str) or not 1 <= len(request_id) <= 64:
        raise ValueError("Invalid request identifier")
    if not isinstance(draft_type,str) or draft_type not in {"quote","job"}:
        raise ValueError("Invalid draft type")
    with connect() as db:
        # Make check-then-insert idempotent even under simultaneous owner retries.
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT id,kind,status FROM customer_requests WHERE id=?",(request_id,)).fetchone()
        if row is None: raise LookupError("Customer request not found")
        if row["status"] in {"declined","closed"}: raise ValueError("Closed requests cannot be converted")
        existing=db.execute("SELECT id,request_id,draft_type,created_at FROM customer_request_drafts WHERE request_id=?",(request_id,)).fetchone()
        if existing:
            if existing["draft_type"]!=draft_type: raise ValueError("Request already has a different draft type")
            return dict(existing)
        identifier=uuid.uuid4().hex
        created=datetime.now(timezone.utc).isoformat()
        db.execute("INSERT INTO customer_request_drafts VALUES(?,?,?,?)",(identifier,request_id,draft_type,created))
        return {"id":identifier,"request_id":request_id,"draft_type":draft_type,"created_at":created}

def list_review_drafts():
    with connect() as db:
        rows=db.execute("""SELECT d.id,d.request_id,d.draft_type,d.created_at,r.name,r.email,r.phone,r.service,r.vehicle_make,r.vehicle_model,r.vehicle_year,r.registration,r.notes,r.preferred_date,r.preferred_time FROM customer_request_drafts d JOIN customer_requests r ON r.id=d.request_id ORDER BY d.created_at DESC LIMIT 100""").fetchall()
    return [dict(row) for row in rows]
