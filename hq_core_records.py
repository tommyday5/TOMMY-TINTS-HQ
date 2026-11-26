"""Durable internal customer, vehicle and work records. No external integrations."""
import json
import sqlite3
import uuid
from hq_job_sync import enqueue as enqueue_sheet_sync
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
        revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
        FOREIGN KEY(customer_id) REFERENCES hq_customers(id),
        FOREIGN KEY(vehicle_id) REFERENCES hq_vehicles(id))""")
    # Additive migration: preserve all legacy work records on the /data volume.
    if not any(r["name"] == "revision" for r in db.execute("PRAGMA table_info(hq_work_records)")):
        try:
            db.execute("ALTER TABLE hq_work_records ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    db.execute("""CREATE TABLE IF NOT EXISTS hq_work_audit (
        id TEXT PRIMARY KEY, work_id TEXT NOT NULL, revision INTEGER NOT NULL,
        action TEXT NOT NULL, before_value TEXT NOT NULL,
        after_value TEXT NOT NULL, created_at TEXT NOT NULL)""")
    # Additive archive state: independent of job lifecycle and payment status.
    db.execute("""CREATE TABLE IF NOT EXISTS hq_work_archive_state (
        work_id TEXT PRIMARY KEY REFERENCES hq_work_records(id),
        is_archived INTEGER NOT NULL CHECK(is_archived IN (0,1)),
        archived_at TEXT, restored_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_work_archive_operations (
        operation_id TEXT PRIMARY KEY, work_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('archive','restore')),
        request_fingerprint TEXT NOT NULL, result_revision INTEGER NOT NULL,
        created_at TEXT NOT NULL)""")


class StaleWorkRecordError(ValueError):
    """A stored record changed after the owner loaded the edit form."""


def _validate_revision(value):
    if value is not None and (type(value) is not int or not 0 <= value < 2**63 - 1):
        raise ValueError("Invalid expected work revision")


def _audit_work(db, record_id, revision, action, before, after):
    """Write PII-free internal change metadata in the same DB transaction."""
    db.execute("INSERT INTO hq_work_audit VALUES(?,?,?,?,?,?,?)", (
        uuid.uuid4().hex, record_id, revision, action,
        json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True), _now()))


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
        enqueue_sheet_sync(db,work_id,0)
        return work_id

def list_work_records(limit=100):
    with connect() as db:
        _schema(db)
        rows=db.execute("""SELECT w.id,w.source_request_id,w.kind,w.service,w.notes,
            w.preferred_date,w.preferred_time,w.status,w.price_pence,w.deposit_pence,w.revision,w.created_at,
            w.customer_id,d.draft_type AS original_draft_kind,c.name,c.email,c.phone,v.make,v.model,v.model_year,v.registration
            FROM hq_work_records w JOIN hq_customers c ON c.id=w.customer_id
            JOIN hq_vehicles v ON v.id=w.vehicle_id
            LEFT JOIN customer_request_drafts d ON d.request_id=w.source_request_id
            WHERE NOT EXISTS (SELECT 1 FROM hq_work_archive_state a
                WHERE a.work_id=w.id AND a.is_archived=1)
            ORDER BY w.created_at DESC LIMIT ?""",(min(100,max(1,int(limit))),)).fetchall()
        return [dict(row) for row in rows]



def search_work_records(query="", kind="", status="", limit=25, offset=0, archived=False):
    """Search all linked internal work records, with bounded stable pagination.

    This is read-only and deliberately does not consult external Sheets, Square,
    bank accounts, or customer messages. Caller must enforce owner access.
    """
    if not isinstance(query, str) or len(query) > 80:
        raise ValueError("Search must be at most 80 characters")
    if kind not in ("", "job", "quote") or not isinstance(kind, str):
        raise ValueError("Invalid work type")
    if not isinstance(status, str) or (status and status not in WORK_TRANSITIONS):
        raise ValueError("Invalid work status")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Page size must be between 1 and 50")
    if type(offset) is not int or not 0 <= offset <= 100000:
        raise ValueError("Invalid page offset")
    if type(archived) is not bool:
        raise ValueError("Invalid archive filter")

    where, args = [], []
    where.append(("EXISTS" if archived else "NOT EXISTS") +
        " (SELECT 1 FROM hq_work_archive_state a WHERE a.work_id=w.id AND a.is_archived=1)")
    term = query.strip()
    if term:
        # instr treats % and _ literally, unlike unescaped SQL LIKE patterns.
        columns = ("c.name", "c.email", "c.phone", "v.registration",
                   "v.make", "v.model", "w.service")
        text_predicate = " OR ".join(
            f"instr(lower({col}), lower(?)) > 0" for col in columns
        )
        where.append("(" + text_predicate + ")")
        args.extend([term] * len(columns))
    if kind:
        where.append("w.kind=?")
        args.append(kind)
    if status:
        where.append("w.status=?")
        args.append(status)
    joins = """ FROM hq_work_records w
        JOIN hq_customers c ON c.id=w.customer_id
        JOIN hq_vehicles v ON v.id=w.vehicle_id
            LEFT JOIN customer_request_drafts d ON d.request_id=w.source_request_id"""
    with connect() as db:
        _schema(db)
        # Both queries use the same read transaction for a consistent count/page.
        db.execute("BEGIN")
        # Imported J references are immutable provenance, independent of notes.
        # Older databases need not have created the migration table yet.
        if term and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_historical_created_work'").fetchone():
            where[1] = "(" + text_predicate + " OR EXISTS (SELECT 1 FROM hq_historical_created_work h WHERE h.work_id=w.id AND h.job_ref=? COLLATE NOCASE))"
            args.insert(len(columns), term)
        predicate = " WHERE " + " AND ".join(where) if where else ""
        total = db.execute("SELECT COUNT(*)" + joins + predicate, args).fetchone()[0]
        rows = db.execute("""SELECT w.id,w.source_request_id,w.kind,w.service,
            w.notes,w.preferred_date,w.preferred_time,w.status,
            w.price_pence,w.deposit_pence,w.revision,w.created_at,w.customer_id,
            d.draft_type AS original_draft_kind,c.name,c.email,c.phone,v.make,v.model,v.model_year,v.registration"""
            + joins + predicate + " ORDER BY w.created_at DESC,w.id DESC LIMIT ? OFFSET ?",
            [*args, limit, offset]).fetchall()
    records = [dict(row) for row in rows]
    return {"records": records, "total": total, "limit": limit,
            "offset": offset, "has_more": offset + len(records) < total}


def read_work_record(record_id):
    """One internal record with linked customer and vehicle, for owner-only review."""
    if not isinstance(record_id, str) or len(record_id) != 32 or any(
        c not in "0123456789abcdef" for c in record_id
    ):
        raise ValueError("Invalid HQ record reference")
    with connect() as db:
        _schema(db)
        row = db.execute("""SELECT w.id,w.source_request_id,w.kind,w.service,w.notes,
            w.preferred_date,w.preferred_time,w.status,w.price_pence,w.deposit_pence,w.revision,w.created_at,
            d.draft_type AS original_draft_kind,c.name,c.email,c.phone,v.make,v.model,v.model_year,v.registration
            FROM hq_work_records w JOIN hq_customers c ON c.id=w.customer_id
            JOIN hq_vehicles v ON v.id=w.vehicle_id
            LEFT JOIN customer_request_drafts d ON d.request_id=w.source_request_id WHERE w.id=?""", (record_id,)).fetchone()
    if row is None:
        raise LookupError("HQ record not found")
    return dict(row)


def recent_work_records(kind, limit=5):
    """Bounded work/quote index, without contact details or registration numbers."""
    if kind not in ("job", "quote"):
        raise ValueError("Invalid HQ record kind")
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("Invalid HQ record limit")
    with connect() as db:
        _schema(db)
        rows = db.execute("""SELECT id,source_request_id,kind,service,status,
            price_pence,deposit_pence,revision
            FROM hq_work_records WHERE kind=?
              AND NOT EXISTS (SELECT 1 FROM hq_work_archive_state a
                  WHERE a.work_id=hq_work_records.id AND a.is_archived=1)
            ORDER BY created_at DESC,id DESC LIMIT ?""", (kind, limit)).fetchall()
    return [dict(row) for row in rows]

def _historical_job_reconciliation_required(db, record_id):
    """Non-quote migration evidence must not enter active work/finance."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_historical_created_work'").fetchone():
        return False
    row = db.execute("SELECT manifest FROM hq_historical_created_work WHERE work_id=?",
                     (record_id,)).fetchone()
    return bool(row and json.loads(row["manifest"]).get("source_status") != "Quoted")


def set_draft_financials(record_id, price_pence, deposit_pence, expected_revision=None):
    """Prevent older forms from overwriting updated draft financials.

    HTTP writes require a revision. The optional Python argument preserves
    backward compatibility for existing trusted internal callers.
    """
    if not isinstance(record_id, str) or len(record_id) != 32 or any(
        c not in "0123456789abcdef" for c in record_id
    ):
        raise ValueError("Invalid record")
    if type(price_pence) is not int or type(deposit_pence) is not int:
        raise ValueError("Amounts must be integer pence")
    if not 0 <= deposit_pence <= price_pence <= 100000000:
        raise ValueError("Invalid price or deposit")
    _validate_revision(expected_revision)
    with connect() as db:
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT status,price_pence,deposit_pence,revision
            FROM hq_work_records WHERE id=?""", (record_id,)).fetchone()
        if not row or row["status"] not in ("draft", "ready_for_review"):
            raise LookupError("Editable draft not found")
        if db.execute("SELECT 1 FROM hq_work_archive_state WHERE work_id=? AND is_archived=1",
                      (record_id,)).fetchone():
            raise ValueError("Restore this archived record before changing its price or deposit")
        if _historical_job_reconciliation_required(db, record_id):
            raise ValueError("Historical job prices require independent financial reconciliation before promotion.")
        unchanged = row["price_pence"] == price_pence and row["deposit_pence"] == deposit_pence
        if expected_revision is not None and row["revision"] != expected_revision and not unchanged:
            raise StaleWorkRecordError("This work record changed. Refresh it before saving.")
        revision = row["revision"]
        if not unchanged:
            changed = db.execute("""UPDATE hq_work_records SET price_pence=?,
                deposit_pence=?,revision=revision+1 WHERE id=? AND revision=?""",
                (price_pence, deposit_pence, record_id, revision))
            if changed.rowcount != 1:
                raise StaleWorkRecordError("This work record changed. Refresh it before saving.")
            revision += 1
            _audit_work(db, record_id, revision, "financials",
                {"price_pence": row["price_pence"], "deposit_pence": row["deposit_pence"]},
                {"price_pence": price_pence, "deposit_pence": deposit_pence})
        if not unchanged:
            enqueue_sheet_sync(db,record_id,revision)
    return {"id": record_id, "price_pence": price_pence,
            "deposit_pence": deposit_pence, "balance_pence": price_pence - deposit_pence,
            "revision": revision}

WORK_TRANSITIONS = {
    "draft": {"ready_for_review"},
    "ready_for_review": {"draft", "approved"},
    "approved": {"in_progress", "cancelled"},
    "in_progress": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}

def change_work_status(record_id, new_status, expected_revision=None):
    """Owner-reviewed internal state only; never books, messages or charges."""
    if not isinstance(record_id, str) or len(record_id) != 32 or any(
        c not in "0123456789abcdef" for c in record_id
    ):
        raise ValueError("Invalid record identifier")
    if not isinstance(new_status, str) or new_status not in WORK_TRANSITIONS:
        raise ValueError("Invalid work status")
    _validate_revision(expected_revision)
    with connect() as db:
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT status,kind,price_pence,revision
            FROM hq_work_records WHERE id=?""", (record_id,)).fetchone()
        if row is None:
            raise LookupError("Work record not found")
        if db.execute("SELECT 1 FROM hq_work_archive_state WHERE work_id=? AND is_archived=1",
                      (record_id,)).fetchone():
            raise ValueError("Restore this archived record before changing its status")
        old = row["status"]
        if old != new_status and _historical_job_reconciliation_required(db, record_id):
            raise ValueError("Historical job status requires independent reconciliation before promotion.")
        kind = "job" if row["kind"] == "quote" and new_status in ("approved", "in_progress") else row["kind"]
        unchanged = old == new_status and kind == row["kind"]
        if expected_revision is not None and row["revision"] != expected_revision and not unchanged:
            raise StaleWorkRecordError("This work record changed. Refresh it before updating status.")
        if old != new_status and new_status not in WORK_TRANSITIONS[old]:
            raise ValueError("Invalid status transition")
        if old != new_status and new_status == "approved" and row["price_pence"] is None:
            raise ValueError("Set and review a draft price before approval")
        if not unchanged:
            if new_status in ("completed", "cancelled"):
                from hq_work_timer import tables as timer_tables, _open as open_timer
                timer_tables(db)
                if open_timer(db, record_id):
                    raise ValueError("Finish and save or correct the open job timer before closing this job")
            changed = db.execute("""UPDATE hq_work_records SET status=?,kind=?,
                revision=revision+1 WHERE id=? AND revision=?""",
                (new_status, kind, record_id, row["revision"]))
            if changed.rowcount != 1:
                raise StaleWorkRecordError("This work record changed. Refresh it before updating status.")
            _audit_work(db, record_id, row["revision"] + 1, "status",
                {"status": old, "kind": row["kind"]}, {"status": new_status, "kind": kind})
            enqueue_sheet_sync(db,record_id,row["revision"] + 1)
    return new_status

def work_summary():
    """Aggregate all internal records, not only the 100 shown in the UI.

    Prices and deposits are owner-entered draft figures, not verified payments.
    """
    with connect() as db:
        _schema(db)
        db.execute("BEGIN")
        rows = db.execute("""SELECT status, COUNT(*) AS record_count,
            COALESCE(SUM(CASE WHEN status IN ('draft','ready_for_review')
                THEN COALESCE(price_pence,0) ELSE 0 END),0) AS draft_value,
            COALESCE(SUM(CASE WHEN status != 'cancelled' AND price_pence IS NOT NULL
                THEN price_pence - deposit_pence ELSE 0 END),0) AS outstanding
            FROM hq_work_records GROUP BY status""").fetchall()
        # A planned deposit is a quote field, NOT a receipt. Only use approved,
        # priced, internally consistent, non-TEST records for pipeline values.
        # Neither invoices nor bank/payment integrations are reconciled here.
        approved = db.execute("""SELECT COUNT(*) AS valid_count,
            COALESCE(SUM(price_pence),0) AS price_total,
            COALESCE(SUM(deposit_pence),0) AS planned_deposit_total,
            COALESCE(SUM(price_pence-deposit_pence),0) AS conditional_remainder
            FROM hq_work_records
            WHERE status IN ('approved','in_progress','completed')
              AND price_pence BETWEEN 0 AND 100000000
              AND deposit_pence BETWEEN 0 AND price_pence
              AND source_request_id NOT LIKE 'TEST-%'""").fetchone()
        approved_count = db.execute("""SELECT COUNT(*) FROM hq_work_records
            WHERE status IN ('approved','in_progress','completed')
              AND source_request_id NOT LIKE 'TEST-%'""").fetchone()[0]
        excluded_test_count = db.execute("""SELECT COUNT(*) FROM hq_work_records
            WHERE status IN ('approved','in_progress','completed')
              AND source_request_id LIKE 'TEST-%'""").fetchone()[0]
    return {
        "record_count": sum(row["record_count"] for row in rows),
        "by_status": {status: sum(row["record_count"] for row in rows if row["status"] == status)
                      for status in WORK_TRANSITIONS},
        "priced_draft_value_pence": sum(row["draft_value"] for row in rows),
        # Historical compatibility only: includes unapproved drafts and assumes
        # planned deposits were received. NEVER label this as money owed or cash.
        "outstanding_recorded_pence": sum(row["outstanding"] for row in rows),
        "outstanding_recorded_basis": "legacy_unverified_price_less_planned_deposit_including_drafts",
        "approved_priced_record_count": approved["valid_count"],
        "approved_amounts_incomplete_count": approved_count - approved["valid_count"],
        "excluded_synthetic_approved_record_count": excluded_test_count,
        "approved_price_total_pence": approved["price_total"],
        "planned_deposit_total_pence": approved["planned_deposit_total"],
        "conditional_remainder_pence": approved["conditional_remainder"],
        # Null means UNKNOWN; zero would falsely imply verified non-payment.
        "verified_cash_received_pence": None,
        "verified_cash_outstanding_pence": None,
        "payment_reconciliation_status": "not_connected",
        "source": "internal_work_records_only",
        "payment_verified": False,
    }

def create_test_job():
    """Explicit owner-initiated, isolated internal test job. Never sends anything."""
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        now=_now()
        cid=uuid.uuid4().hex
        vid=uuid.uuid4().hex
        wid=uuid.uuid4().hex
        # TEST-prefixed source IDs distinguish synthetic records from customer enquiries.
        source="TEST-"+uuid.uuid4().hex
        db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                   (cid,source,"TEST CUSTOMER — DO NOT CONTACT","test@example.invalid","00000000000",now))
        db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                   (vid,cid,"TEST","VEHICLE","","TEST-ONLY",now))
        db.execute("""INSERT INTO hq_work_records
            (id,source_request_id,customer_id,vehicle_id,kind,service,notes,
             preferred_date,preferred_time,status,price_pence,deposit_pence,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (wid,source,cid,vid,"job","Window Tinting",
             "TEST DATA ONLY — no real customer, appointment or payment",
             "","","draft",None,0,now))
        return wid

def delete_test_job(record_id):
    """Only delete synthetic test records; preserve every real customer record."""
    if not isinstance(record_id,str) or len(record_id)!=32 or any(c not in "0123456789abcdef" for c in record_id):
        raise ValueError("Invalid record")
    with connect() as db:
        _schema(db)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("""SELECT w.source_request_id,w.customer_id,w.vehicle_id
            FROM hq_work_records w WHERE w.id=?""",(record_id,)).fetchone()
        if row is None: raise LookupError("Test job not found")
        if not row["source_request_id"].startswith("TEST-"):
            raise ValueError("Only synthetic test jobs may be deleted")
        db.execute("DELETE FROM hq_work_records WHERE id=?",(record_id,))
        db.execute("DELETE FROM hq_vehicles WHERE id=?",(row["vehicle_id"],))
        db.execute("DELETE FROM hq_customers WHERE id=?",(row["customer_id"],))
    return True
