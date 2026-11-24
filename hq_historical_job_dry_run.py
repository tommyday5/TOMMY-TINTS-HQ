"""Owner-triggered, immutable dry-run comparison; never writes canonical work."""
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from customer_request_store import connect, storage_ready
from hq_historical_job_full_evidence import verified_batch_evidence, _full_rows, _pad, WIDTH
from hq_historical_job_field_review import read_plan
from hq_work_progress import _actor_fingerprint


def _pence(value):
    # Sheets unformatted numeric cells only. Blank or ambiguous input is unknown.
    if type(value) not in (int, float):
        return None
    try:
        pence = Decimal(str(value)) * 100
        if not pence.is_finite() or not 0 <= pence <= 100000000 or pence != pence.to_integral_value():
            return None
        return int(pence)
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _compare(left, right):
    if left is None or right is None:
        return "Unknown / missing"
    return "Equal" if left == right else "Different"


def _name(value):
    return " ".join(value.casefold().split()) if isinstance(value, str) and value.strip() else None


def _customer_crosscheck(db, item, raw_job_name, effective_job_name, formula, computed):
    """Cross-check an inactive Customers proposal, never establish identity."""
    from hq_historical_customer_links import _norm
    from hq_historical_customer_field_review import _candidate_context

    result = {"status": "No active customer proposal"}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_historical_customer_candidates'").fetchone():
        return result
    proposal = db.execute("""SELECT id,basis FROM hq_historical_customer_candidates
        WHERE customer_id=? AND withdrawn_at IS NULL""", (item["customer_id"],)).fetchone()
    if not proposal:
        return result
    result = {"status": "Customer proposal stale or ambiguous", "proposal_id": proposal["id"],
              "basis": proposal["basis"]}
    try:
        candidate, raw, _, _ = _candidate_context(db, proposal["id"], formula, computed)
    except (ValueError, LookupError):
        return result
    result["customer_sheet_row"] = candidate["sheet_row"]
    if (not isinstance(raw_job_name, str) or raw_job_name.startswith("=")
            or not _norm(raw_job_name)
            or _name(raw_job_name) != _name(effective_job_name)
            or _name(raw_job_name) != _name(raw[0])):
        result["status"] = "Job and Customers source names differ or are not literal"
    elif item["customer_id"] != item["vehicle_customer"]:
        result["status"] = "Native vehicle/customer conflict"
    else:
        result["status"] = "Current inactive proposal cross-check agrees"
    return result


def read_comparison(db, batch_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_historical_job_dry_runs'").fetchone():
        return None
    row = db.execute("""SELECT revision,plan_revision,report,created_at
        FROM hq_historical_job_dry_runs WHERE batch_id=? ORDER BY revision DESC LIMIT 1""",
        (batch_id,)).fetchone()
    if not row:
        return None
    return {**dict(row), "report": json.loads(row["report"])}


def save_comparison(batch_id, expected_revision, expected_plan_revision, *,
                    legacy_formula, legacy_computed, full_formula, full_computed, owner_identity):
    from hq_historical_preview import _HEX
    if (not storage_ready() or not isinstance(batch_id, str) or not _HEX.fullmatch(batch_id)
            or any(type(v) is not int or not 0 <= v < 2**63-1
                   for v in (expected_revision, expected_plan_revision))):
        raise ValueError("Invalid comparison revision")
    actor = _actor_fingerprint(owner_identity)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_job_dry_runs (
            batch_id TEXT NOT NULL REFERENCES hq_historical_job_batches(id),
            revision INTEGER NOT NULL CHECK(revision>0),
            plan_revision INTEGER NOT NULL CHECK(plan_revision>0),
            report TEXT NOT NULL, actor_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(batch_id,revision))""")
        db.execute("BEGIN IMMEDIATE")
        fresh = verified_batch_evidence(db, batch_id, legacy_formula, legacy_computed,
                                        full_formula, full_computed)
        proof = db.execute("""SELECT evidence FROM hq_historical_job_batch_full_evidence
            WHERE batch_id=? AND version=2""", (batch_id,)).fetchone()
        if not proof or not hmac.compare_digest(proof["evidence"], fresh):
            raise ValueError("Source evidence missing or changed")
        plan = read_plan(db, batch_id)
        if not plan or plan["revision"] != expected_plan_revision:
            raise ValueError("Save or reload the current field plan first")
        values = _full_rows(full_computed)
        raw_values = _full_rows(full_formula)
        items = db.execute("""SELECT i.job_ref,i.sheet_row,i.work_revision,
            w.price_pence,w.customer_id,c.name,v.registration,v.customer_id AS vehicle_customer
            FROM hq_historical_job_batch_items i
            JOIN hq_work_records w ON w.id=i.work_id
            JOIN hq_customers c ON c.id=w.customer_id
            JOIN hq_vehicles v ON v.id=w.vehicle_id
            WHERE i.batch_id=? ORDER BY i.sheet_row""", (batch_id,)).fetchall()
        if len(items) != len(json.loads(fresh)):
            raise ValueError("Native relationship missing")
        rows = []
        for item in items:
            source = _pad(values[item["sheet_row"]-4], WIDTH)
            quoted = _pence(source[16])
            rows.append({
                "job_ref": item["job_ref"], "sheet_row": item["sheet_row"],
                "source_quote_pence": quoted, "native_quote_pence": item["price_pence"],
                "quote_comparison": _compare(quoted, item["price_pence"]),
                "customer_name_comparison": _compare(_name(source[2]), _name(item["name"])),
                "registration_comparison": _compare(_name(source[4]), _name(item["registration"])),
                "vehicle_customer_consistent": item["customer_id"] == item["vehicle_customer"],
                "customer_proposal": _customer_crosscheck(db, item,
                    _pad(raw_values[item["sheet_row"]-4], WIDTH)[2], source[2],
                    legacy_formula, legacy_computed),
            })
        report = {"rows": rows, "unresolved_fields": WIDTH-plan["reviewed"]}
        previous = read_comparison(db, batch_id)
        revision = previous["revision"] if previous else 0
        if (previous and previous["plan_revision"] == expected_plan_revision
                and previous["report"] == report and expected_revision in (revision, revision-1)):
            return revision
        if revision != expected_revision:
            raise ValueError("Dry run changed; reload before saving")
        db.execute("""INSERT INTO hq_historical_job_dry_runs VALUES(?,?,?,?,?,?)""",
                   (batch_id,revision+1,expected_plan_revision,
                    json.dumps(report,separators=(",",":"),allow_nan=False),actor,
                    datetime.now(timezone.utc).isoformat()))
        return revision+1
