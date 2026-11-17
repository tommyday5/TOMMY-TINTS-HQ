"""Owner-only WebAuthn enrollment and public-key sign-in."""
import hashlib
import json
import os
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request, session
from webauthn import (
    generate_authentication_options, generate_registration_options, options_to_json,
    verify_authentication_response, verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
    ResidentKeyRequirement, UserVerificationRequirement,
)

from customer_request_store import storage_ready
from hq_auth import active_session, configured, owner_email, issue_session, bind_session
from hq_auth_store import auth_db, consume

passkeys = Blueprint("passkeys", __name__, url_prefix="/api/hq/passkeys")
ORIGIN = "https://tommy-tints-web-production.up.railway.app"
UV = UserVerificationRequirement.REQUIRED


def _user_handle(subject):
    return hashlib.sha256(("tommy-tints-hq:" + subject).encode()).digest()


def _settings():
    origin = os.environ.get("HQ_WEBAUTHN_ORIGIN", ORIGIN)
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or origin != f"https://{parsed.netloc}" or parsed.username:
        raise ValueError("Invalid WebAuthn origin configuration")
    return origin, parsed.hostname


def _allowed(manage=False):
    try:
        origin, rp_id = _settings()
    except ValueError:
        return None
    if (not configured() or not storage_ready() or not request.is_json
            or request.headers.get("Origin") != origin or request.headers.get("X-HQ-Passkey") != "1"
            or request.content_length is None or request.content_length > 24000):
        return None
    if not manage:
        return origin, rp_id, None
    active = active_session()
    if (active is None or int(time.time()) - active["issued"] > 300
            or not secrets.compare_digest(request.headers.get("X-HQ-CSRF", ""), session.get("csrf", "not-set"))):
        return None
    return origin, rp_id, active


def _challenge(challenge, operation, active):
    session["webauthn"] = {
        "id": bytes_to_base64url(challenge), "operation": operation,
        "expires": int(time.time()) + 120, "sid": active["id"] if active else None,
    }


def _take(operation, active):
    value = session.pop("webauthn", None)
    if (not value or value["operation"] != operation or value["expires"] <= int(time.time())
            or value["sid"] != (active["id"] if active else None)):
        raise ValueError("Passkey request expired")
    return base64url_to_bytes(value["id"]), value["expires"]


@passkeys.post("/<operation>/options")
def options(operation):
    if operation not in {"register", "login"}:
        return jsonify(error="Invalid operation"), 404
    allowed = _allowed(operation == "register")
    if not allowed:
        return jsonify(error="Passkey setup requires a recent owner sign-in"), 403
    _, rp, active = allowed
    if active:
        with auth_db() as db:
            descriptors = [PublicKeyCredentialDescriptor(id=row["id"]) for row in db.execute(
                "SELECT id FROM passkeys WHERE email=?", (owner_email(),))]
        opts = generate_registration_options(
            rp_id=rp, rp_name="Tommy Tints HQ", user_name=owner_email(),
            user_id=_user_handle(active["subject"]), exclude_credentials=descriptors,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UV))
    else:
        # Discoverable passkeys avoid disclosing the owner's credential IDs.
        opts = generate_authentication_options(rp_id=rp, user_verification=UV)
    _challenge(opts.challenge, operation, active)
    return jsonify(json.loads(options_to_json(opts)))


@passkeys.post("/<operation>/verify")
def verify(operation):
    if operation not in {"register", "login"}:
        return jsonify(error="Invalid operation"), 404
    allowed = _allowed(operation == "register")
    if not allowed:
        return jsonify(error="Passkey verification unavailable"), 403
    origin, rp, active = allowed
    try:
        challenge, expires = _take(operation, active)
        credential = request.get_json()
        if not isinstance(credential, dict):
            raise ValueError("Invalid credential")
        with auth_db() as db:
            db.execute("BEGIN IMMEDIATE")
            if active:
                if not db.execute("SELECT 1 FROM sessions WHERE id=? AND expires>?",
                                  (active["id"], int(time.time()))).fetchone():
                    raise ValueError("Owner session expired")
                result = verify_registration_response(
                    credential=credential, expected_challenge=challenge,
                    expected_rp_id=rp, expected_origin=origin, require_user_verification=True)
                consume(db, challenge, expires)
                db.execute("INSERT INTO passkeys VALUES (?,?,?,?,?,?)",
                           (result.credential_id, owner_email(), active["subject"],
                            result.credential_public_key, result.sign_count, int(time.time())))
            else:
                ref = base64url_to_bytes(credential["rawId"])
                key = db.execute("SELECT * FROM passkeys WHERE id=? AND email=?",
                                 (ref, owner_email())).fetchone()
                if key is None or base64url_to_bytes(credential["response"]["userHandle"]) != _user_handle(key["subject"]):
                    raise ValueError("Invalid credential owner")
                result = verify_authentication_response(
                    credential=credential, expected_challenge=challenge,
                    expected_origin=origin, expected_rp_id=rp, require_user_verification=True,
                    credential_public_key=key["public_key"], credential_current_sign_count=key["counter"])
                consume(db, challenge, expires)
                db.execute("UPDATE passkeys SET counter=? WHERE id=?", (result.new_sign_count, ref))
                sid = issue_session(db, key["subject"], session.get("hq_sid"))
        if not active:
            bind_session(sid, key["subject"])
    except (WebAuthnException, ValueError, TypeError, KeyError, sqlite3.IntegrityError):
        return jsonify(error="Passkey verification failed. Try again or sign in with Google."), 403
    return jsonify(ok=True)


@passkeys.post("/remove")
def remove():
    allowed = _allowed(manage=True)
    if not allowed:
        return jsonify(error="Sign in again before removing a passkey"), 403
    data = request.get_json()
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        return jsonify(error="Invalid passkey"), 400
    try:
        ref = base64url_to_bytes(data["id"])
    except ValueError:
        return jsonify(error="Invalid passkey"), 400
    _, _, active = allowed
    with auth_db() as db:
        changed = db.execute("""DELETE FROM passkeys WHERE id=? AND email=? AND subject=?
            AND EXISTS (SELECT 1 FROM sessions WHERE id=? AND expires>?)""",
            (ref, owner_email(), active["subject"], active["id"], int(time.time()))).rowcount
    return (jsonify(ok=True) if changed else (jsonify(error="Passkey not found"), 404))


def registered_passkeys(active):
    if not storage_ready():
        return []
    with auth_db() as db:
        return [{"id": bytes_to_base64url(row["id"]), "created": row["created"]} for row in db.execute(
            "SELECT id, created FROM passkeys WHERE email=? AND subject=? ORDER BY created DESC",
            (owner_email(), active["subject"]))]
