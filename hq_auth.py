"""Google OpenID Connect authentication for the private Tommy Tints HQ dashboard."""
import os
import secrets
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

def required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not configured():
            return "Authentication configuration required. Private HQ data remains disabled.", 503
        if session.get("hq_auth") is not True:
            return redirect(url_for("auth.login"))
        return fn(*args, **kwargs)
    return wrapped

@auth.get("/login")
def login():
    if not configured():
        return "Google sign-in is not configured yet.", 503
    if session.get("hq_auth") is True:
        return redirect("/")
    return google.authorize_redirect(url_for("auth.callback", _external=True, _scheme="https"))

@auth.get("/auth/google/callback")
def callback():
    if not configured():
        abort(503)
    token = google.authorize_access_token()
    identity = token.get("userinfo") or google.userinfo(token=token).json()
    expected = os.environ["HQ_ALLOWED_GOOGLE_EMAIL"].strip().casefold()
    actual = str(identity.get("email", "")).strip().casefold()
    if not identity.get("email_verified") or not secrets.compare_digest(actual, expected):
        session.clear()
        return "This Google account is not authorised for Tommy Tints HQ.", 403
    session.clear()
    session.permanent = True
    session["hq_auth"] = True
    session["csrf"] = secrets.token_urlsafe(32)
    session["google_sub"] = identity.get("sub")
    return redirect("/")

@auth.post("/logout")
@required
def logout():
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "")):
        abort(400)
    session.clear()
    return redirect(url_for("auth.login"))
