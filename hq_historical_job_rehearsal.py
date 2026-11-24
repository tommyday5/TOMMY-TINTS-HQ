"""Bounded, owner-triggered historical mapping rehearsal on an ephemeral SQLite copy.

No canonical writes, provider calls, workbook writes or durable backup claims.
"""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import time

MAX_BYTES = 16 * 1024 * 1024
MAX_SECONDS = 5


def _snapshot(db, work_id):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_native_work_details'").fetchone():
        row = db.execute("""SELECT w.*,d.customer_tier FROM hq_work_records w
            LEFT JOIN hq_native_work_details d ON d.work_id=w.id WHERE w.id=?""",
            (work_id,)).fetchone()
    else:
        row = db.execute("SELECT w.*,NULL AS customer_tier FROM hq_work_records w WHERE w.id=?",
                         (work_id,)).fetchone()
    if row is None:
        raise LookupError("Native work missing")
    return dict(row)


def rehearse_mapping(batch_id, candidate_id):
    from hq_historical_job_quote_mapping import _context
    from hq_core_records import _audit_work

    root = os.environ.get("HQ_DATA_DIR", "")
    path = Path(root) / "customer_requests.sqlite3"
    if not root or not path.is_absolute() or not path.is_file():
        raise LookupError("Canonical database unavailable")
    deadline = time.monotonic() + MAX_SECONDS

    def check_deadline():
        if time.monotonic() >= deadline:
            raise ValueError("Rehearsal time limit exceeded")

    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=1)) as source, \
         closing(sqlite3.connect(":memory:")) as copy:
        source.execute("PRAGMA query_only=ON")
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
        if source.execute("PRAGMA page_count").fetchone()[0] * page_size > MAX_BYTES:
            raise ValueError("Database too large for in-memory rehearsal")

        def progress(status, remaining, total):
            check_deadline()
            if total * page_size > MAX_BYTES:
                raise ValueError("Database too large for in-memory rehearsal")

        source.backup(copy, pages=64, progress=progress, sleep=0.01)
        copy.row_factory = sqlite3.Row
        copy.execute("PRAGMA foreign_keys=ON")
        copy.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        if copy.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
            raise ValueError("Copied database integrity check failed")
        ctx = _context(copy, batch_id, candidate_id)
        if not ctx["current"]:
            raise ValueError("Stale or archived mapping")
        work_id = ctx["row"]["work_id"]
        before = _snapshot(copy, work_id)
        if before["status"] != "draft":
            raise ValueError("Only draft work can be rehearsed")
        updates = {}
        if ctx["history"] and ctx["history"][0]["decision"] == "propose":
            if not ctx["eligible"] or ctx["history"][0]["source_pence"] != ctx["source_pence"]:
                raise ValueError("Quote proposal no longer eligible")
            updates["price_pence"] = ctx["source_pence"]
        for item in ctx["classification"]:
            if item["history"] and item["history"][0]["decision"] == "propose":
                target = item["history"][0]["target"]
                if not item["eligible"] or target not in item["choices"]:
                    raise ValueError("Classification proposal no longer eligible")
                updates["service" if item["field"] == "service" else "customer_tier"] = target
        excluded = [item["label"] for item in ctx["actual"]["fields"]
                    if item["history"] and item["history"][0]["decision"] == "propose"]
        if not updates:
            raise ValueError("Save an eligible price, service or tier proposal first")
        if updates.get("price_pence", before["price_pence"]) is not None and (
                before["deposit_pence"] > updates.get("price_pence", before["price_pence"])):
            raise ValueError("Proposed price is below existing planned deposit")
        changed = {k:v for k,v in updates.items() if before[k] != v}
        audit_before = copy.execute("SELECT COUNT(*) FROM hq_work_audit WHERE work_id=?", (work_id,)).fetchone()[0]
        copy.execute("BEGIN IMMEDIATE")
        try:
            if changed:
                count = copy.execute("""UPDATE hq_work_records SET price_pence=?,service=?,revision=revision+1
                    WHERE id=? AND revision=? AND status='draft'""",
                    (updates.get("price_pence",before["price_pence"]),updates.get("service",before["service"]),
                     work_id,before["revision"])).rowcount
                if count != 1:
                    raise ValueError("Copied work revision changed")
                if "customer_tier" in changed:
                    if copy.execute("UPDATE hq_native_work_details SET customer_tier=? WHERE work_id=?",
                                    (changed["customer_tier"],work_id)).rowcount != 1:
                        raise ValueError("Native tier missing")
                _audit_work(copy,work_id,before["revision"]+1,"historical_rehearsal",
                            {"changed_fields":sorted(changed)},{"changed_fields":sorted(changed)})
            after = _snapshot(copy,work_id)
            expected = dict(before, **changed)
            expected["revision"] += bool(changed)
            if after != expected:
                raise ValueError("Rehearsal changed unexpected work fields")
            if copy.execute("SELECT COUNT(*) FROM hq_work_audit WHERE work_id=?", (work_id,)).fetchone()[0] != audit_before + bool(changed):
                raise ValueError("Copied audit verification failed")
            check_deadline()
        finally:
            copy.rollback()
        if _snapshot(copy,work_id) != before or copy.execute(
                "SELECT COUNT(*) FROM hq_work_audit WHERE work_id=?", (work_id,)).fetchone()[0] != audit_before:
            raise ValueError("Copied rollback verification failed")
        return {"fields":[{"label":k,"before":before[k],"after":after[k]} for k in updates],
                "revision_before":before["revision"],"revision_after":after["revision"],
                "excluded":excluded,"rollback_verified":True,"copy_integrity_checked":True}
