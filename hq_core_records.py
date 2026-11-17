"""Durable internal customer, vehicle and work records. No external integrations."""
import sqlite3
import uuid
from datetime import datetime, timezone
from customer_request_store import connect

def _now():
    return datetime.now(timezone.utc).isoformat()

def _schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_customers (
        id TEXT PRIMARY KEY, source_request_id TEXT UNIQUE, name TEXT NOT NULL,
        email TEXT NOT NULL, phone TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_vehicles (
        id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, make TEXT NOT NULL,
        model TEXT NOT NULL, model_year TEXT NOT NULL, registration TEXT NOT NULL,
        created_at TEXT NOT NULL, FOREIGN KEY(customer_id) REFERENCES hq_customers(id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_work_records (
        id TEXT PRIMARY KEY, source_request_id TEXT NOT NULL UNIQUE,
        customer_id TEXT NOT NULL, vehicle_id TEXT NOT NULL,
        kind TEXT NOT NULL, service TEXT NOT NULL, notes TEXT NOT NULL,
        preferred_date TEXT NOT NULL, preferred_time TEXT NOT NULL,
        status TEXT NOT NULL, price_pence INTEGER, deposit_pence INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY(customer_id) REFERENCES hq_customers(id),
        FOREIGN KEY(vehicle_id) REFERENCES hq_vehicles(id))""")

def create_internal_work_record(request_id):
    """Idempotent conversion from a reviewed request to an internal work record."""
    if not isinstance(request_id,str) or len(request_id)!=32 or not all(c in "0123456789abcdef" for c in request_id):
        raise ValueError("Invalid request reference")
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT * FROM customer_requests WHERE id=?",(request_id,)).fetchone()
        if row is None: raise LookupError("Customer request not found")
        if row["status"] in ("declined","closed"): raise ValueError("Cannot convert a closed or declined request")
        draft=db.execute("SELECT * FROM customer_request_drafts WHERE request_id=?",(request_id,)).fetchone()
        if draft is None: raise ValueError("Prepare an internal quote or job draft first")
        existing=db.execute("SELECT id FROM hq_work_records WHERE source_request_id=?",(request_id,)).fetchone()
        if existing: return existing["id"]
        now=_now()
        customer_id=uuid.uuid4().hex
        vehicle_id=uuid.uuid4().hex
        work_id=uuid.uuid4().hex
        db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                   (customer_id,request_id,row["name"],row["email"],row["phone"],now))
        db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                   (vehicle_id,customer_id,row["vehicle_make"],row["vehicle_model"],row["vehicle_year"],row["registration"],now))
        db.execute("""INSERT INTO hq_work_records
          (id,source_request_id,customer_id,vehicle_id,kind,service,notes,preferred_date,preferred_time,status,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
          (work_id,request_id,customer_id,vehicle_id,draft["draft_type"],row["service"],row["notes"],
           row["preferred_date"],row["preferred_time"],"draft",now))
        return work_id

def list_work_records(limit=100):
    with connect() as db:
        _schema(db)
        rows=db.execute("""SELECT w.id,w.source_request_id,w.kind,w.service,w.notes,
            w.preferred_date,w.preferred_time,w.status,w.price_pence,w.deposit_pence,w.created_at,
            c.name,c.email,c.phone,v.make,v.model,v.model_year,v.registration
            FROM hq_work_records w JOIN hq_customers c ON c.id=w.customer_id
            JOIN hq_vehicles v ON v.id=w.vehicle_id
            ORDER BY w.created_at DESC LIMIT ?""",(min(100,max(1,int(limit))),)).fetchall()
        return [dict(row) for row in rows]

def set_draft_financials(record_id,price_pence,deposit_pence):
    if not isinstance(record_id,str) or len(record_id)!=32: raise ValueError("Invalid record")
    if type(price_pence) is not int or type(deposit_pence) is not int:
        raise ValueError("Amounts must be integer pence")
    if not 0 <= deposit_pence <= price_pence <= 100000000:
        raise ValueError("Invalid price or deposit")
    with connect() as db:
        _schema(db)
        changed=db.execute("""UPDATE hq_work_records SET price_pence=?,deposit_pence=?
          WHERE id=? AND status='draft'""",(price_pence,deposit_pence,record_id))
        if not changed.rowcount: raise LookupError("Editable draft not found")
    return {"id":record_id,"price_pence":price_pence,"deposit_pence":deposit_pence,
            "balance_pence":price_pence-deposit_pence}
