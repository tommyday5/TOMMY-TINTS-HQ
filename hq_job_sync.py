"""Transactional job-sync outbox. No provider writes until mapping/consent activation."""
import re
from datetime import datetime, timezone
from customer_request_store import connect

WORKBOOK='1B0XULUDiMlVuW9u0kUOYem8WelBtOH-Xey_wWVYdb2M'
# Inspected Jobs row 4. Formula/payment-derived columns deliberately excluded.
INPUT_COLUMNS={'job_ref':'A','date':'B','customer':'C','customer_type':'D','vehicle':'E',
 'services':['F','G','H','I','J'],'quoted_price':'Q','status':'R',
 'actual_hours':'AG','actual_materials':'AH','actual_consumables':'AI','actual_extra_costs':'AJ'}


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS hq_job_sync_outbox (
        work_id TEXT PRIMARY KEY, desired_revision INTEGER NOT NULL,
        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, updated_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS hq_job_sheet_links (
        work_id TEXT PRIMARY KEY, workbook TEXT NOT NULL, job_ref TEXT NOT NULL,
        baseline TEXT NOT NULL, UNIQUE(workbook,job_ref))''')


def enqueue(db, work_id, revision):
    """Queue only an actual saved HQ revision, within the caller's transaction.

    Reject future/invalid revisions, ignore superseded delayed calls, and
    restart retry bookkeeping only when a genuinely newer revision is queued.
    No Google/Sheets write is performed here.
    """
    if not isinstance(work_id, str) or not re.fullmatch(r'[0-9a-f]{32}', work_id):
        raise ValueError('Invalid work record for Sheets outbox')
    if type(revision) is not int or not 0 <= revision < 2**63 - 1:
        raise ValueError('Invalid work revision for Sheets outbox')
    schema(db)
    row=db.execute('SELECT source_request_id,revision FROM hq_work_records WHERE id=?',
                   (work_id,)).fetchone()
    if not row or row['source_request_id'].startswith('TEST-'):
        return
    if revision > row['revision']:
        raise ValueError('Cannot queue a Sheets revision that has not been saved')
    if revision < row['revision']:
        return
    db.execute('''INSERT INTO hq_job_sync_outbox(work_id,desired_revision,state,updated_at)
        VALUES (?,?,'pending',?) ON CONFLICT(work_id) DO UPDATE SET
        desired_revision=excluded.desired_revision,state='pending',attempts=0,
        last_error=NULL,updated_at=excluded.updated_at
        WHERE excluded.desired_revision > hq_job_sync_outbox.desired_revision''',
        (work_id,revision,datetime.now(timezone.utc).isoformat()))


def status(work_id):
    if not isinstance(work_id,str) or not re.fullmatch('[0-9a-f]{32}',work_id):
        raise ValueError('Invalid work record')
    with connect() as db:
        # Read side does not migrate, initialise schema or call Google.
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='hq_job_sync_outbox'").fetchone():
            return {'state':'not_linked','activation':'write_access_and_verified_mapping_required'}
        row=db.execute('SELECT desired_revision,state,attempts,last_error,updated_at FROM hq_job_sync_outbox WHERE work_id=?',(work_id,)).fetchone()
        return {**(dict(row) if row else {'state':'not_linked'}),
                'activation':'write_access_and_verified_mapping_required','live_sync':False}
