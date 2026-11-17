"""Google OpenID Connect authentication for the private Tommy Tints HQ dashboard."""
import os
import secrets
import sqlite3
import time
from hq_auth_store import auth_db
from functools import wraps
from flask import Blueprint, current_app, redirect, request, session, url_for, abort
from authlib.integrations.flask_client import OAuth

auth = Blueprint("auth", __name__)
oauth = OAuth()
google = oauth.register(
    name="google",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

def configured():
    return all(os.environ.get(key) for key in (
        "HQ_SESSION_SECRET", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
        "HQ_ALLOWED_GOOGLE_EMAIL"))

def owner_email():
    return os.environ.get("HQ_ALLOWED_GOOGLE_EMAIL", "").strip().casefold()


def active_session():
    sid = session.get("hq_sid")
    if not isinstance(sid, str) or not configured():
        return None
    try:
        with auth_db() as db:
            return db.execute("SELECT * FROM sessions WHERE id=? AND email=? AND expires>?",
                              (sid, owner_email(), int(time.time()))).fetchone()
    except (OSError, sqlite3.Error):
        return None


def issue_session(db, subject, previous=None):
    if not isinstance(subject, str) or not subject:
        raise ValueError("Google owner subject is required")
    sid = secrets.token_urlsafe(32)
    now = int(time.time())
    db.execute("DELETE FROM sessions WHERE expires<=? OR id=?", (now, previous))
    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (sid, owner_email(), subject, now, now+3600))
    return sid


def bind_session(sid, subject):
    session.clear()
    session.permanent = True
    session["hq_sid"] = sid
    session["csrf"] = secrets.token_urlsafe(32)
    session["google_sub"] = subject


def start_session(subject):
    with auth_db() as db:
        sid = issue_session(db, subject, session.get("hq_sid"))
    bind_session(sid, subject)


def required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not configured():
            return "Authentication configuration required. Private HQ data remains disabled.", 503
        if active_session() is None:
            return redirect("/")
        return fn(*args, **kwargs)
    return wrapped

@auth.get("/login")
def login():
    if not configured():
        return "Google sign-in is not configured yet.", 503
    if active_session() is not None:
        return redirect("/")
    return google.authorize_redirect(url_for("auth.callback", _external=True, _scheme="https"),
                                     prompt="select_account")

@auth.get("/auth/google/callback")
def callback():
    if not configured():
        abort(503)
    token = google.authorize_access_token()
    identity = token.get("userinfo") or google.userinfo(token=token).json()
    expected = os.environ["HQ_ALLOWED_GOOGLE_EMAIL"].strip().casefold()
    actual = str(identity.get("email", "")).strip().casefold()
    if identity.get("email_verified") is not True or actual != expected or not identity.get("sub"):
        session.clear()
        return "This Google account is not authorised for Tommy Tints HQ.", 403
    start_session(identity["sub"])
    return redirect("/")

@auth.post("/logout")
@required
def logout():
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "not-set")):
        abort(400)
    with auth_db() as db:
        db.execute("DELETE FROM sessions WHERE id=?", (session["hq_sid"],))
    session.clear()
    return redirect("/")
