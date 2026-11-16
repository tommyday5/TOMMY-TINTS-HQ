"""Durable, owner-only assistant task inbox. Approvals never execute actions."""
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

CATEGORIES = {"email", "calendar", "quote", "payments", "customer", "daily", "general"}
STATUSES = {"draft", "approved", "rejected"}

def database_path():
    base = Path(os.environ.get("HQ_DATA_DIR", "/tmp/tommy-tints-hq"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "assistant_tasks.sqlite3"

def connect():
    db = sqlite3.connect(str(database_path()), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS assistant_tasks (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
        details TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    return db

def create_task(payload):
    if not isinstance(payload, dict):
        raise ValueError("Task must be a JSON object")
    title, details, category = payload.get("title"), payload.get("details", ""), payload.get("category", "general")
    if not isinstance(title, str) or not 3 <= len(title.strip()) <= 160:
        raise ValueError("Task title must be 3 to 160 characters")
    if not isinstance(details, str) or len(details) > 3000:
        raise ValueError("Task details must be 3000 characters or fewer")
    if category not in CATEGORIES:
        raise ValueError("Unknown task category")
    now = datetime.now(timezone.utc).isoformat()
    item = {"id": str(uuid.uuid4()), "title": title.strip(), "category": category,
            "details": details.strip(), "status": "draft", "created_at": now, "updated_at": now}
    with connect() as db:
        db.execute("INSERT INTO assistant_tasks VALUES (:id,:title,:category,:details,:status,:created_at,:updated_at)", item)
    return item

def list_tasks():
    with connect() as db:
        rows = db.execute("SELECT * FROM assistant_tasks ORDER BY created_at DESC LIMIT 100").fetchall()
    return [dict(row) for row in rows]

def review_task(task_id, decision):
    if decision not in ("approved", "rejected"):
        raise ValueError("Decision must be approved or rejected")
    if not isinstance(task_id, str) or len(task_id) > 64:
        raise ValueError("Invalid task identifier")
    with connect() as db:
        changed = db.execute(
            "UPDATE assistant_tasks SET status=?,updated_at=? WHERE id=? AND status='draft'",
            (decision, datetime.now(timezone.utc).isoformat(), task_id))
        if changed.rowcount != 1:
            exists = db.execute("SELECT 1 FROM assistant_tasks WHERE id=?", (task_id,)).fetchone()
            if exists is None:
                raise LookupError("Task not found")
            raise ValueError("Only draft tasks can be reviewed")
        updated = db.execute("SELECT * FROM assistant_tasks WHERE id=?", (task_id,)).fetchone()
    return dict(updated)
