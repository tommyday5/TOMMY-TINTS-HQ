"""SQLite persistence for owner-only job material logs. Requires mounted HQ_DATA_DIR."""
import json, os, sqlite3
from pathlib import Path

def database_path():
    root=os.environ.get("HQ_DATA_DIR")
    if not root: raise RuntimeError("Persistent storage not configured")
    directory=Path(root)
    if not directory.is_dir(): raise RuntimeError("Persistent storage directory missing")
    return str(directory/"hq_jobs.sqlite3")

def connect():
    db=sqlite3.connect(database_path(),timeout=10)
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("""CREATE TABLE IF NOT EXISTS job_material_logs(
        owner TEXT NOT NULL, job_key TEXT NOT NULL, payload TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(owner,job_key))""")
    return db

def read_log(owner,job_key):
    with connect() as db:
        row=db.execute("SELECT payload,updated_at FROM job_material_logs WHERE owner=? AND job_key=?",(owner,job_key)).fetchone()
    return {"data":json.loads(row[0]),"updated_at":row[1]} if row else {"data":None,"updated_at":None}

def write_log(owner,job_key,data):
    encoded=json.dumps(data,separators=(",",":"),allow_nan=False)
    if len(encoded)>100000: raise ValueError("Job material log too large")
    with connect() as db:
        db.execute("""INSERT INTO job_material_logs(owner,job_key,payload) VALUES(?,?,?)
            ON CONFLICT(owner,job_key) DO UPDATE SET payload=excluded.payload,updated_at=CURRENT_TIMESTAMP""",
            (owner,job_key,encoded))
    return read_log(owner,job_key)
