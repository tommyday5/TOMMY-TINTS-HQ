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
    db.execute("""CREATE TABLE IF NOT EXISTS assistant_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL,
        details TEXT NOT NULL, created_at TEXT NOT NULL)""")
    return db

def history(limit=12):
    if type(limit) is not int or not 1 <= limit <= MAX_CHAT_MESSAGES:
        raise ValueError("History limit must be between 1 and 200")
    with connect() as db:
        rows=db.execute("SELECT role,content FROM chat_messages ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
    return [dict(row) for row in reversed(rows)]

def record_chat(user_message,assistant_reply):
    if not all(isinstance(s,str) and 0<len(s.strip())<=12000 for s in (user_message,assistant_reply)):
        raise ValueError("Invalid conversation content")
    now=datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.executemany("INSERT INTO chat_messages(role,content,created_at) VALUES(?,?,?)",
                       [("user",user_message,now),("assistant",assistant_reply,now)])
        db.execute("""DELETE FROM chat_messages WHERE id NOT IN
                      (SELECT id FROM chat_messages ORDER BY id DESC LIMIT ?)""",
                   (MAX_CHAT_MESSAGES,))
        db.execute("INSERT INTO assistant_audit(event,details,created_at) VALUES(?,?,?)",
                   ("chat_reply","Read-only AI response stored; no external action",now))

def clear_history():
    now=datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("DELETE FROM chat_messages")
        db.execute("INSERT INTO assistant_audit(event,details,created_at) VALUES(?,?,?)",
                   ("chat_cleared","Owner cleared stored assistant conversation",now))

def audit(event,details):
    if not isinstance(event,str) or not 1<=len(event)<=80 or not isinstance(details,str) or len(details)>500:
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
