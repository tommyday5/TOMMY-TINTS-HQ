"""Customer-requested vehicle additions, owner-reviewed against canonical HQ IDs.

Customer requests are proposals, not vehicles, bookings or stock commitments.
Only the existing owner vehicle form can create the canonical record. An owner
explicitly links a matching saved vehicle or declines the proposal. No Sheets
write, bulk import, external message or historical job change is performed.
"""
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from customer_request_store import connect
from hq_core_records import _schema
from hq_customer_archive import _tables as _archive_tables
from hq_native_work import _vehicle_fields

_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_PENDING = 5


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid HQ reference")


def _normal_registration(value):
    return (value or "").replace(" ", "").replace("-", "").upper()


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_portal_vehicle_add_requests (
        id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        make TEXT NOT NULL, model TEXT NOT NULL,
        model_year TEXT NOT NULL, registration TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','linked','declined')),
        linked_vehicle_id TEXT REFERENCES hq_vehicles(id),
        created_at TEXT NOT NULL, decided_at TEXT,
        CHECK((status='linked' AND linked_vehicle_id IS NOT NULL)
              OR (status!='linked' AND linked_vehicle_id IS NULL))
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_portal_vehicle_add_pending
        ON hq_portal_vehicle_add_requests(customer_id,status,created_at)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_portal_vehicle_add_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL REFERENCES hq_portal_vehicle_add_requests(id),
        action TEXT NOT NULL CHECK(action IN ('requested','linked','declined')),
        occurred_at TEXT NOT NULL
    )""")


def submit_vehicle_request(customer_id, payload):
    """Idempotent customer-owned request: never inserts into hq_vehicles."""
    _id(customer_id)
    if not hasattr(payload, "get"):
        raise ValueError("Invalid vehicle request")
    operation_id = payload.get("operation_id", "")
    _id(operation_id)
    values = _vehicle_fields(payload)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _archive_tables(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("""SELECT * FROM hq_portal_vehicle_add_requests
            WHERE operation_id=?""", (operation_id,)).fetchone()
        if previous:
            if previous["customer_id"] != customer_id or any(
                previous[key] != values[key] for key in values
            ):
                raise ValueError("This submission reference was already used with different details")
            return previous["id"]
        if not db.execute("SELECT 1 FROM hq_customers WHERE id=?",
                          (customer_id,)).fetchone():
            raise LookupError("HQ customer does not exist")
        if db.execute("""SELECT 1 FROM hq_customer_archive_state
            WHERE customer_id=? AND is_archived=1""", (customer_id,)).fetchone():
            raise ValueError("This customer account is archived; contact Tommy Tints")
        if db.execute("""SELECT COUNT(*) FROM hq_portal_vehicle_add_requests
            WHERE customer_id=? AND status='pending'""", (customer_id,)).fetchone()[0] >= _MAX_PENDING:
            raise ValueError("Several vehicle additions are already under review")
        normalized = _normal_registration(values["registration"])
        if normalized:
            if db.execute("""SELECT 1 FROM hq_vehicles WHERE
                replace(replace(upper(registration),' ',''),'-','')=? LIMIT 1""",
                (normalized,)).fetchone():
                raise ValueError("That registration is already held in HQ; request a correction instead")
            if db.execute("""SELECT 1 FROM hq_portal_vehicle_add_requests WHERE
                customer_id=? AND status='pending' AND
                replace(replace(upper(registration),' ',''),'-','')=? LIMIT 1""",
                (customer_id, normalized)).fetchone():
                raise ValueError("An addition for this registration is already pending")
        else:
            if db.execute("""SELECT 1 FROM hq_portal_vehicle_add_requests
                WHERE customer_id=? AND status='pending' AND registration=''
                AND lower(trim(make))=lower(trim(?))
                AND lower(trim(model))=lower(trim(?)) AND model_year=? LIMIT 1""",
                (customer_id, values["make"], values["model"],
                 values["model_year"])).fetchone():
                raise ValueError("A matching unregistered vehicle request is already pending")
        ref, now = uuid.uuid4().hex, _now()
        db.execute("""INSERT INTO hq_portal_vehicle_add_requests
            (id,operation_id,customer_id,make,model,model_year,registration,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (ref, operation_id, customer_id,
                values["make"], values["model"], values["model_year"],
                values["registration"], now))
        db.execute("""INSERT INTO hq_portal_vehicle_add_events
            (request_id,action,occurred_at) VALUES(?,'requested',?)""", (ref, now))
    return ref


def customer_vehicle_requests(customer_id):
    """Customer-scoped status snapshot, with no other customers' vehicles."""
    _id(customer_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        return [dict(row) for row in db.execute("""SELECT id,make,model,model_year,
            registration,status,linked_vehicle_id,created_at
            FROM hq_portal_vehicle_add_requests WHERE customer_id=?
            ORDER BY created_at DESC,id DESC LIMIT 20""", (customer_id,))]


def owner_vehicle_requests():
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        rows = [dict(r) for r in db.execute("""SELECT r.id,r.customer_id,r.make,
            r.model,r.model_year,r.registration,r.created_at,c.name AS customer_name
            FROM hq_portal_vehicle_add_requests r
            JOIN hq_customers c ON c.id=r.customer_id
            WHERE r.status='pending' ORDER BY r.created_at,r.id LIMIT 100""")]
        for item in rows:
            item["vehicles"] = [dict(v) for v in db.execute(
                """SELECT id,make,model,model_year,registration FROM hq_vehicles
                WHERE customer_id=? ORDER BY created_at DESC,id DESC LIMIT 100""",
                (item["customer_id"],))]
    return rows


def owner_prefill(ref, customer_id):
    """Only a pending request for this exact customer may prefill the owner form."""
    _id(ref)
    _id(customer_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        row = db.execute("""SELECT make,model,model_year,registration FROM
            hq_portal_vehicle_add_requests
            WHERE id=? AND customer_id=? AND status='pending'""",
            (ref, customer_id)).fetchone()
        if row is None:
            raise LookupError("Pending vehicle request not found")
    return dict(row)


def decide_vehicle_request(ref, decision, vehicle_id=""):
    """Link only an exact saved customer vehicle, or decline with audit trail.

    Optimistic decisions serialize under BEGIN IMMEDIATE and are repeat-safe.
    A saved canonical vehicle is never created or modified here.
    """
    _id(ref)
    if decision not in ("linked", "declined"):
        raise ValueError("Unknown vehicle request decision")
    if decision == "linked":
        _id(vehicle_id)
    elif vehicle_id:
        raise ValueError("A declined request must not claim a vehicle")
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        source = db.execute("SELECT * FROM hq_portal_vehicle_add_requests WHERE id=?",
                            (ref,)).fetchone()
        if source is None:
            raise LookupError("Vehicle request not found")
        if source["status"] == decision and (source["linked_vehicle_id"] or "") == vehicle_id:
            return
        if source["status"] != "pending":
            raise ValueError("This request has already been decided")
        if decision == "linked":
            target = db.execute("""SELECT customer_id,make,model,model_year,registration
                FROM hq_vehicles WHERE id=?""", (vehicle_id,)).fetchone()
            if (target is None or target["customer_id"] != source["customer_id"] or
                (target["make"].strip().casefold(), target["model"].strip().casefold(),
                 target["model_year"], _normal_registration(target["registration"])) !=
                (source["make"].strip().casefold(), source["model"].strip().casefold(),
                 source["model_year"], _normal_registration(source["registration"]))):
                raise ValueError("The saved HQ vehicle must belong to this customer and match every proposed field")
        now = _now()
        updated = db.execute("""UPDATE hq_portal_vehicle_add_requests
            SET status=?, linked_vehicle_id=?, decided_at=?
            WHERE id=? AND status='pending'""",
            (decision, vehicle_id if decision == "linked" else None, now, ref))
        if updated.rowcount != 1:
            raise ValueError("Request changed; reload before deciding")
        db.execute("""INSERT INTO hq_portal_vehicle_add_events
            (request_id,action,occurred_at) VALUES(?,?,?)""", (ref, decision, now))
