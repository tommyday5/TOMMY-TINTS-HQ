"""Private customer availability requests tied to canonical HQ work records.

A request or owner's tentative offer is NOT a confirmed appointment.
This module never writes Google Sheets, Calendar, Square, finance or messages.
"""
import re
import secrets
import sqlite3
import time

import requests
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for

from customer_request_store import connect
from hq_auth import required
from hq_core_records import _schema
from hq_customer_portal import active_customer
from hq_google_connections import stored_grant_present
from maintenance_calendar_google import live_conflicts

availability_requests = Blueprint("availability_requests", __name__)
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TIMES = frozenset(f"{hour:02d}:00" for hour in range(9, 16))
_MAX_PENDING = 8


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid canonical HQ reference")
    return value


def _slot(day, hour):
    """Validate a requested preference, not availability or a reservation."""
    if not isinstance(day, str) or not _DATE.fullmatch(day) or hour not in _TIMES:
        raise ValueError("Select a valid preferred date and hour")
    try:
        selected = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Select a valid preferred date") from exc
    today = datetime.now(ZoneInfo("Europe/London")).date()
    if not today <= selected <= today + timedelta(days=365):
        raise ValueError("Select a date within the next 365 days")
    return day, hour


def _note(value, maximum=500):
    if not isinstance(value, str):
        raise ValueError("Invalid request message")
    value = value.strip()
    if len(value) > maximum or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError("Message too long or contains invalid characters")
    return value


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_portal_availability_requests (
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE,
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        work_id TEXT NOT NULL REFERENCES hq_work_records(id),
        requested_date TEXT NOT NULL, requested_time TEXT NOT NULL,
        note TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'requested'
            CHECK(status IN ('requested','offered','declined','withdrawn')),
        offered_date TEXT, offered_time TEXT, owner_message TEXT,
        revision INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_availability_customer
        ON hq_portal_availability_requests(customer_id,created_at)""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_availability_owner
        ON hq_portal_availability_requests(status,created_at)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_portal_availability_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL REFERENCES hq_portal_availability_requests(id),
        revision INTEGER NOT NULL,
        actor TEXT NOT NULL CHECK(actor IN ('customer','owner')),
        action TEXT NOT NULL CHECK(action IN ('requested','offered','declined','withdrawn')),
        occurred_at TEXT NOT NULL
    )""")

    # A separate immutable receipt avoids changing the CHECK constraint on older
    # production availability tables; these are responses, NOT bookings.
    db.execute("""CREATE TABLE IF NOT EXISTS hq_portal_availability_offer_responses (
        request_id TEXT PRIMARY KEY REFERENCES hq_portal_availability_requests(id),
        offer_revision INTEGER NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('accepted','declined')),
        responded_at TEXT NOT NULL
    )""")
    # Separate native ledger: historical tentative-offer constraints remain intact.
    db.execute("""CREATE TABLE IF NOT EXISTS hq_portal_booking_confirmations (
        request_id TEXT PRIMARY KEY REFERENCES hq_portal_availability_requests(id),
        customer_id TEXT NOT NULL REFERENCES hq_customers(id),
        work_id TEXT NOT NULL REFERENCES hq_work_records(id),
        offered_revision INTEGER NOT NULL,
        booked_date TEXT NOT NULL, booked_time TEXT NOT NULL,
        duration_hours INTEGER NOT NULL CHECK(duration_hours BETWEEN 1 AND 8),
        status TEXT NOT NULL CHECK(status IN ('confirmed','cancelled')),
        confirmation_note TEXT NOT NULL, cancel_reason TEXT NOT NULL DEFAULT '',
        external_calendar_attested INTEGER NOT NULL CHECK(external_calendar_attested=1),
        revision INTEGER NOT NULL DEFAULT 0,
        confirmed_at TEXT NOT NULL, cancelled_at TEXT
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS hq_booking_day
        ON hq_portal_booking_confirmations(booked_date,status)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_portal_booking_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL REFERENCES hq_portal_booking_confirmations(request_id),
        revision INTEGER NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('confirmed','cancelled')),
        occurred_at TEXT NOT NULL
    )""")
    # Optional evidence attached only to an owner-confirmed booking after a
    # successful, fresh, read-only Google primary-calendar check.
    db.execute("""CREATE TABLE IF NOT EXISTS hq_portal_booking_calendar_evidence (
        request_id TEXT PRIMARY KEY REFERENCES hq_portal_booking_confirmations(request_id),
        provider TEXT NOT NULL CHECK(provider='google_calendar_primary'),
        checked_at TEXT NOT NULL,
        slot_start TEXT NOT NULL,
        slot_end TEXT NOT NULL
    )""")


def submit_availability_request(customer_id, work_id, day, hour, note, operation_id):
    _id(customer_id)
    _id(work_id)
    _id(operation_id)
    day, hour = _slot(day, hour)
    note = _note(note)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("""SELECT id,customer_id,work_id,requested_date,requested_time,note
            FROM hq_portal_availability_requests WHERE operation_id=?""",
            (operation_id,)).fetchone()
        if previous is not None:
            if (previous["customer_id"],previous["work_id"],previous["requested_date"],
                previous["requested_time"],previous["note"]) != (customer_id,work_id,day,hour,note):
                raise ValueError("This submission reference has already been used")
            return previous["id"]
        eligible = db.execute("""SELECT 1 FROM hq_work_records w
            JOIN hq_vehicles v ON v.id=w.vehicle_id AND v.customer_id=w.customer_id
            WHERE w.id=? AND w.customer_id=?
              AND w.status IN ('approved','in_progress')
              AND NOT EXISTS (SELECT 1 FROM hq_work_archive_state a
                  WHERE a.work_id=w.id AND a.is_archived=1)""",
            (work_id,customer_id)).fetchone()
        if eligible is None:
            raise ValueError("Only your approved active work can have an availability request")
        if db.execute("""SELECT 1 FROM hq_portal_booking_confirmations
            WHERE work_id=? AND status='confirmed'""",(work_id,)).fetchone():
            raise ValueError("This work already has an HQ-confirmed appointment")
        count = db.execute("""SELECT COUNT(*) FROM hq_portal_availability_requests a
            WHERE a.customer_id=? AND a.status IN ('requested','offered')
              AND NOT EXISTS (SELECT 1 FROM hq_portal_booking_confirmations b
                  WHERE b.request_id=a.id)
              AND NOT EXISTS (SELECT 1 FROM hq_portal_availability_offer_responses r
                  WHERE r.request_id=a.id AND r.decision='declined')""",
            (customer_id,)).fetchone()[0]
        if count >= _MAX_PENDING:
            raise ValueError("Too many open availability requests")
        existing = db.execute("""SELECT a.id FROM hq_portal_availability_requests a
            WHERE a.customer_id=? AND a.work_id=? AND a.status IN ('requested','offered')
              AND NOT EXISTS (SELECT 1 FROM hq_portal_booking_confirmations b
                  WHERE b.request_id=a.id)
              AND NOT EXISTS (SELECT 1 FROM hq_portal_availability_offer_responses r
                  WHERE r.request_id=a.id AND r.decision='declined')""",
            (customer_id,work_id)).fetchone()
        if existing:
            raise ValueError("A request for this job is already awaiting review")
        ref, now = uuid.uuid4().hex, _now()
        db.execute("""INSERT INTO hq_portal_availability_requests
            (id,operation_id,customer_id,work_id,requested_date,requested_time,note,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (ref,operation_id,customer_id,work_id,day,hour,note,now,now))
        db.execute("""INSERT INTO hq_portal_availability_events
            (request_id,revision,actor,action,occurred_at)
            VALUES (?,0,'customer','requested',?)""",(ref,now))
        return ref


def customer_availability_snapshot(customer_id):
    _id(customer_id)
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        works = [dict(r) for r in db.execute("""SELECT w.id,w.service,v.registration
            FROM hq_work_records w
            JOIN hq_vehicles v ON v.id=w.vehicle_id AND v.customer_id=w.customer_id
            WHERE w.customer_id=? AND w.status IN ('approved','in_progress')
              AND NOT EXISTS (SELECT 1 FROM hq_work_archive_state a
                  WHERE a.work_id=w.id AND a.is_archived=1)
              AND NOT EXISTS (SELECT 1 FROM hq_portal_booking_confirmations b
                  WHERE b.work_id=w.id AND b.status='confirmed')
            ORDER BY w.created_at DESC LIMIT 100""",(customer_id,))]
        requests = [dict(r) for r in db.execute("""SELECT a.id,a.work_id,a.requested_date,
            a.requested_time,a.note,a.status,a.offered_date,a.offered_time,a.owner_message,
            a.revision,a.created_at,w.service,v.registration,
            r.decision AS customer_decision,r.responded_at AS customer_responded_at,
            b.status AS booking_status,b.duration_hours,b.confirmed_at,b.cancelled_at,
            b.revision AS booking_revision,b.confirmation_note,b.cancel_reason
            FROM hq_portal_availability_requests a
            LEFT JOIN hq_portal_availability_offer_responses r ON r.request_id=a.id
            LEFT JOIN hq_portal_booking_confirmations b ON b.request_id=a.id
            JOIN hq_work_records w ON w.id=a.work_id AND w.customer_id=a.customer_id
            JOIN hq_vehicles v ON v.id=w.vehicle_id AND v.customer_id=a.customer_id
            WHERE a.customer_id=? ORDER BY a.created_at DESC,a.id DESC LIMIT 100""",
            (customer_id,))]
    return works, requests


def owner_availability_snapshot():
    with connect() as db:
        _schema(db)
        _tables(db)
        db.execute("BEGIN")
        rows = db.execute("""SELECT a.id,a.work_id,a.customer_id,a.status,a.revision,
            a.requested_date,a.requested_time,a.note,a.offered_date,a.offered_time,
            a.owner_message,a.created_at,c.name AS customer_name,w.service,v.registration,
            r.decision AS customer_decision,r.responded_at AS customer_responded_at,
            b.status AS booking_status,b.duration_hours,b.confirmed_at,b.cancelled_at,
            b.revision AS booking_revision,b.confirmation_note,b.cancel_reason,
            ce.checked_at AS primary_calendar_checked_at
            FROM hq_portal_availability_requests a
            LEFT JOIN hq_portal_availability_offer_responses r ON r.request_id=a.id
            LEFT JOIN hq_portal_booking_confirmations b ON b.request_id=a.id
            LEFT JOIN hq_portal_booking_calendar_evidence ce ON ce.request_id=a.id
            JOIN hq_work_records w ON w.id=a.work_id AND w.customer_id=a.customer_id
            JOIN hq_vehicles v ON v.id=w.vehicle_id AND v.customer_id=a.customer_id
            JOIN hq_customers c ON c.id=a.customer_id
            ORDER BY CASE WHEN a.status='requested' THEN 0 ELSE 1 END,
                a.created_at DESC,a.id DESC LIMIT 100""").fetchall()
    return [dict(row) for row in rows]


def _revision(value):
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]{0,15})", value):
        raise ValueError("Invalid request revision")
    return int(value)


def owner_decide_availability(ref, expected_revision, decision, day="", hour="", message=""):
    """Owner may send a tentative offer or decline. Never confirms a booking."""
    _id(ref)
    if decision not in ("offered","declined"):
        raise ValueError("Invalid review decision")
    if decision == "offered":
        day, hour = _slot(day, hour)
    else:
        day, hour = None, None
    message = _note(message)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM hq_portal_availability_requests WHERE id=?",
                         (ref,)).fetchone()
        if row is None:
            raise LookupError("Availability request not found")
        # Retries with a matching decision are idempotent; old or different edits conflict.
        if (row["revision"] == expected_revision + 1 and row["status"] == decision
                and row["offered_date"] == day and row["offered_time"] == hour
                and (row["owner_message"] or "") == message):
            return
        if row["revision"] != expected_revision or row["status"] not in ("requested","offered"):
            raise ValueError("Request changed. Reload before making a decision")
        # An offer acknowledged by the customer is immutable; owner cannot quietly
        # alter an accepted/declined date or overwrite the customer's response.
        if db.execute("""SELECT 1 FROM hq_portal_availability_offer_responses
            WHERE request_id=?""",(ref,)).fetchone():
            raise ValueError("Customer already responded. This offer cannot be changed")
        now = _now()
        updated = db.execute("""UPDATE hq_portal_availability_requests
            SET status=?,offered_date=?,offered_time=?,owner_message=?,
                revision=revision+1,updated_at=?
            WHERE id=? AND revision=? AND status IN ('requested','offered')""",
            (decision,day,hour,message,now,ref,expected_revision))
        if updated.rowcount != 1:
            raise ValueError("Request changed. Reload before making a decision")
        db.execute("""INSERT INTO hq_portal_availability_events
            (request_id,revision,actor,action,occurred_at)
            VALUES (?,?,'owner',?,?)""",(ref,expected_revision+1,decision,now))


def respond_to_tentative_offer(customer_id, ref, expected_revision, decision):
    """Record the customer's answer to a tentative offer, never a confirmed booking.

    One immutable response per offered revision; BEGIN IMMEDIATE serialises owner
    revisions and concurrent customer submissions across Gunicorn workers.
    """
    _id(customer_id)
    _id(ref)
    if decision not in ("accepted","declined"):
        raise ValueError("Choose accept or decline")
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT a.revision,a.status,a.offered_date,a.offered_time,
            w.status AS work_status
            FROM hq_portal_availability_requests a
            JOIN hq_work_records w ON w.id=a.work_id AND w.customer_id=a.customer_id
            WHERE a.id=? AND a.customer_id=?
              AND NOT EXISTS (SELECT 1 FROM hq_work_archive_state z
                  WHERE z.work_id=w.id AND z.is_archived=1)""",
            (ref,customer_id)).fetchone()
        if row is None:
            raise LookupError("Availability request not found")
        if row["status"] != "offered" or row["work_status"] not in ("approved","in_progress"):
            raise ValueError("Only an active tentative offer can be answered")
        previous = db.execute("""SELECT offer_revision,decision FROM
            hq_portal_availability_offer_responses WHERE request_id=?""",
            (ref,)).fetchone()
        if previous:
            if (previous["offer_revision"],previous["decision"]) == (expected_revision,decision):
                return  # Same form replay is safe; no extra history or messages.
            raise ValueError("This offer was already answered. Reload before continuing")
        if row["revision"] != expected_revision:
            raise ValueError("Offer changed. Reload before responding")
        _slot(row["offered_date"], row["offered_time"])  # No acceptance of past slots.
        db.execute("""INSERT INTO hq_portal_availability_offer_responses
            (request_id,offer_revision,decision,responded_at)
            VALUES (?,?,?,?)""",(ref,expected_revision,decision,_now()))



def _google_calendar_preflight(ref, expected_revision, duration_hours):
    """Check the primary calendar at submission time when owner consent exists.

    Fail closed when a stored grant cannot be used. Without any connected
    calendar, preserve the existing explicitly manual attestation pathway.
    This point-in-time read cannot reserve external capacity.
    """
    _id(ref)
    if not isinstance(duration_hours, str) or not re.fullmatch(r"[1-8]", duration_hours):
        raise ValueError("Choose a duration of 1 to 8 hours")
    with connect() as db:
        _schema(db)
        _tables(db)
        row = db.execute("""SELECT a.status,a.revision,a.offered_date,a.offered_time,
                r.decision,b.status AS booking_status
            FROM hq_portal_availability_requests a
            LEFT JOIN hq_portal_availability_offer_responses r ON r.request_id=a.id
            LEFT JOIN hq_portal_booking_confirmations b ON b.request_id=a.id
            WHERE a.id=?""", (ref,)).fetchone()
    if row is None:
        raise LookupError("Availability request not found")
    if row["booking_status"] == "confirmed":
        return None  # A replay is evaluated for idempotency by the writer.
    if (row["status"] != "offered" or row["revision"] != expected_revision
            or row["decision"] != "accepted"):
        raise ValueError("Only a current accepted offer can be confirmed")
    day, hour = _slot(row["offered_date"], row["offered_time"])
    begin = datetime.fromisoformat(day + "T" + hour).replace(tzinfo=ZoneInfo("Europe/London"))
    end = begin + timedelta(hours=int(duration_hours))
    if begin <= datetime.now(ZoneInfo("Europe/London")) or end.hour > 17 or end.date() != begin.date():
        raise ValueError("Appointment must be in the future and finish by 17:00 UK time")
    resolve = current_app.extensions.get("hq_calendar_connection")
    entry = resolve() if callable(resolve) else None
    if (not entry or not isinstance(entry, tuple) or len(entry) != 2
            or not isinstance(entry[1], (int, float)) or entry[1] <= time.time()):
        if stored_grant_present("calendar", session.get("google_sub")):
            raise ValueError("Google Calendar permission cannot be used. Reconnect it before confirming.")
        return None
    try:
        check = live_conflicts(entry[0], begin.isoformat(), end.isoformat())
    except (requests.RequestException, RuntimeError, ValueError, TypeError):
        current_app.logger.warning("Primary Calendar could not be checked at booking confirmation")
        raise ValueError("Live Google Calendar could not be verified. No HQ booking was made.") from None
    if not isinstance(check, dict) or check.get("available") is not True:
        raise ValueError("Primary Google Calendar is busy or could not prove the slot is free. No HQ booking was made.")
    return {"checked_at": _now(), "start": begin.isoformat(), "end": end.isoformat()}


def confirm_accepted_offer(ref, expected_revision, duration_hours, external_checked, note="", calendar_evidence=None):
    """Owner action only. Local conflicts checked atomically; external check attested.

    Does not write Google Calendar/Square or send customer messages.
    """
    _id(ref)
    if not isinstance(duration_hours,str) or not re.fullmatch(r"[1-8]",duration_hours):
        raise ValueError("Choose a duration of 1 to 8 hours")
    duration=int(duration_hours)
    if external_checked is not True:
        raise ValueError("Check external calendars and acknowledge that review")
    note=_note(note)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("""SELECT a.*,r.offer_revision,r.decision AS customer_decision,
            w.status AS work_status
            FROM hq_portal_availability_requests a
            LEFT JOIN hq_portal_availability_offer_responses r ON r.request_id=a.id
            JOIN hq_work_records w ON w.id=a.work_id AND w.customer_id=a.customer_id
            WHERE a.id=? AND NOT EXISTS (SELECT 1 FROM hq_work_archive_state z
                WHERE z.work_id=w.id AND z.is_archived=1)""",(ref,)).fetchone()
        if row is None:
            raise LookupError("Active availability request not found")
        prior=db.execute("""SELECT * FROM hq_portal_booking_confirmations
            WHERE request_id=?""",(ref,)).fetchone()
        if prior is not None:
            if (prior["status"]=="confirmed" and prior["offered_revision"]==expected_revision
                and prior["booked_date"]==row["offered_date"]
                and prior["booked_time"]==row["offered_time"]
                and prior["duration_hours"]==duration and prior["confirmation_note"]==note):
                return
            raise ValueError("This request already has a booking decision; reload")
        if (row["status"]!="offered" or row["revision"]!=expected_revision
            or row["work_status"] not in ("approved","in_progress")
            or row["customer_decision"]!="accepted"
            or row["offer_revision"]!=expected_revision):
            raise ValueError("Only a current accepted, approved offer can be confirmed")
        day,hour=_slot(row["offered_date"],row["offered_time"])
        begins=datetime.combine(datetime.strptime(day,"%Y-%m-%d").date(),
                                datetime.strptime(hour,"%H:%M").time(),
                                tzinfo=ZoneInfo("Europe/London"))
        if begins <= datetime.now(ZoneInfo("Europe/London")):
            raise ValueError("A past appointment cannot be confirmed")
        if calendar_evidence is not None:
            if (not isinstance(calendar_evidence, dict)
                    or calendar_evidence.get("start") != begins.isoformat()
                    or calendar_evidence.get("end") != (begins + timedelta(hours=duration)).isoformat()):
                raise ValueError("Calendar evidence is for a different appointment slot")
            try:
                verified_at = datetime.fromisoformat(calendar_evidence["checked_at"])
                age = (datetime.now(timezone.utc) - verified_at).total_seconds()
                if verified_at.tzinfo is None or not 0 <= age <= 180:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise ValueError("Calendar evidence is expired or invalid") from None
        start=int(hour[:2])
        if start+duration>17:
            raise ValueError("The appointment must finish by 17:00 UK time")
        busy=db.execute("""SELECT booked_time,duration_hours
            FROM hq_portal_booking_confirmations
            WHERE status='confirmed' AND booked_date=?""",(day,)).fetchall()
        if any(start < int(b["booked_time"][:2])+b["duration_hours"]
               and int(b["booked_time"][:2]) < start+duration for b in busy):
            raise ValueError("Overlaps another HQ-confirmed appointment")
        if db.execute("""SELECT 1 FROM hq_portal_booking_confirmations
            WHERE work_id=? AND status='confirmed'""",(row["work_id"],)).fetchone():
            raise ValueError("This work already has an HQ-confirmed appointment")
        now=_now()
        db.execute("""INSERT INTO hq_portal_booking_confirmations
            (request_id,customer_id,work_id,offered_revision,booked_date,booked_time,
             duration_hours,status,confirmation_note,external_calendar_attested,confirmed_at)
            VALUES (?,?,?,?,?,?,?,'confirmed',?,1,?)""",
            (ref,row["customer_id"],row["work_id"],expected_revision,day,hour,
             duration,note,now))
        if calendar_evidence is not None:
            db.execute("""INSERT INTO hq_portal_booking_calendar_evidence
                (request_id,provider,checked_at,slot_start,slot_end)
                VALUES (?,'google_calendar_primary',?,?,?)""",
                (ref,calendar_evidence["checked_at"],calendar_evidence["start"],
                 calendar_evidence["end"]))
        db.execute("""INSERT INTO hq_portal_booking_events
            (request_id,revision,action,occurred_at) VALUES (?,0,'confirmed',?)""",
            (ref,now))


def cancel_confirmed_booking(ref, expected_booking_revision, reason):
    """Cancels only the native HQ record, never an external booking."""
    _id(ref)
    reason=_note(reason)
    if not reason:
        raise ValueError("A customer-safe cancellation reason is required")
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("""SELECT status,revision,cancel_reason
            FROM hq_portal_booking_confirmations WHERE request_id=?""",(ref,)).fetchone()
        if row is None:
            raise LookupError("HQ booking not found")
        if (row["status"]=="cancelled" and row["revision"]==expected_booking_revision+1
            and row["cancel_reason"]==reason):
            return
        if row["status"]!="confirmed" or row["revision"]!=expected_booking_revision:
            raise ValueError("Booking changed. Reload before cancelling")
        now=_now()
        changed=db.execute("""UPDATE hq_portal_booking_confirmations
            SET status='cancelled',cancel_reason=?,cancelled_at=?,revision=revision+1
            WHERE request_id=? AND status='confirmed' AND revision=?""",
            (reason,now,ref,expected_booking_revision))
        if changed.rowcount!=1:
            raise ValueError("Booking changed. Reload before cancelling")
        db.execute("""INSERT INTO hq_portal_booking_events
            (request_id,revision,action,occurred_at) VALUES (?,?,'cancelled',?)""",
            (ref,expected_booking_revision+1,now))


def withdraw_availability_request(customer_id, ref, expected_revision):
    """A customer may withdraw only their own unconfirmed request."""
    _id(customer_id)
    _id(ref)
    with connect() as db:
        db.execute("PRAGMA foreign_keys=ON")
        _schema(db)
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT revision,status FROM hq_portal_availability_requests
            WHERE id=? AND customer_id=?""",(ref,customer_id)).fetchone()
        if row is None:
            raise LookupError("Request not found")
        if db.execute("""SELECT 1 FROM hq_portal_booking_confirmations
            WHERE request_id=? AND status='confirmed'""",(ref,)).fetchone():
            raise ValueError("The owner must cancel an HQ-confirmed appointment")
        if row["revision"] == expected_revision + 1 and row["status"] == "withdrawn":
            return
        if row["revision"] != expected_revision or row["status"] not in ("requested","offered"):
            raise ValueError("Request changed. Reload before withdrawing")
        now = _now()
        changed = db.execute("""UPDATE hq_portal_availability_requests SET
            status='withdrawn',revision=revision+1,updated_at=?
            WHERE id=? AND customer_id=? AND revision=? AND status IN ('requested','offered')""",
            (now,ref,customer_id,expected_revision))
        if changed.rowcount != 1:
            raise ValueError("Request changed. Reload before withdrawing")
        db.execute("""INSERT INTO hq_portal_availability_events
            (request_id,revision,actor,action,occurred_at)
            VALUES (?,?,'customer','withdrawn',?)""",(ref,expected_revision+1,now))


@availability_requests.after_request
def _privacy(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _customer_page(customer_id, error=None, status=200):
    try:
        works, items = customer_availability_snapshot(customer_id)
    except (sqlite3.Error, OSError):
        current_app.logger.exception("Availability snapshot unavailable")
        abort(503)
    # A GET deep link never authorises a work record. Only a customer-owned,
    # active and unbooked work ID from the tenant-filtered query can be selected.
    requested_work = request.args.get("work_id", "")
    selected_work_id = requested_work if any(
        work["id"] == requested_work for work in works) else ""
    return render_template("customer_availability.html", works=works, items=items,
                           selected_work_id=selected_work_id,
                           csrf=session["customer_portal_csrf"], error=error,
                           operation_id=secrets.token_hex(16),
                           saved=request.args.get("saved") == "1"), status


@availability_requests.route("/customer/availability", methods=["GET","POST"])
def customer_availability():
    customer_id = active_customer()
    if not customer_id:
        return redirect("/customer/login", code=302)
    if request.method == "POST":
        if request.content_length is None or request.content_length > 2500:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf", ""),
                                      session.get("customer_portal_csrf", "unset")):
            abort(403)
        try:
            action = request.form.get("action")
            if action == "request":
                submit_availability_request(customer_id,request.form.get("work_id",""),
                                            request.form.get("date",""),request.form.get("time",""),
                                            request.form.get("note",""),request.form.get("operation_id",""))
            elif action == "respond":
                respond_to_tentative_offer(customer_id,request.form.get("request_id",""),
                                           _revision(request.form.get("revision","")),
                                           request.form.get("decision",""))
            elif action == "withdraw":
                withdraw_availability_request(customer_id,request.form.get("request_id",""),
                                              _revision(request.form.get("revision","")))
            else:
                abort(400)
        except LookupError:
            abort(404)
        except ValueError as exc:
            return _customer_page(customer_id,str(exc),409)
        except (sqlite3.Error,OSError):
            current_app.logger.exception("Availability save unavailable")
            abort(503)
        return redirect(url_for(".customer_availability",saved="1"),code=303)
    return _customer_page(customer_id)


@availability_requests.route("/hq/availability-requests", methods=["GET","POST"])
@required
def owner_availability():
    if request.method == "POST":
        if request.content_length is None or request.content_length > 2500:
            abort(413)
        if not secrets.compare_digest(request.form.get("csrf",""),
                                      session.get("csrf","unset")):
            abort(403)
        try:
            decision=request.form.get("decision","")
            if decision=="confirm":
                ref = request.form.get("request_id","")
                revision = _revision(request.form.get("revision",""))
                duration = request.form.get("duration_hours","")
                attested = request.form.get("external_checked") == "yes"
                if not attested:
                    raise ValueError("Check other calendars and acknowledge that review")
                evidence = _google_calendar_preflight(ref, revision, duration)
                confirm_accepted_offer(ref, revision, duration, attested,
                                       request.form.get("message",""), evidence)
            elif decision=="cancel":
                cancel_confirmed_booking(request.form.get("request_id",""),
                                         _revision(request.form.get("booking_revision","")),
                                         request.form.get("cancel_reason",""))
            else:
                owner_decide_availability(request.form.get("request_id",""),
                                          _revision(request.form.get("revision","")),
                                          decision,request.form.get("date",""),
                                          request.form.get("time",""),
                                          request.form.get("message",""))
        except LookupError:
            abort(404)
        except ValueError as exc:
            try:
                items=owner_availability_snapshot()
            except (sqlite3.Error,OSError):
                abort(503)
            return render_template("hq_availability_requests.html",items=items,
                                   csrf=session["csrf"],error=str(exc),saved=False),409
        except (sqlite3.Error,OSError):
            current_app.logger.exception("Availability review unavailable")
            abort(503)
        return redirect(url_for(".owner_availability",saved="1"),code=303)
    try:
        items=owner_availability_snapshot()
    except (sqlite3.Error,OSError):
        current_app.logger.exception("Availability inbox unavailable")
        abort(503)
    return render_template("hq_availability_requests.html",items=items,
                           csrf=session["csrf"],error=None,
                           saved=request.args.get("saved") == "1")
