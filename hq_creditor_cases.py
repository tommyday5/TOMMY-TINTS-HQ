"""Private, versioned creditor case planning. No provider writes or debt admission.

Amounts and arrangement status are owner observations, not verified liability,
payment, affordability calculation or legal conclusion. Historical Sheets
records stay separate until an explicitly approved mapping and reconciliation.
"""
from contextlib import closing
from datetime import date, datetime, timezone
import json
import re
import secrets
import sqlite3
import uuid

from flask import abort, redirect, render_template, request, session, url_for
from customer_request_store import connect, storage_ready
from hq_auth import required
from hq_work_progress import _actor_fingerprint

ID = re.compile(r"[0-9a-f]{32}\Z")
GBP = re.compile(r"[0-9]{1,7}(?:\.[0-9]{1,2})?\Z")
SCOPES = ("unknown", "business", "personal", "joint_or_guarantee")
STATUSES = ("unreviewed", "disputed", "arrangement_proposed", "arrangement_reported",
            "hold_requested", "hold_reported", "closed_reported")
INCLUSION = ("unknown", "include", "exclude")
AMOUNTS = ("claimed_balance", "current_monthly", "proposed_monthly", "committed_monthly")
TEXT = {"creditor": 160, "reference": 120, "evidence": 160, "notes": 500}


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_creditor_cases (
        id TEXT PRIMARY KEY, revision INTEGER NOT NULL, identity TEXT NOT NULL,
        state TEXT NOT NULL, archived INTEGER NOT NULL CHECK(archived IN (0,1)),
        updated_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_creditor_case_events (
        operation_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES hq_creditor_cases(id),
        revision INTEGER NOT NULL, actor TEXT NOT NULL, state TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(case_id,revision))""")


def _amount(raw):
    if raw == "":
        return None
    if not isinstance(raw, str) or not GBP.fullmatch(raw):
        raise ValueError("Use pounds and up to two decimal places, or leave unknown.")
    whole, _, decimal = raw.partition(".")
    return int(whole)*100 + int((decimal+"00")[:2])


def _fields(form):
    state = {}
    for key, limit in TEXT.items():
        value = form.get(key, "")
        if (not isinstance(value, str) or len(value.strip()) > limit
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("Check text length and characters.")
        state[key] = value.strip()
    if not state["creditor"]:
        raise ValueError("Creditor name is required.")
    for key, options in (("scope", SCOPES), ("status", STATUSES), ("inclusion", INCLUSION)):
        value = form.get(key, "")
        if value not in options:
            raise ValueError("Choose a valid case scope, status and affordability treatment.")
        state[key] = value
    for key in AMOUNTS:
        state[key] = _amount(form.get(key, ""))
    for key in ("review_date", "last_confirmed"):
        value = form.get(key, "")
        if value:
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError("Use a valid date.")
            date.fromisoformat(value)
        state[key] = value
    archived = form.get("archived", "")
    if archived not in ("", "yes"):
        raise ValueError("Invalid archive flag.")
    state["archived"] = archived == "yes"
    return state


def _norm(value):
    return " ".join(value.casefold().split())


def _identity(state):
    return json.dumps([_norm(state["creditor"]), _norm(state["reference"]), state["scope"]])


def _collision(db, identifier, state):
    # Missing references and unknown scopes are ambiguous. Never silently
    # create a second case or merge an archived one on a fuzzy match.
    name, ref, scope = json.loads(_identity(state))
    for row in db.execute("SELECT id,identity FROM hq_creditor_cases WHERE id<>?", (identifier,)):
        other_name, other_ref, other_scope = json.loads(row["identity"])
        # The same creditor/reference must not be counted twice across scopes.
        if name == other_name and (not ref or not other_ref or ref == other_ref):
            raise ValueError("Possible duplicate creditor case (including archives). Review the existing case.")


def save_case(identifier, operation, revision, form, owner):
    if not storage_ready():
        raise RuntimeError("Persistent storage required.")
    if (not isinstance(identifier, str) or not ID.fullmatch(identifier)
            or not isinstance(operation, str) or not ID.fullmatch(operation)
            or type(revision) is not int or not 0 <= revision < 2**63-1):
        raise ValueError("Invalid case or revision.")
    state = _fields(form)
    serialized = json.dumps(state, sort_keys=True, ensure_ascii=True)
    actor = _actor_fingerprint(owner)
    with closing(connect()) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        old_event = db.execute("SELECT * FROM hq_creditor_case_events WHERE operation_id=?", (operation,)).fetchone()
        if old_event:
            if (old_event["case_id"], old_event["revision"], old_event["state"], old_event["actor"]) != (
                    identifier, revision+1, serialized, actor):
                raise ValueError("Operation already used with different details.")
            return
        old = db.execute("SELECT revision FROM hq_creditor_cases WHERE id=?", (identifier,)).fetchone()
        if (old["revision"] if old else 0) != revision:
            raise ValueError("Case changed elsewhere. Reload before saving.")
        if old is None and state["archived"]:
            raise ValueError("Save a new case before archiving it.")
        _collision(db, identifier, state)
        now = datetime.now(timezone.utc).isoformat()
        if old:
            db.execute("UPDATE hq_creditor_cases SET revision=?,identity=?,state=?,archived=?,updated_at=? WHERE id=?",
                       (revision+1, _identity(state), serialized, int(state["archived"]), now, identifier))
        else:
            db.execute("INSERT INTO hq_creditor_cases VALUES(?,?,?,?,?,?)",
                       (identifier, revision+1, _identity(state), serialized, 0, now))
        db.execute("INSERT INTO hq_creditor_case_events VALUES(?,?,?,?,?,?)",
                   (operation, identifier, revision+1, actor, serialized, now))


def _record(row):
    return dict(json.loads(row["state"]), id=row["id"], revision=row["revision"], updated_at=row["updated_at"])


def snapshot(identifier=""):
    if not storage_ready():
        raise RuntimeError("Persistent storage required.")
    if identifier and not ID.fullmatch(identifier):
        raise ValueError("Invalid case reference.")
    with closing(connect()) as db, db:
        _tables(db)
        db.execute("BEGIN")
        records = [_record(r) for r in db.execute(
            "SELECT * FROM hq_creditor_cases ORDER BY archived,updated_at DESC,id LIMIT 250")]
        count = db.execute("SELECT COUNT(*) FROM hq_creditor_cases").fetchone()[0]
        if not identifier:
            return records, count, None, [], 0
        row = db.execute("SELECT * FROM hq_creditor_cases WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise LookupError("Case not found.")
        history = [dict(revision=r["revision"], created_at=r["created_at"], state=json.loads(r["state"]))
                   for r in db.execute("SELECT * FROM hq_creditor_case_events WHERE case_id=? ORDER BY revision DESC LIMIT 100",
                                       (identifier,))]
        return records, count, _record(row), history, row["revision"]


def register(bp):
    @bp.route("/hq/finance/creditors", methods=["GET", "POST"])
    @required
    def creditor_cases():
        error = None
        code = 200
        identifier = request.args.get("case", "")
        if request.method == "POST":
            if request.content_length is None or request.content_length > 8192:
                abort(413)
            csrf = session.get("csrf")
            if not csrf or not secrets.compare_digest(request.form.get("csrf", ""), csrf):
                abort(403)
            if request.form.get("confirm") != "yes":
                error, code = "Confirm that these are owner-reported details, not a verified payment or legal admission.", 400
            else:
                try:
                    revision = request.form.get("revision", "")
                    if not re.fullmatch(r"[0-9]{1,19}", revision):
                        raise ValueError("Invalid revision.")
                    save_case(request.form.get("id", ""), request.form.get("operation", ""),
                              int(revision), request.form, session.get("google_sub"))
                    response = redirect(url_for(".creditor_cases", case=request.form["id"], saved="1"), 303)
                    response.headers["Cache-Control"] = "no-store"
                    return response
                except ValueError as exc:
                    error, code = str(exc), 409
                except (sqlite3.Error, RuntimeError, OSError):
                    error, code = "Persistent finance storage unavailable; save not confirmed.", 503
        try:
            records, count, selected, history, history_count = snapshot(identifier)
        except LookupError:
            abort(404)
        except ValueError:
            abort(400)
        except (sqlite3.Error, RuntimeError, OSError):
            return "Persistent creditor storage unavailable.", 503, {"Cache-Control": "no-store"}
        if selected is None:
            selected = dict(id=uuid.uuid4().hex, revision=0, creditor="", reference="", scope="unknown",
                            status="unreviewed", inclusion="unknown", archived=False, evidence="", notes="",
                            review_date="", last_confirmed="", **{key:None for key in AMOUNTS})
        return render_template("hq_creditor_cases.html", records=records, count=count, selected=selected,
                               history=history, history_count=history_count, scopes=SCOPES, statuses=STATUSES,
                               inclusions=INCLUSION, amounts=AMOUNTS, csrf=session.get("csrf", ""),
                               operation=uuid.uuid4().hex, error=error, saved=request.args.get("saved")=="1"), code, {"Cache-Control":"no-store"}
