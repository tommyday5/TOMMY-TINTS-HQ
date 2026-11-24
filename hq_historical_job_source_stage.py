"""Explicit owner-triggered historical Jobs source staging, not canonical import."""
import hmac
import json
from datetime import datetime, timezone

from customer_request_store import connect, storage_ready
from hq_historical_job_full_evidence import (
    HEADERS, WIDTH, RANGE, _full_rows, _pad, _digest, verified_batch_evidence,
)
from hq_historical_job_field_review import read_plan
from hq_historical_job_dry_run import read_comparison
from hq_work_progress import _actor_fingerprint

MAX_BYTES = 2 * 1024 * 1024


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_job_source_stages (
        batch_id TEXT PRIMARY KEY REFERENCES hq_historical_job_batches(id),
        workbook_id TEXT NOT NULL, sheet_id INTEGER NOT NULL, source_range TEXT NOT NULL,
        headers TEXT NOT NULL, evidence_version INTEGER NOT NULL CHECK(evidence_version=2),
        plan_revision INTEGER NOT NULL CHECK(plan_revision>0),
        dry_revision INTEGER NOT NULL CHECK(dry_revision>0),
        row_count INTEGER NOT NULL CHECK(row_count BETWEEN 1 AND 20),
        actor_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_job_source_rows (
        batch_id TEXT NOT NULL REFERENCES hq_historical_job_source_stages(batch_id),
        candidate_id TEXT NOT NULL, sheet_row INTEGER NOT NULL, job_ref TEXT NOT NULL,
        source_digest TEXT NOT NULL, raw_cells TEXT NOT NULL, effective_cells TEXT NOT NULL,
        PRIMARY KEY(batch_id,candidate_id),
        UNIQUE(batch_id,sheet_row), UNIQUE(batch_id,job_ref),
        FOREIGN KEY(batch_id,candidate_id)
          REFERENCES hq_historical_job_batch_items(batch_id,candidate_id))""")


def read_stage(db, batch_id):
    if not db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
        AND name='hq_historical_job_source_stages'""").fetchone():
        return None
    row = db.execute("""SELECT plan_revision,dry_revision,row_count,created_at
        FROM hq_historical_job_source_stages WHERE batch_id=?""", (batch_id,)).fetchone()
    if not row:
        return None
    count = db.execute("""SELECT COUNT(*) FROM hq_historical_job_source_rows
        WHERE batch_id=?""", (batch_id,)).fetchone()[0]
    return {**dict(row), "row_count_consistent": count == row["row_count"]}


def stage_source_rows(batch_id, expected_plan, expected_dry, *,
                      legacy_formula, legacy_computed, full_formula, full_computed,
                      owner_identity):
    """Copy bounded, freshly verified source evidence into an inactive staging area.

    Original formula strings are retained as data, never evaluated. This is not
    a field mapping, an active link, a payment allocation or import approval.
    """
    from hq_historical_preview import WORKBOOK, _HEX
    if (not storage_ready() or not isinstance(batch_id, str) or not _HEX.fullmatch(batch_id)
            or any(type(v) is not int or not 1 <= v < 2**63-1
                   for v in (expected_plan, expected_dry))):
        raise ValueError("Invalid stage revisions")
    actor = _actor_fingerprint(owner_identity)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        fresh = verified_batch_evidence(db,batch_id,legacy_formula,legacy_computed,
                                        full_formula,full_computed)
        proof = db.execute("""SELECT evidence FROM hq_historical_job_batch_full_evidence
            WHERE batch_id=? AND version=2""", (batch_id,)).fetchone()
        if not proof or not hmac.compare_digest(proof["evidence"],fresh):
            raise ValueError("Source evidence missing or changed")
        plan, dry = read_plan(db,batch_id), read_comparison(db,batch_id)
        if (not plan or not dry or plan["revision"] != expected_plan
                or dry["revision"] != expected_dry or dry["plan_revision"] != expected_plan):
            raise ValueError("Save or reload current comparison first")
        raw, effective = _full_rows(full_formula), _full_rows(full_computed)
        rows, size = [], 0
        for cid, row, ref, digest in json.loads(fresh):
            a,b = _pad(raw[row-4],WIDTH),_pad(effective[row-4],WIDTH)
            if any(type(v) not in (str,int,float,bool) and v is not None for v in a+b):
                raise ValueError("Unsupported source cell")
            if not hmac.compare_digest(_digest([row,ref,HEADERS,a,b]),digest):
                raise ValueError("Source cells changed")
            raw_json = json.dumps(a,ensure_ascii=True,separators=(",",":"),allow_nan=False)
            effective_json = json.dumps(b,ensure_ascii=True,separators=(",",":"),allow_nan=False)
            size += len(raw_json)+len(effective_json)
            if size > MAX_BYTES:
                raise ValueError("Source stage too large; use smaller batches")
            rows.append((batch_id,cid,row,ref,digest,raw_json,effective_json))
        prior = read_stage(db,batch_id)
        if prior:
            existing = [tuple(r) for r in db.execute("""SELECT batch_id,candidate_id,sheet_row,
                job_ref,source_digest,raw_cells,effective_cells FROM hq_historical_job_source_rows
                WHERE batch_id=? ORDER BY sheet_row""",(batch_id,))]
            if (not prior["row_count_consistent"] or existing != rows
                    or prior["plan_revision"] != expected_plan or prior["dry_revision"] != expected_dry):
                raise ValueError("Stage immutable; archive and restage for changed review")
            return
        db.execute("""INSERT INTO hq_historical_job_source_stages
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(batch_id,WORKBOOK,330634230,RANGE,
            json.dumps(HEADERS,ensure_ascii=True),2,expected_plan,expected_dry,
            len(rows),actor,datetime.now(timezone.utc).isoformat()))
        db.executemany("""INSERT INTO hq_historical_job_source_rows
            VALUES(?,?,?,?,?,?,?)""",rows)
