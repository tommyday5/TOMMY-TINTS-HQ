import os, secrets, time
from functools import wraps
from flask import Blueprint, request, session, redirect, url_for, render_template_string, abort
from werkzeug.security import check_password_hash
auth = Blueprint("auth", __name__)
attempts = {}
def configured():
    return bool(os.environ.get("HQ_SESSION_SECRET") and os.environ.get("HQ_ADMIN_PASSWORD_HASH"))
def required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not configured():
            return "Authentication configuration required.", 503
        if not session.get("hq_auth"):
            return redirect(url_for("auth.login"))
        return fn(*args, **kwargs)
    return wrapped
@auth.route("/login", methods=["GET", "POST"])
def login():
    if not configured():
        return "Authentication configuration required.", 503
    if session.get("hq_auth"):
        return redirect("/")
    session.setdefault("login_csrf", secrets.token_urlsafe(32))
    error = ""
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        now = time.monotonic()
        recent = [t for t in attempts.get(ip, []) if now - t < 900]
        if len(recent) >= 10:
            return "Too many attempts", 429
        attempts[ip] = recent + [now]
        if not secrets.compare_digest(request.form.get("csrf", ""), session["login_csrf"]):
            abort(400)
        if check_password_hash(os.environ["HQ_ADMIN_PASSWORD_HASH"], request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["hq_auth"] = True
            session["csrf"] = secrets.token_urlsafe(32)
            attempts.pop(ip, None)
            return redirect("/")
        error = "Incorrect password"
    return render_template_string("""<!doctype html><html><meta name="viewport" content="width=device-width,initial-scale=1"><title>HQ Sign in</title><style>body{background:#0b1018;color:#fff;font-family:system-ui;display:grid;place-items:center;min-height:100vh}form{background:#182231;padding:30px;border-radius:14px}input,button{display:block;padding:12px;margin:12px 0;width:90%;border-radius:7px}button{background:#d32d40;color:white;border:0}</style><form method="post"><h1>Tommy Tints HQ</h1><p>Private sign in</p><p>{{error}}</p><input type="hidden" name="csrf" value="{{csrf}}"><input type="password" name="password" required autocomplete="current-password" placeholder="Password"><button>Sign in</button></form></html>""",csrf=session["login_csrf"],error=error)
@auth.post("/logout")
@required
def logout():
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "")):
        abort(400)
    session.clear()
    return redirect(url_for("auth.login"))
