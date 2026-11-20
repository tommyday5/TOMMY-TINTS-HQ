"""Durable shared usage ceiling for owner-triggered billable Routes calls."""
import time
from hq_auth_store import auth_db


def reserve_route_request():
    # Shared across workers/sessions and both Routes endpoints. Failed provider
    # attempts count too: provider errors can still consume quota.
    now = int(time.time())
    with auth_db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS maps_route_usage (created INTEGER NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM maps_route_usage WHERE created <= ?', (now - 86400,))
        hourly, daily = db.execute(
            'SELECT COALESCE(SUM(created > ?),0), COUNT(*) FROM maps_route_usage',
            (now - 3600,)).fetchone()
        if hourly >= 20 or daily >= 100:
            return False
        db.execute('INSERT INTO maps_route_usage VALUES (?)', (now,))
    return True
