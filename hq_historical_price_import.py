"""Owner-approved, reversible one-row historical quoted-price import.

Only fills an absent price on an existing owner-native draft. No customer
merge, new jobs, payment, provider, outbox, or workbook writes.
"""
import hmac
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone

from customer_request_store import connect, storage_ready
from hq_core_records import _audit_work
from hq_historical_job_full_evidence import _digest, verified_batch_evidence
from hq_historical_job_rehearsal import _snapshot
from hq_historical_preview import _HEX, _identity_evidence, _norm
from hq_work_progress import _actor_fingerprint


def _now():
    return datetime.now(timezone.utc).isoformat()


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_price_imports (
        id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
        job_ref TEXT NOT NULL, work_id TEXT NOT NULL, manifest TEXT NOT NULL,
        prepared_by TEXT NOT NULL, prepared_at TEXT NOT NULL,
        applied_at TEXT, applied_by TEXT, applied_stamp TEXT,
        rolled_back_at TEXT, rolled_back_by TEXT,
        FOREIGN KEY(batch_id,candidate_id) REFERENCES hq_historical_job_source_rows(batch_id,candidate_id))""")
    # An applied historical reference or target cannot be reused by restaging,
    # even after reversal; review of a second attempt requires a later workflow.
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_historical_price_import_ref
        ON hq_historical_price_imports(job_ref COLLATE NOCASE) WHERE applied_at IS NOT NULL""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS hq_historical_price_import_work
        ON hq_historical_price_imports(work_id) WHERE applied_at IS NOT NULL""")


def read_manifests(db, batch_id, candidate_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_historical_price_imports'").fetchone():
        return []
    return [dict(r, manifest=json.loads(r["manifest"])) for r in db.execute(
        """SELECT id,manifest,prepared_at,applied_at,rolled_back_at
        FROM hq_historical_price_imports WHERE batch_id=? AND candidate_id=?
        ORDER BY prepared_at DESC LIMIT 20""", (batch_id,candidate_id))]


def _snapshot_matches(snapshot_id, before, source_digest, batch_id, candidate_id):
    from hq_private_recovery import _paths, _existing, _restore_check, MAX_SECONDS
    if not isinstance(snapshot_id,str) or not _HEX.fullmatch(snapshot_id):
        raise ValueError("Choose a verified private snapshot")
    _, directory = _paths()
    if not any(x["id"] == snapshot_id for x in _existing(directory)):
        raise ValueError("Snapshot missing")
    path = directory / (snapshot_id + ".sqlite3")
    _restore_check(path, time.monotonic() + MAX_SECONDS)
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=1)) as saved:
        saved.row_factory = sqlite3.Row
        saved.execute("PRAGMA query_only=ON")
        if _snapshot(saved,before["id"]) != before:
            raise ValueError("Recovery snapshot does not contain current draft")
        row = saved.execute("""SELECT source_digest FROM hq_historical_job_source_rows
            WHERE batch_id=? AND candidate_id=?""",(batch_id,candidate_id)).fetchone()
        if not row or not hmac.compare_digest(row["source_digest"],source_digest):
            raise ValueError("Recovery snapshot predates the staged evidence")


def _manifest(db, batch_id, candidate_id, snapshot_id, source):
    from hq_historical_job_quote_mapping import _context
    formula, computed, full_formula, full_computed = source
    ctx = _context(db,batch_id,candidate_id)
    row = ctx["row"]
    # A batch with more than one row would invalidate its remaining review
    # revisions after the first write. Require a singleton for this first slice.
    if db.execute("SELECT COUNT(*) FROM hq_historical_job_batch_items WHERE batch_id=?",(batch_id,)).fetchone()[0] != 1:
        raise ValueError("Use a single-row review batch")
    if not ctx["eligible"] or not ctx["history"] or ctx["history"][0]["decision"] != "propose":
        raise ValueError("Save an eligible quoted-price proposal")
    if any(i["history"] and i["history"][0]["decision"] == "propose"
           for i in ctx["classification"] + ctx["actual"]["fields"]):
        raise ValueError("This slice only imports price; preserve other mappings for later")
    before = _snapshot(db,row["work_id"])
    if (before["source_request_id"] != "HQ-"+before["id"] or before["status"] != "draft"
            or before["price_pence"] is not None or before["deposit_pence"] != 0
            or before["customer_tier"] is None):
        raise ValueError("Only an unpriced owner-native draft with no deposit is eligible")
    fresh = verified_batch_evidence(db,batch_id,formula,computed,full_formula,full_computed)
    proof = db.execute("SELECT evidence FROM hq_historical_job_batch_full_evidence WHERE batch_id=? AND version=2",(batch_id,)).fetchone()
    if not proof or not hmac.compare_digest(proof["evidence"],fresh):
        raise ValueError("Fresh Sheets evidence differs")
    if json.loads(fresh) != [[candidate_id,row["sheet_row"],row["job_ref"],row["source_digest"]]]:
        raise ValueError("Staged row no longer agrees")
    candidate = db.execute("SELECT * FROM hq_historical_j_candidates WHERE id=?",(candidate_id,)).fetchone()
    identity = _identity_evidence(db,candidate,formula,computed)
    review = db.execute("SELECT * FROM hq_historical_identity_reviews WHERE candidate_id=?",(candidate_id,)).fetchone()
    assessment = db.execute("SELECT * FROM hq_historical_candidate_field_reviews WHERE candidate_id=?",(candidate_id,)).fetchone()
    if (not review or not assessment or review["decision"] != "potential_match"
            or review["basis"] != "owner_document_review"
            or review["source_stamp"] != row["source_stamp"] or review["work_revision"] != before["revision"]
            or review["customer_id"] != before["customer_id"] or review["customer_stamp"] != identity["stamp"]
            or review["assessment_revision"] != assessment["revision"]
            or assessment["source_stamp"] != row["source_stamp"] or assessment["work_revision"] != before["revision"]
            or not identity["same_name"] or identity["source_count"] != 1 or identity["native_count"] != 1):
        raise ValueError("Current owner document identity review required")
    decisions = json.loads(assessment["assessments"])
    if any(decisions.get(k) != "appears_consistent" for k in ("customer","vehicle","services")):
        raise ValueError("Customer, vehicle and scope must be reviewed")
    if "conflict" in decisions.values():
        raise ValueError("Resolve conflicting field assessments")
    vehicle = db.execute("SELECT * FROM hq_vehicles WHERE id=?",(before["vehicle_id"],)).fetchone()
    raw_reg, effective_reg = ctx["cells"][4][1:]
    if (not vehicle or vehicle["customer_id"] != before["customer_id"]
            or not isinstance(raw_reg,str) or raw_reg.startswith("=")
            or raw_reg != effective_reg or not _norm(raw_reg)
            or _norm(raw_reg) != _norm(vehicle["registration"])):
        raise ValueError("Literal vehicle registration must agree")
    if sum(_norm(v["registration"]) == _norm(raw_reg) for v in db.execute("SELECT registration FROM hq_vehicles")) != 1:
        raise ValueError("Duplicate vehicle registration")
    if ctx["history"][0]["source_pence"] != ctx["source_pence"]:
        raise ValueError("Price proposal changed")
    if db.execute("""SELECT 1 FROM hq_historical_price_imports WHERE applied_at IS NOT NULL
        AND (work_id=? OR job_ref=? COLLATE NOCASE)""",(before["id"],row["job_ref"])).fetchone():
        raise ValueError("Historical reference or target previously imported")
    _snapshot_matches(snapshot_id,before,row["source_digest"],batch_id,candidate_id)
    return dict(version=1,batch_id=batch_id,candidate_id=candidate_id,job_ref=row["job_ref"],
                sheet_row=row["sheet_row"],work_id=before["id"],customer_id=before["customer_id"],
                vehicle_id=before["vehicle_id"],before_price=None,after_price=ctx["source_pence"],
                work_revision=before["revision"],work_stamp=_digest(before),
                source_digest=row["source_digest"],mapping_revision=ctx["history"][0]["revision"],
                plan_revision=row["plan_revision"],dry_revision=row["dry_revision"],
                identity_revision=review["revision"],assessment_revision=assessment["revision"],
                vehicle_stamp=_digest(dict(vehicle)),snapshot_id=snapshot_id,
                affected_jobs=1,affected_customers=0,affected_vehicles=0)


def prepare(batch_id,candidate_id,snapshot_id,source,owner):
    if not storage_ready():
        raise ValueError("Storage unavailable")
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        manifest = _manifest(db,batch_id,candidate_id,snapshot_id,source)
        mid = _digest(manifest)
        db.execute("""INSERT OR IGNORE INTO hq_historical_price_imports
            (id,batch_id,candidate_id,job_ref,work_id,manifest,prepared_by,prepared_at)
            VALUES(?,?,?,?,?,?,?,?)""",(mid,batch_id,candidate_id,manifest["job_ref"],manifest["work_id"],
            json.dumps(manifest,sort_keys=True),_actor_fingerprint(owner),_now()))
        return mid


def execute(batch_id,candidate_id,manifest_id,source,owner):
    if not storage_ready():
        raise ValueError("Storage unavailable")
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        event = db.execute("""SELECT * FROM hq_historical_price_imports
            WHERE id=? AND batch_id=? AND candidate_id=?""",(manifest_id,batch_id,candidate_id)).fetchone()
        if not event:
            raise LookupError("Manifest unavailable")
        if event["rolled_back_at"]:
            raise ValueError("Reversed import cannot be reapplied")
        if event["applied_at"]:
            return
        saved = json.loads(event["manifest"])
        current = _manifest(db,batch_id,candidate_id,saved["snapshot_id"],source)
        if current != saved or not hmac.compare_digest(_digest(current),manifest_id):
            raise ValueError("Manifest changed; prepare again")
        before = _snapshot(db,saved["work_id"])
        rev = before["revision"]+1
        if db.execute("""UPDATE hq_work_records SET price_pence=?, revision=?
            WHERE id=? AND revision=? AND price_pence IS NULL AND status='draft'""",
            (saved["after_price"],rev,before["id"],before["revision"])).rowcount != 1:
            raise ValueError("Concurrent change")
        after = _snapshot(db,before["id"])
        if after != dict(before,price_pence=saved["after_price"],revision=rev):
            raise ValueError("Post-import parity failed")
        _audit_work(db,before["id"],rev,"historical_price_import",
                    {"price_pence":None},{"price_pence":saved["after_price"],"manifest":manifest_id})
        db.execute("""UPDATE hq_historical_price_imports SET applied_at=?,applied_by=?,applied_stamp=?
            WHERE id=?""",(_now(),_actor_fingerprint(owner),_digest(after),manifest_id))


def reverse(batch_id,candidate_id,manifest_id,owner):
    if not storage_ready():
        raise ValueError("Storage unavailable")
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        event = db.execute("""SELECT * FROM hq_historical_price_imports
            WHERE id=? AND batch_id=? AND candidate_id=?""",(manifest_id,batch_id,candidate_id)).fetchone()
        if not event or not event["applied_at"]:
            raise ValueError("No applied import")
        if event["rolled_back_at"]:
            return
        saved = json.loads(event["manifest"])
        before = _snapshot(db,saved["work_id"])
        if (before["status"] != "draft" or not hmac.compare_digest(_digest(before),event["applied_stamp"])
            or db.execute("SELECT 1 FROM hq_work_archive_state WHERE work_id=? AND is_archived=1",(before["id"],)).fetchone()):
            raise ValueError("Draft changed after import; manual review required")
        rev = before["revision"]+1
        if db.execute("""UPDATE hq_work_records SET price_pence=NULL,revision=? WHERE id=? AND revision=?""",
                      (rev,before["id"],before["revision"])).rowcount != 1:
            raise ValueError("Concurrent change")
        if _snapshot(db,before["id"]) != dict(before,price_pence=None,revision=rev):
            raise ValueError("Reversal parity failed")
        _audit_work(db,before["id"],rev,"historical_price_reversal",
                    {"price_pence":before["price_pence"]},{"price_pence":None,"manifest":manifest_id})
        db.execute("UPDATE hq_historical_price_imports SET rolled_back_at=?,rolled_back_by=? WHERE id=?",
                   (_now(),_actor_fingerprint(owner),manifest_id))
