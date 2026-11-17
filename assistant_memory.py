"""Owner-only persistent chat transcript and action audit, stored on HQ_DATA_DIR."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Keep at most 100 conversation turns on disk; older messages are removed.
MAX_CHAT_MESSAGES = 200

def connect():
    base=Path(os.environ.get("HQ_DATA_DIR", "/tmp/tommy-tints-hq"))
    base.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(str(base/"assistant_memory.sqlite3"),timeout=10)
    db.row_factory=sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS chat_messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL,
        content TEXT NOT NULL, created_at TEXT NOT NULL)""")
    # Safe additive migration: pre-existing conversations remain readable.
    columns = {row[1] for row in db.execute("PRAGMA table_info(chat_messages)")}
    if "provider_visible" not in columns:
        db.execute("ALTER TABLE chat_messages ADD COLUMN provider_visible INTEGER NOT NULL DEFAULT 1")
    db.execute("""CREATE TABLE IF NOT EXISTS assistant_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL,
        details TEXT NOT NULL, created_at TEXT NOT NULL)""")
    return db

def history(limit=12, *, provider_visible_only=False):
    if type(limit) is not int or not 1 <= limit <= MAX_CHAT_MESSAGES:
        raise ValueError("History limit must be between 1 and 200")
    if type(provider_visible_only) is not bool:
        raise ValueError("Invalid history visibility")
    with connect() as db:
        if provider_visible_only:
            rows=db.execute("SELECT role,content FROM chat_messages WHERE provider_visible=1 ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
        else:
            rows=db.execute("SELECT role,content FROM chat_messages ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
    return [dict(row) for row in reversed(rows)]

def record_chat(user_message,assistant_reply, *, provider_visible=True):
    if not all(isinstance(s,str) and bool(s.strip()) and len(s)<=12000 for s in (user_message,assistant_reply)):
        raise ValueError("Invalid conversation content")
    if type(provider_visible) is not bool:
        raise ValueError("Invalid provider history visibility")
    now=datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.executemany("INSERT INTO chat_messages(role,content,created_at,provider_visible) VALUES(?,?,?,?)",
                       [("user",user_message,now,int(provider_visible)),
                        ("assistant",assistant_reply,now,int(provider_visible))])
        db.execute("""DELETE FROM chat_messages WHERE id NOT IN
                      (SELECT id FROM chat_messages ORDER BY id DESC LIMIT ?)""",
                   (MAX_CHAT_MESSAGES,))
        db.execute("INSERT INTO assistant_audit(event,details,created_at) VALUES(?,?,?)",
                   ("chat_reply","Local HQ reply stored privately; not sent to AI provider" if not provider_visible
                    else "Read-only AI response stored; no external action",now))

def clear_history():
    now=datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("DELETE FROM chat_messages")
        db.execute("INSERT INTO assistant_audit(event,details,created_at) VALUES(?,?,?)",
                   ("chat_cleared","Owner cleared stored assistant conversation",now))

def audit(event,details):
    if not isinstance(event,str) or not 1<=len(event)<=80 or not event.strip() or any(ord(ch)<32 or ord(ch)==127 for ch in event) or not isinstance(details,str) or len(details)>500:
        raise ValueError("Invalid audit event")
    with connect() as db:
        db.execute("INSERT INTO assistant_audit(event,details,created_at) VALUES(?,?,?)",
                   (event,details,datetime.now(timezone.utc).isoformat()))

def audit_events(limit=50):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Audit limit must be between 1 and 100")
    with connect() as db:
        rows=db.execute("SELECT event,details,created_at FROM assistant_audit ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
    return [dict(row) for row in rows]
