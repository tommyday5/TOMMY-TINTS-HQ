"""Private reversible lifecycle of manually entered transactions only."""
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from flask import abort, redirect, render_template, request, session, url_for
from customer_request_store import connect, storage_ready
from hq_auth import required

HEX = re.compile(r"[0-9a-f]{32}\Z")


def register(bp):
    @bp.route("/hq/finance/transactions/<ref>/archive", methods=["GET","POST"])
    @required
    def transaction_archive_page(ref):
        from hq_native_finance import schema, transaction_archived, audit, capture_transaction, text
        from hq_finance_splits import tables, active
        if not HEX.fullmatch(ref):
            abort(404)
        if not storage_ready():
            abort(503)
        error, status = None, 200
        try:
            with closing(connect()) as db, db:
                schema(db)
                tables(db)
                if request.method == "POST":
                    request.max_content_length = 4096
                    token = session.get("csrf")
                    if not token or not secrets.compare_digest(request.form.get("csrf",""),token):
                        abort(403)
                    db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM finance_transactions WHERE id=?", (ref,)).fetchone()
                if not row:
                    abort(404)
                archived = transaction_archived(db,ref)
                split = active(db,ref)
                if request.method == "POST":
                    raw = request.form.get("revision","")
                    action = request.form.get("action")
                    if not re.fullmatch(r"[0-9]{1,19}",raw) or action not in ("archive","restore"):
                        error,status="Invalid archive request.",400
                    elif int(raw) != row["revision"]:
                        error,status="Transaction changed elsewhere. Reload and review again.",409
                    elif (action == "archive") == archived:
                        error,status="Transaction already has that state.",409
                    elif split:
                        error,status="Withdraw the active split before changing this transaction.",409
                    elif row["revision"] >= 2**63-1:
                        error,status="Transaction revision limit reached.",409
                    else:
                        try:
                            reason = text(request.form.get("reason"))
                        except ValueError:
                            error,status="Enter a reason of 1–160 characters.",400
                        if not error:
                            revision = row["revision"]+1
                            capture_transaction(db,row,"before_edit")
                            db.execute("UPDATE finance_transactions SET revision=? WHERE id=? AND revision=?",(revision,ref,row["revision"]))
                            db.execute("INSERT INTO finance_transaction_archive VALUES(?,?,?,?,?)",
                                (ref,revision,int(action=="archive"),reason,datetime.now(timezone.utc).isoformat()))
                            capture_transaction(db,db.execute("SELECT * FROM finance_transactions WHERE id=?",(ref,)).fetchone(),"saved")
                            audit(db,"transaction_"+action,ref,revision)
                            return redirect(url_for("native_finance.transaction_archive_page",ref=ref),code=303)
                history = [dict(r) for r in db.execute("SELECT * FROM finance_transaction_archive WHERE transaction_id=? ORDER BY revision DESC LIMIT 100",(ref,))]
                parent = dict(row)
        except (sqlite3.Error,OSError):
            abort(503)
        return render_template("finance_transaction_archive.html",parent=parent,archived=archived,
            split=split,history=history,csrf=session.get("csrf",""),error=error),status,{"Cache-Control":"no-store"}
