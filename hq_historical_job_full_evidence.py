"""Immutable v2 source evidence for inactive historical Jobs review batches.

Legacy A:AZ candidate fingerprints remain untouched. This records keyed
evidence for A:CP, not an import, ownership transfer, or payment verification.
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone

import requests
from flask import current_app
from customer_request_store import connect, storage_ready
from hq_work_progress import _actor_fingerprint

WIDTH = 94
RANGE = "'Jobs'!A4:CP1000"
# Verified against the historical Jobs header; a schema change fails closed.
HEADERS = (
    "Job ID", "Date", "Customer", "Customer Type", "Vehicle/Reg", "Service 1",
    "Service 2", "Service 3", "Service 4", "Service 5", "Difficulty",
    "Manual Total Hours Override", "Material Override", "Consumable Override",
    "One-way Miles (Maps/Auto)", "Manual Travel £ Override", "Quoted Price",
    "Status", "Total Hours", "Materials £", "Consumables £", "Travel £",
    "Labour Rate", "Multiplier", "Loyalty %", "Minimum £", "Recommended £",
    "Premium £", "Suggested Increase", "Tommy Score", "Decision", "AI Notes",
    "Actual Hours", "Actual Materials £", "Actual Consumables £",
    "Actual Extra Costs £", "Job Contribution £ (actual costs where entered)",
    "Realised £/hr", "Realised Rate Status", "Pricing Lesson", "Quoted Hours",
    "Hours Variance", "Variance %", "Variance Reason", "Overrun Type",
    "Comparable Completed Jobs", "Avg Actual Hours — Comparable", "Learned Hours",
    "Learning Status", "Commercial Context", "Commercial Recommendation",
    "Scheduled Date", "Suggested Deposit £", "Deposit Received £", "Deposit Status",
    "Balance Outstanding £", "Payment Due Date", "Payment Status", "Quote Expiry",
    "Follow-up Status", "Cash Expected Date", "Overrun Gate", "Chargeable Variation?",
    "Variation Approved £", "Rework Hours", "Rework Materials £", "Rework Loss £",
    "QC Passed?", "Job Intervention", "Overrun Action", "Customer Payment Risk",
    "Risk Reason / Notes", "Risk-Adjusted Deposit £", "Payment Protection Action",
    "Job Location", "Pricing Method Override", "Agreed Customer Cap £",
    "Customer Approval Status", "Customer Approval / Variation Notes",
    "Mobile Visit Count", "One-way Travel Minutes (Maps/Auto)",
    "Manual Travel-Time £ Override", "Travel-Time Recovery £ (Internal)",
    "Total Business Hours incl Travel", "Effective Customer £ / Business Hour",
    "Travel-Time Billing Status", "Mileage Rate £ Used / Snapshot",
    "Rust / Corrosion Present?", "Agreement Requirement Override",
    "Agreement Required?", "Recommended Agreement", "Agreement Status",
    "Signed Agreement", "Work Release Gate",
)


def read_full_width(token):
    from hq_historical_preview import SHEETS_BASE, WORKBOOK
    headers = {"Authorization": "Bearer " + token}
    response = requests.get(SHEETS_BASE, headers=headers, timeout=(3, 15), params={
        "fields": "spreadsheetId,sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"
    })
    response.raise_for_status()
    meta = response.json()
    jobs = [s.get("properties", {}) for s in meta.get("sheets", [])
            if s.get("properties", {}).get("title") == "Jobs"]
    if (meta.get("spreadsheetId") != WORKBOOK or len(jobs) != 1
            or jobs[0].get("sheetId") != 330634230
            or jobs[0].get("gridProperties", {}).get("rowCount") != 1000
            or jobs[0].get("gridProperties", {}).get("columnCount") != WIDTH):
        raise ValueError("Full historical Jobs bounds changed")
    snapshots = []
    for mode in ("FORMULA", "UNFORMATTED_VALUE"):
        response = requests.get(SHEETS_BASE + "/values:batchGet", headers=headers,
                                timeout=(3, 15), params=[("ranges", RANGE),
                                ("valueRenderOption", mode)])
        response.raise_for_status()
        snapshots.append(response.json())
    return snapshots


def _full_rows(snapshot):
    from hq_historical_preview import WORKBOOK
    if not isinstance(snapshot, dict) or snapshot.get("spreadsheetId") != WORKBOOK:
        raise ValueError("Full-width workbook identity not verified")
    ranges = snapshot.get("valueRanges")
    if not isinstance(ranges, list) or len(ranges) != 1 or not isinstance(ranges[0], dict):
        raise ValueError("Full-width source missing")
    item = ranges[0]
    if item.get("range") not in (RANGE, "Jobs!A4:CP1000"):
        raise ValueError("Full-width range not verified")
    rows = item.get("values")
    if (not isinstance(rows, list) or not 2 <= len(rows) <= 997
            or any(not isinstance(row, list) or len(row) > WIDTH for row in rows)
            or tuple(rows[0]) != HEADERS or len(HEADERS) != WIDTH):
        raise ValueError("Full-width header or row bounds changed")
    return rows


def _pad(row, width):
    return (list(row[:width]) + [""] * width)[:width]


def _digest(value):
    from hq_historical_preview import WORKBOOK
    key = current_app.secret_key
    if not isinstance(key, str) or not key:
        raise RuntimeError("Persistent owner key required")
    data = json.dumps(["historical-jobs-evidence-v2", WORKBOOK, 330634230, RANGE, value],
                      ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()


def evidence_for_items(items, legacy_formula, legacy_computed, full_formula, full_computed):
    from hq_historical_preview import _check_source, _ranges, _verified_source_candidate
    legacy_raw, _ = _check_source(legacy_formula, legacy_computed)
    legacy_values, _ = _ranges(legacy_computed)
    full_raw, full_values = _full_rows(full_formula), _full_rows(full_computed)
    # Compare every row's literal identity across all four reads. Extra financial
    # cells on otherwise blank rows do not silently create historical identities.
    def ids(rows):
        return [_pad(rows[i], 1)[0] if i < len(rows) else ""
                for i in range(1, 997)]
    if not (ids(legacy_raw) == ids(legacy_values) == ids(full_raw) == ids(full_values)):
        raise ValueError("Historical identities changed across reads")
    if (_pad(legacy_raw[0], 52) != full_raw[0][:52]
            or _pad(legacy_values[0], 52) != full_values[0][:52]):
        raise ValueError("Legacy and full header disagree")
    evidence = []
    for item in items:
        row, ref = item["sheet_row"], item["job_ref"]
        _verified_source_candidate(legacy_formula, legacy_computed, row, ref, item["source_stamp"])
        i = row - 4
        if i >= len(full_raw) or i >= len(full_values):
            raise ValueError("Full-width row missing")
        if (_pad(legacy_raw[i], 52) != _pad(full_raw[i], 52)
                or _pad(legacy_values[i], 52) != _pad(full_values[i], 52)):
            raise ValueError("Source changed between legacy and full reads")
        evidence.append([item["candidate_id"], row, ref,
                         _digest([row, ref, HEADERS, _pad(full_raw[i], WIDTH),
                                  _pad(full_values[i], WIDTH)])])
    return json.dumps(evidence, ensure_ascii=True, separators=(",", ":"))


def save_full_width_evidence(batch_id, *, legacy_formula, legacy_computed,
                             full_formula, full_computed, owner_identity):
    from hq_historical_preview import _HEX
    if not storage_ready() or not isinstance(batch_id, str) or not _HEX.fullmatch(batch_id):
        raise ValueError("Invalid review batch")
    actor = _actor_fingerprint(owner_identity)
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_job_batch_full_evidence (
            batch_id TEXT PRIMARY KEY REFERENCES hq_historical_job_batches(id),
            version INTEGER NOT NULL CHECK(version=2), evidence TEXT NOT NULL,
            actor_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("BEGIN IMMEDIATE")
        evidence = verified_batch_evidence(db, batch_id, legacy_formula, legacy_computed,
                                           full_formula, full_computed)
        prior = db.execute("SELECT evidence FROM hq_historical_job_batch_full_evidence WHERE batch_id=?",
                           (batch_id,)).fetchone()
        if prior:
            if not hmac.compare_digest(prior["evidence"], evidence):
                raise ValueError("Full-width evidence changed; archive and restage batch")
            return
        db.execute("""INSERT INTO hq_historical_job_batch_full_evidence
            (batch_id,version,evidence,actor_fingerprint,created_at) VALUES (?,2,?,?,?)""",
            (batch_id, evidence, actor, now))


def verified_batch_evidence(db, batch_id, legacy_formula, legacy_computed, full_formula, full_computed):
    """Revalidate an active batch inside the caller's write transaction."""
    batch = db.execute("SELECT revision,archived_at FROM hq_historical_job_batches WHERE id=?",
                       (batch_id,)).fetchone()
    if not batch or batch["revision"] != 0 or batch["archived_at"] is not None:
        raise ValueError("Only active batches can receive evidence")
    items = [dict(r) for r in db.execute("""SELECT i.*,c.source_stamp AS live_stamp,
        c.withdrawn_at,c.id AS live_candidate,w.revision AS live_revision,
        EXISTS(SELECT 1 FROM hq_work_archive_state a WHERE a.work_id=i.work_id
               AND a.is_archived=1) AS work_archived
        FROM hq_historical_job_batch_items i
        LEFT JOIN hq_historical_j_candidates c ON c.id=i.candidate_id
        LEFT JOIN hq_work_records w ON w.id=i.work_id
        WHERE i.batch_id=? ORDER BY i.sheet_row""", (batch_id,))]
    if not 1 <= len(items) <= 20 or any(
            not r["live_candidate"] or r["withdrawn_at"] is not None
            or r["live_revision"] != r["work_revision"] or r["work_archived"]
            or r["live_stamp"] != r["source_stamp"] for r in items):
        raise ValueError("Batch contains stale proposals")
    evidence = evidence_for_items(items, legacy_formula, legacy_computed, full_formula, full_computed)
    return evidence
