"""Private canonical SQLite recovery workspace. No live restore or external transfer."""
from datetime import datetime, timezone
import secrets
import sqlite3
import uuid

from cryptography.exceptions import UnsupportedAlgorithm
from flask import Response, abort, make_response, render_template, request, session

from hq_auth import required
from hq_private_recovery import create_snapshot, list_snapshots
from hq_recovery_export import export_snapshot


def register_routes(bp):
    @bp.route("/hq/migration/recovery", methods=["GET", "POST"])
    @required
    def recovery_workspace():
        saved = None
        error = None
        status = 200
        operation = uuid.uuid4().hex
        if request.method == "POST":
            if request.content_length is None or request.content_length > 8192:
                abort(413)
            csrf = session.get("csrf")
            if not isinstance(csrf, str) or not csrf or not secrets.compare_digest(
                    request.form.get("csrf", ""), csrf):
                abort(403)
            action = request.form.get("action")
            fields = {
                "private_backup": {"csrf", "action", "operation_id", "confirm_backup"},
                "encrypted_export": {"csrf", "action", "snapshot_id", "public_key", "confirm_export"},
            }
            if action not in fields or set(request.form) != fields[action] or any(
                    len(request.form.getlist(k)) != 1 for k in request.form):
                abort(400)
            if action == "private_backup":
                if request.form.get("confirm_backup") != "yes":
                    abort(400)
                operation = request.form["operation_id"]
                try:
                    saved = create_snapshot(operation)
                except (ValueError, LookupError, sqlite3.Error, OSError):
                    error = "Private backup not confirmed. Check storage, capacity or concurrent operations. Review the saved list before retrying."
                    status = 409
                else:
                    operation = uuid.uuid4().hex
            else:
                if request.form.get("confirm_export") != "yes":
                    abort(400)
                try:
                    envelope = export_snapshot(request.form["snapshot_id"], request.form["public_key"])
                except (ValueError, LookupError, sqlite3.Error, OSError, UnsupportedAlgorithm):
                    error = "Encrypted export not completed. Check the saved snapshot and RSA public key."
                    status = 400
                else:
                    return Response(envelope, mimetype="application/octet-stream", headers={
                        "Content-Disposition": 'attachment; filename="tommy-tints-hq-recovery.tthq"',
                        "Cache-Control": "private, no-store, max-age=0",
                        "Pragma": "no-cache",
                        "X-Content-Type-Options": "nosniff",
                    })
        try:
            snapshots = [dict(item, utc=datetime.fromtimestamp(
                item["created_at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
                for item in list_snapshots()]
        except (ValueError, LookupError, sqlite3.Error, OSError, OverflowError):
            snapshots = None
            error = error or "Canonical database or private snapshot listing unavailable."
            status = 503
        response = make_response(render_template("hq_recovery_workspace.html",
            snapshots=snapshots, saved=saved, error=error, operation=operation,
            csrf=session.get("csrf", "")), status)
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response
