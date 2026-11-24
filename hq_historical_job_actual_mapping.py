"""Inactive per-job actual-cost aggregate proposals, never ledger entries."""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from customer_request_store import connect, storage_ready
from hq_historical_job_dry_run import _pence
from hq_historical_job_full_evidence import HEADERS
from hq_work_progress import _actor_fingerprint

FIELDS = {
    "actual_hours": (32, "minutes"),
    "actual_materials": (33, "pence"),
    "actual_consumables": (34, "pence"),
    "actual_extra_costs": (35, "pence"),
}


def _minutes(value):
    # An explicit whole-minute aggregate only; never round an ambiguous value.
    if type(value) not in (int, float):
        return None
    try:
        minutes = Decimal(str(value)) * 60
        if not minutes.is_finite() or not 0 <= minutes <= 1000000 or minutes != minutes.to_integral_value():
            return None
        return int(minutes)
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _exists(db, table):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def actual_context(db, row, raw, effective, plan, current):
    # Existing recorded ledgers are contextual only: their scope may differ from
    # the historical aggregate, and materials/consumables cannot be split safely.
    active = {}
    for label, table in (("time", "hq_work_time_entries"),
                         ("materials", "hq_work_material_entries"),
                         ("expenses", "hq_work_expense_entries")):
        active[label] = db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE work_id=? AND voided_at IS NULL",
            (row["work_id"],)).fetchone()[0] if _exists(db, table) else 0
    fields = []
    for field, (index, unit) in FIELDS.items():
        convert = _minutes if unit == "minutes" else _pence
        value = convert(effective[index])
        literal = value is not None and convert(raw[index]) == value
        eligible = bool(current and literal and row["status"] == "draft"
                        and plan["fields"][index]["decision"] == "n")
        history = []
        if _exists(db, "hq_historical_job_actual_mapping"):
            history = [dict(r) for r in db.execute("""SELECT revision,decision,source_amount,created_at
                FROM hq_historical_job_actual_mapping WHERE batch_id=? AND candidate_id=? AND field=?
                ORDER BY revision DESC LIMIT 50""", (row["batch_id"], row["candidate_id"], field))]
        fields.append(dict(field=field,label=HEADERS[index],unit=unit,source=value,
                           eligible=eligible,history=history))
    return {"fields":fields,"active":active}


def save_actual_mapping(batch_id, candidate_id, expected, field, decision, owner):
    from hq_historical_job_quote_mapping import _context
    if (not storage_ready() or type(expected) is not int or not 0 <= expected < 2**63-1
            or field not in FIELDS or decision not in ("preserve", "propose")):
        raise ValueError("Invalid actual mapping")
    actor = _actor_fingerprint(owner)
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_job_actual_mapping (
            batch_id TEXT NOT NULL,candidate_id TEXT NOT NULL,field TEXT NOT NULL
                CHECK(field IN ('actual_hours','actual_materials','actual_consumables','actual_extra_costs')),
            revision INTEGER NOT NULL CHECK(revision>0),decision TEXT NOT NULL CHECK(decision IN ('preserve','propose')),
            source_amount INTEGER CHECK(source_amount>=0),source_digest TEXT NOT NULL,
            work_revision INTEGER NOT NULL,actor_fingerprint TEXT NOT NULL,created_at TEXT NOT NULL,
            PRIMARY KEY(batch_id,candidate_id,field,revision),
            FOREIGN KEY(batch_id,candidate_id) REFERENCES hq_historical_job_source_rows(batch_id,candidate_id))""")
        db.execute("BEGIN IMMEDIATE")
        ctx = _context(db,batch_id,candidate_id)
        item = next(i for i in ctx["actual"]["fields"] if i["field"] == field)
        if not ctx["current"] or (decision == "propose" and not item["eligible"]):
            raise ValueError("Actual mapping not eligible")
        prior = item["history"][0] if item["history"] else None
        revision = prior["revision"] if prior else 0
        if prior and prior["decision"] == decision and expected in (revision,revision-1):
            return
        if expected != revision:
            raise ValueError("Actual mapping changed")
        row = ctx["row"]
        db.execute("INSERT INTO hq_historical_job_actual_mapping VALUES(?,?,?,?,?,?,?,?,?,?)",
            (batch_id,candidate_id,field,revision+1,decision,item["source"],row["source_digest"],
             row["work_revision"],actor,datetime.now(timezone.utc).isoformat()))
