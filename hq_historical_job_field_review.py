"""Inactive field-level migration review plans, never an authority transfer."""
import hmac
import json
from datetime import datetime, timezone

from customer_request_store import connect, storage_ready
from hq_historical_job_full_evidence import HEADERS, verified_batch_evidence
from hq_work_progress import _actor_fingerprint

OPTIONS = (
    ("u", "Unresolved"),
    ("h", "Preserve historical evidence"),
    ("n", "Native mapping required"),
    ("f", "Formula parity required"),
    ("v", "Independent verification required"),
)
# Suggested destinations are not verified mappings. In particular the native
# draft deposit is a requested amount, never evidence of receipt.
HINTS = {
    0: "Original J-series reference; never replace canonical HQ ID",
    2: "Canonical customer relationship; identity review required",
    3: "Native customer account classification; compare separately",
    4: "Canonical vehicle relationship; registration alone is not identity",
    5: "Native service selection; multiple services need separate mapping",
    16: "Native quoted price (integer pence); not a receipt",
    17: "Native work status; do not infer payment or booking",
    52: "Requested deposit only; not money received",
    53: "Historical reported deposit; verify provider or bank evidence",
    54: "Historical deposit status; not verified settlement",
    55: "Historical balance; reconcile invoice and verified allocations",
    57: "Historical payment status; not verified settlement",
    63: "Approved variation evidence; do not silently change price",
    92: "Preserve signed agreement evidence and access restrictions",
}
# Financial evidence may be retained or independently verified, never assigned
# a native mapping or formula-only status by this advisory editor.
RECEIPT_FIELDS = {53, 54, 55, 57}


def options_for(index):
    return tuple(o for o in OPTIONS if index not in RECEIPT_FIELDS or o[0] in ("u", "h", "v"))


def field_rows(decisions=None):
    decisions = decisions if decisions is not None else ["u"] * len(HEADERS)
    return [{"index": i, "header": header, "hint": HINTS.get(i, ""),
             "options": options_for(i), "decision": decisions[i]}
            for i, header in enumerate(HEADERS)]


def _schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_job_field_review_events (
        batch_id TEXT NOT NULL REFERENCES hq_historical_job_batches(id),
        revision INTEGER NOT NULL CHECK(revision>0),
        evidence_version INTEGER NOT NULL CHECK(evidence_version=2),
        decisions TEXT NOT NULL, actor_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(batch_id, revision))""")


def read_plan(db, batch_id):
    if not db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
        AND name='hq_historical_job_field_review_events'""").fetchone():
        return None
    row = db.execute("""SELECT revision,decisions,created_at FROM
        hq_historical_job_field_review_events WHERE batch_id=?
        ORDER BY revision DESC LIMIT 1""", (batch_id,)).fetchone()
    if not row:
        return None
    decisions = json.loads(row["decisions"])
    return {"revision": row["revision"], "fields": field_rows(decisions),
            "reviewed": sum(d != "u" for d in decisions), "created_at": row["created_at"]}


def save_plan(batch_id, expected_revision, decisions, *, legacy_formula, legacy_computed,
              full_formula, full_computed, owner_identity):
    from hq_historical_preview import _HEX
    if (not storage_ready() or not isinstance(batch_id, str) or not _HEX.fullmatch(batch_id)
            or type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1
            or not isinstance(decisions, list) or len(decisions) != len(HEADERS)
            or any(d not in {o[0] for o in options_for(i)} for i, d in enumerate(decisions))):
        raise ValueError("Invalid field review")
    payload = json.dumps(decisions, separators=(",", ":"))
    actor = _actor_fingerprint(owner_identity)
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        fresh = verified_batch_evidence(db, batch_id, legacy_formula, legacy_computed,
                                        full_formula, full_computed)
        proof = db.execute("""SELECT evidence FROM hq_historical_job_batch_full_evidence
            WHERE batch_id=? AND version=2""", (batch_id,)).fetchone()
        if not proof or not hmac.compare_digest(proof["evidence"], fresh):
            raise ValueError("Saved source evidence missing or changed")
        prior = db.execute("""SELECT revision,decisions FROM hq_historical_job_field_review_events
            WHERE batch_id=? ORDER BY revision DESC LIMIT 1""", (batch_id,)).fetchone()
        revision = prior["revision"] if prior else 0
        if prior and prior["decisions"] == payload and expected_revision <= revision:
            return revision
        if expected_revision != revision:
            raise ValueError("Field review changed; reload before saving")
        db.execute("""INSERT INTO hq_historical_job_field_review_events
            VALUES(?,?,?,?,?,?)""", (batch_id, revision+1, 2, payload, actor, now))
        return revision+1
