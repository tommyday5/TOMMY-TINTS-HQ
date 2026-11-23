"""Owner-only, persistent field-by-field review of historical Customers candidates.

This is an INACTIVE reconciliation worksheet, not an import, merge, approval
of customer identity, source-of-truth change, or authorization to edit Sheets.
No customer PII or unkeyed source values are stored in these review tables.
"""
import hmac
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from flask import abort, current_app, redirect, render_template, request, session, url_for

from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_core_records import _schema
from hq_customer_postcodes import postcode_status
from hq_work_progress import _actor_fingerprint

HEX = re.compile(r"[0-9a-f]{32}\Z")
REV = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z")
DECISIONS = {
    "name": ("unreviewed", "candidate_agreement", "requires_followup"),
    "account_type": ("unreviewed", "candidate_agreement", "requires_followup", "not_mapped"),
    "postcode": ("unreviewed", "requires_followup", "not_mapped"),
}
FIELDS = tuple(DECISIONS)


def _helpers():
    # Delayed import avoids a circular import when attaching to the established
    # owner-only historical customer blueprint during application startup.
    import hq_historical_customer_links as original
    return original


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_customer_field_reviews (
        candidate_id TEXT PRIMARY KEY REFERENCES hq_historical_customer_candidates(id),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        source_stamp TEXT NOT NULL,
        native_stamp TEXT NOT NULL,
        tier_revision INTEGER NOT NULL,
        name_decision TEXT NOT NULL,
        account_type_decision TEXT NOT NULL,
        postcode_decision TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_customer_field_events (
        id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL REFERENCES hq_historical_customer_candidates(id),
        revision INTEGER NOT NULL,
        name_decision TEXT NOT NULL,
        account_type_decision TEXT NOT NULL,
        postcode_decision TEXT NOT NULL,
        actor_fingerprint TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        UNIQUE(candidate_id,revision)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_historical_customer_field_operations (
        operation_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL REFERENCES hq_historical_customer_candidates(id),
        request_stamp TEXT NOT NULL,
        result_revision INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )""")


def _tier(db, customer_id):
    exists = db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
        AND name='hq_customer_account_types'""").fetchone()
    if not exists:
        return {"tier": "retail", "revision": 0, "reviewed": False}
    row = db.execute("""SELECT tier, revision FROM hq_customer_account_types
        WHERE customer_id=?""", (customer_id,)).fetchone()
    return ({"tier": row["tier"], "revision": row["revision"], "reviewed": True}
            if row else {"tier": "retail", "revision": 0, "reviewed": False})


def _candidate_context(db, candidate_id, formula, computed):
    original = _helpers()
    candidate = db.execute("""SELECT id, customer_id, sheet_row, source_stamp,
        native_stamp, withdrawn_at, revision
        FROM hq_historical_customer_candidates WHERE id=?""", (candidate_id,)).fetchone()
    if candidate is None:
        raise LookupError("Historical customer candidate unavailable")
    if candidate["withdrawn_at"] is not None or candidate["revision"] != 0:
        raise ValueError("Historical customer candidate was withdrawn")
    source_raw, _ = original._source_row(formula, computed, candidate["sheet_row"])
    actual_source = original.source_stamp(formula, computed, candidate["sheet_row"])
    if not hmac.compare_digest(actual_source, candidate["source_stamp"]):
        raise ValueError("Customers sheet source changed; withdraw and review again")
    native = original._native_row(db, candidate["customer_id"])
    if native is None:
        raise ValueError("HQ customer is missing or archived")
    actual_native = original._native_digest(native)
    if not hmac.compare_digest(actual_native, candidate["native_stamp"]):
        raise ValueError("HQ customer identity changed; withdraw and review again")
    return candidate, source_raw, native, _tier(db, candidate["customer_id"])


def _read_snapshot(candidate_id, formula, computed):
    if not isinstance(candidate_id, str) or not HEX.fullmatch(candidate_id):
        raise LookupError("Candidate unavailable")
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _helpers()._schema_candidates(db)
        _tables(db)
        db.execute("BEGIN")
        candidate, raw, native, tier = _candidate_context(db, candidate_id, formula, computed)
        saved = db.execute("""SELECT revision, name_decision, account_type_decision,
            postcode_decision, tier_revision FROM hq_historical_customer_field_reviews
            WHERE candidate_id=?""", (candidate_id,)).fetchone()
        events = [dict(row) for row in db.execute("""SELECT revision,name_decision,
            account_type_decision,postcode_decision,occurred_at FROM
            hq_historical_customer_field_events WHERE candidate_id=?
            ORDER BY revision DESC LIMIT 15""", (candidate_id,))]
        return {
            "candidate_id": candidate_id, "row": candidate["sheet_row"],
            "customer_id": candidate["customer_id"],
            "source_name": str(raw[0]), "source_type": str(raw[1]) if len(raw)>1 else "",
            "source_postcode": str(raw[2]) if len(raw)>2 else "",
            "native_name": native["name"], "tier": tier,
            "native_postcode": postcode_status(db, candidate["customer_id"]),
            "revision": saved["revision"] if saved else 0,
            "name_decision": saved["name_decision"] if saved else "unreviewed",
            "account_type_decision": saved["account_type_decision"] if saved else "unreviewed",
            "postcode_decision": saved["postcode_decision"] if saved else "unreviewed",
            "tier_changed": bool(saved and saved["tier_revision"] != tier["revision"]),
            "events": events,
        }


def save_field_review(*, candidate_id, formula, computed, decisions, expected_revision,
                      operation_id, owner_identity):
    """Atomically save a PII-free versioned review; canonical data stays intact."""
    if not storage_ready():
        raise ValueError("Durable HQ storage unavailable")
    if not isinstance(candidate_id, str) or not HEX.fullmatch(candidate_id):
        raise ValueError("Invalid candidate")
    if not isinstance(operation_id, str) or not HEX.fullmatch(operation_id):
        raise ValueError("Invalid submission")
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**63-1:
        raise ValueError("Invalid review revision")
    if not isinstance(decisions, dict) or set(decisions) != set(FIELDS):
        raise ValueError("All fields require a review decision")
    if any(decisions[field] not in DECISIONS[field] for field in FIELDS):
        raise ValueError("Invalid field decision")
    actor = _actor_fingerprint(owner_identity)
    original = _helpers()
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        original._schema_candidates(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        candidate, raw, native, tier = _candidate_context(db, candidate_id, formula, computed)
        if decisions["name"] == "candidate_agreement" and (
                original._norm(raw[0]) != original._norm(native["name"])):
            raise ValueError("Different customer names cannot be marked consistent")
        if decisions["account_type"] == "candidate_agreement":
            alias = {"trader": "trade", "trade": "trade", "retail": "retail",
                     "maintenance": "maintenance"}
            if not tier["reviewed"] or alias.get(original._norm(raw[1]) if len(raw)>1 else "") != tier["tier"]:
                raise ValueError("Unverified or different tiers cannot be marked consistent")
        stamp = original._stamp([candidate_id, candidate["source_stamp"],
                                 candidate["native_stamp"], tier["revision"],
                                 expected_revision, decisions, actor])
        prior = db.execute("""SELECT candidate_id,request_stamp,result_revision FROM
            hq_historical_customer_field_operations WHERE operation_id=?""",
            (operation_id,)).fetchone()
        if prior:
            if prior["candidate_id"] != candidate_id or not hmac.compare_digest(
                    prior["request_stamp"], stamp):
                raise ValueError("Submission reused with different review data")
            return prior["result_revision"]
        existing = db.execute("""SELECT revision, name_decision, account_type_decision,
            postcode_decision, tier_revision FROM hq_historical_customer_field_reviews
            WHERE candidate_id=?""", (candidate_id,)).fetchone()
        revision = existing["revision"] if existing else 0
        if revision != expected_revision:
            raise ValueError("Field review changed; reload before saving")
        if (existing and all(existing[field+"_decision"] == decisions[field]
                             for field in FIELDS)
                and existing["tier_revision"] == tier["revision"]):
            resulting_revision = revision
        else:
            resulting_revision = revision + 1
            now = datetime.now(timezone.utc).isoformat()
            db.execute("""INSERT INTO hq_historical_customer_field_reviews
                (candidate_id,revision,source_stamp,native_stamp,tier_revision,
                 name_decision,account_type_decision,postcode_decision,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                revision=excluded.revision,source_stamp=excluded.source_stamp,
                native_stamp=excluded.native_stamp,tier_revision=excluded.tier_revision,
                name_decision=excluded.name_decision,
                account_type_decision=excluded.account_type_decision,
                postcode_decision=excluded.postcode_decision,
                updated_at=excluded.updated_at""",
                (candidate_id,resulting_revision,candidate["source_stamp"],
                 candidate["native_stamp"],tier["revision"],
                 decisions["name"],decisions["account_type"],decisions["postcode"],now))
            db.execute("""INSERT INTO hq_historical_customer_field_events
                VALUES(?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex,candidate_id,resulting_revision,
                 decisions["name"],decisions["account_type"],decisions["postcode"],
                 actor,now))
        db.execute("""INSERT INTO hq_historical_customer_field_operations
            VALUES(?,?,?,?,?)""", (operation_id,candidate_id,stamp,resulting_revision,
                                  datetime.now(timezone.utc).isoformat()))
        return resulting_revision


def register_routes(blueprint):
    @blueprint.route("/hq/migration/customers/<candidate_id>/fields", methods=["GET","POST"])
    @required
    def customer_field_review(candidate_id):
        if not HEX.fullmatch(candidate_id):
            abort(404)
        if request.method == "POST":
            if request.content_length is None or request.content_length > 3072:
                abort(413)
            if not secrets.compare_digest(request.form.get("csrf",""),
                                          session.get("csrf","not-set")):
                abort(403)
            raw_revision = request.form.get("revision","")
            if not REV.fullmatch(raw_revision):
                abort(400)
        try:
            provider = current_app.extensions["hq_sheets_connection"]()
            if not provider or provider[1] <= time.time():
                raise ValueError("Connect owner Google Sheets read-only access first")
            formula, computed = _helpers()._read_live(provider[0])
            if request.method == "POST":
                save_field_review(
                    candidate_id=candidate_id, formula=formula, computed=computed,
                    decisions={field: request.form.get(field+"_decision","")
                               for field in FIELDS},
                    expected_revision=int(raw_revision),
                    operation_id=request.form.get("operation_id",""),
                    owner_identity=session.get("google_sub"))
                return redirect(url_for(".customer_field_review",
                                        candidate_id=candidate_id,saved="1"), code=303)
            snapshot = _read_snapshot(candidate_id, formula, computed)
        except LookupError:
            abort(404)
        except ValueError as exc:
            return render_template("hq_historical_customer_fields.html",
                error=str(exc), snapshot=None, candidate_id=candidate_id,
                csrf=session.get("csrf",""), saved=False, options=DECISIONS), 409
        except (sqlite3.Error,OSError):
            current_app.logger.warning("Historical customer field review storage unavailable")
            abort(503)
        except Exception:
            current_app.logger.warning("Historical customer field review source unavailable")
            abort(503)
        return render_template("hq_historical_customer_fields.html",
            snapshot=snapshot, candidate_id=candidate_id,
            csrf=session.get("csrf",""), saved=request.args.get("saved")=="1",
            error=None, options=DECISIONS, operation_id=uuid.uuid4().hex)
