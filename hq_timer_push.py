"""Private opt-in device enrollment only; no sender, network calls or delivery."""
import base64
import hashlib
import os
import re
from urllib.parse import urlsplit
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey, SECP256R1
from customer_request_store import connect
from hq_work_timer import now

MAX_DEVICES = 5
# Restrict enrollment to known Apple Web Push endpoints. No arbitrary URL delivery.
APPLE_HOST = "web.push.apple.com"


def _bytes(value, length):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value) or len(value) > 100:
        raise ValueError("Invalid subscription key")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        raise ValueError("Invalid subscription key") from None
    if len(raw) != length or base64.urlsafe_b64encode(raw).decode().rstrip("=") != value:
        raise ValueError("Invalid subscription key")
    return raw


def _point(value):
    raw = _bytes(value, 65)
    if raw[0] != 4:
        raise ValueError("Invalid public key")
    EllipticCurvePublicKey.from_encoded_point(SECP256R1(), raw)
    return value


def public_key():
    value = os.environ.get("HQ_TIMER_PUSH_PUBLIC_KEY", "").strip()
    try:
        return _point(value)
    except ValueError:
        return None


def _endpoint(value):
    if not isinstance(value, str) or len(value) > 2048 or not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid push endpoint")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != APPLE_HOST
            or not parsed.path or parsed.path == "/" or parsed.fragment
            or "\\" in value):
        raise ValueError("Only Apple Web Push devices are supported")
    return value


def _actor(actor):
    if not isinstance(actor, str) or not actor or len(actor) > 256:
        raise ValueError("Owner identity required")
    return actor


def _tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hq_timer_push_devices(
        id TEXT PRIMARY KEY, owner_sub TEXT NOT NULL, endpoint TEXT,
        p256dh TEXT, auth_key TEXT, application_key TEXT,
        enrolled_at INTEGER NOT NULL, revoked_at INTEGER,
        revision INTEGER NOT NULL DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS hq_timer_push_events(
        device_id TEXT NOT NULL, owner_sub TEXT NOT NULL, revision INTEGER NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('enroll','revoke')),
        recorded_at INTEGER NOT NULL, PRIMARY KEY(device_id,revision))""")


def devices(actor):
    actor = _actor(actor)
    with connect() as db:
        _tables(db)
        return [dict(row) for row in db.execute("""SELECT id,enrolled_at,revoked_at,revision
            FROM hq_timer_push_devices WHERE owner_sub=? AND revoked_at IS NULL
            ORDER BY enrolled_at DESC,id""", (actor,))]


def enroll(actor, *, endpoint, p256dh, auth_key, application_key, consent):
    actor = _actor(actor)
    key = public_key()
    if consent != "yes" or not key or key != application_key:
        raise ValueError("Explicit consent and current application key required")
    endpoint = _endpoint(endpoint)
    _point(p256dh)
    _bytes(auth_key, 16)
    identifier = hashlib.sha256(endpoint.encode()).hexdigest()
    with connect() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT * FROM hq_timer_push_devices WHERE id=?", (identifier,)).fetchone()
        if old and old["owner_sub"] != actor:
            raise ValueError("Device belongs to another owner")
        if old and old["revoked_at"] is None:
            if (old["p256dh"], old["auth_key"], old["application_key"]) == (p256dh, auth_key, key):
                return identifier
            raise ValueError("Revoke the previous registration before replacing its keys")
        if db.execute("SELECT COUNT(*) FROM hq_timer_push_devices WHERE owner_sub=? AND revoked_at IS NULL", (actor,)).fetchone()[0] >= MAX_DEVICES:
            raise ValueError("Revoke an existing device before enrolling another")
        revision = old["revision"] + 1 if old else 0
        timestamp = now()
        db.execute("""INSERT INTO hq_timer_push_devices
            (id,owner_sub,endpoint,p256dh,auth_key,application_key,enrolled_at,revoked_at,revision)
            VALUES (?,?,?,?,?,?,?,NULL,?)
            ON CONFLICT(id) DO UPDATE SET endpoint=excluded.endpoint,p256dh=excluded.p256dh,
            auth_key=excluded.auth_key,application_key=excluded.application_key,
            enrolled_at=excluded.enrolled_at,revoked_at=NULL,revision=excluded.revision""",
            (identifier, actor, endpoint, p256dh, auth_key, key, timestamp, revision))
        db.execute("INSERT INTO hq_timer_push_events VALUES (?,?,?,?,?)",
                   (identifier, actor, revision, "enroll", timestamp))
    return identifier


def revoke(actor, identifier, revision):
    actor = _actor(actor)
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError("Invalid device")
    with connect() as db:
        _tables(db)
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT * FROM hq_timer_push_devices WHERE id=? AND owner_sub=?",
                         (identifier, actor)).fetchone()
        if old is None:
            raise LookupError("Device not found")
        if old["revoked_at"] is not None and old["revision"] == revision + 1:
            return
        if old["revision"] != revision or old["revoked_at"] is not None:
            raise ValueError("Registration changed; refresh and review")
        timestamp = now()
        db.execute("""UPDATE hq_timer_push_devices SET endpoint=NULL,p256dh=NULL,auth_key=NULL,
            application_key=NULL,revoked_at=?,revision=revision+1 WHERE id=?""",
            (timestamp, identifier))
        db.execute("INSERT INTO hq_timer_push_events VALUES (?,?,?,?,?)",
                   (identifier, actor, revision + 1, "revoke", timestamp))
