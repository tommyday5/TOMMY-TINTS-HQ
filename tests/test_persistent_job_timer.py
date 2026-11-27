import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import create_test_job, change_work_status, StaleWorkRecordError
from hq_work_archive import set_work_archived
from hq_work_timer import snapshot, transition, history, open_timer_jobs
from hq_work_time import time_ledger_snapshot, append_work_time

class PersistentTimerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, HQ_DATA_DIR=self.tmp.name, HQ_SESSION_SECRET="test",
                         GOOGLE_CLIENT_ID="test", GOOGLE_CLIENT_SECRET="test",
                         HQ_ALLOWED_GOOGLE_EMAIL="owner@example.invalid")
        env.start()
        self.addCleanup(env.stop)
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.job = create_test_job()
        with connect() as db:
            db.execute("UPDATE hq_work_records SET status='approved' WHERE id=?", (self.job,))
        self.clock = patch("hq_work_timer.now", return_value=100000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def command(self, action, rev=-1, sid="", operation=None, **kwargs):
        transition(self.job, action, rev, operation or uuid.uuid4().hex, sid, **kwargs)

    def test_server_time_break_multiday_replay_and_atomic_post(self):
        op = uuid.uuid4().hex
        self.command("start", operation=op, allowance="390")
        self.command("start", operation=op, allowance="390")
        row = snapshot(self.job)
        sid = row["id"]
        self.assertEqual(row["allowance_minutes"],390)
        self.now.return_value += 120
        self.command("pause",0,sid)
        self.now.return_value += 86400
        self.assertEqual(snapshot(self.job)["display_seconds"],120)
        self.command("resume",1,sid)
        self.now.return_value += 90
        self.command("finish",2,sid)
        row = snapshot(self.job)
        self.assertEqual((row["state"],row["active_seconds"],row["break_seconds"]),("review",210,86400))
        payload={"operation_id":sid,"minutes":"4","stage":"repair","hourly_cost_pence":"", "note":""}
        with self.assertRaises(ValueError):
            append_work_time(self.job,payload,0,"owner")
        payload["minutes"]="3"
        first=append_work_time(self.job,payload,0,"owner")
        self.assertEqual(append_work_time(self.job,payload,0,"owner"),first)
        self.assertIsNone(snapshot(self.job))
        ledger=time_ledger_snapshot(self.job)
        self.assertEqual(ledger["minutes_total"],3)
        self.assertIsNone(ledger["actual_labour_cost_pence"])
        with connect() as db:
            row=db.execute("SELECT * FROM hq_timer_sessions WHERE id=?",(sid,)).fetchone()
            self.assertEqual((row["state"],row["entry_id"],row["active_seconds"]),("saved",first,210))

    def test_owner_route_csrf_and_reload(self):
        c=app.test_client()
        self.assertEqual(c.get("/hq/work/"+self.job+"/time").status_code,302)
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"]=issue_session(db,"owner")
            s["csrf"]="test"
        url="/hq/work/"+self.job+"/timer"
        data={"action":"start","timer_revision":"-1","operation_id":uuid.uuid4().hex,"allowance_minutes":"390"}
        self.assertEqual(c.post(url,data=data).status_code,403)
        data["csrf"]="test"
        self.assertEqual(c.post(url,data=data).status_code,303)
        self.now.return_value+=60
        page=c.get("/hq/work/"+self.job+"/time")
        self.assertEqual(page.status_code,200)
        self.assertIn(b'Pause / break',page.data)
        self.assertIn(b'data-seconds="60"',page.data)
        self.assertIn(b'id="serverTotalClock"',page.data)
        self.assertIn(b'id="serverBreakClock"',page.data)
        self.assertIn(b'data-break="0" data-paused="no"',page.data)
        self.assertIn(b'data-allowance="390"',page.data)
        other=app.test_client()
        with other.session_transaction() as s, auth_db() as db:
            s["hq_sid"]=issue_session(db,"owner")
            s["csrf"]="test2"
        self.assertIn(b'data-seconds="60"',other.get("/hq/work/"+self.job+"/time").data)

    def test_short_session_correction_and_stale_session(self):
        self.command("start")
        sid=snapshot(self.job)["id"]
        self.now.return_value+=15
        self.command("finish",0,sid)
        with self.assertRaises(ValueError):
            self.command("discard",1,sid)
        self.command("discard",1,sid,reason="Synthetic mistaken start")
        self.command("start")
        with self.assertRaises(ValueError):
            self.command("pause",0,sid)
        with connect() as db:
            row=db.execute("SELECT state,active_seconds FROM hq_timer_sessions WHERE id=?",(sid,)).fetchone()
            self.assertEqual(tuple(row),("discarded",15))
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"],0)

    def test_open_timer_prevents_closure_and_archive_until_reviewed(self):
        # Use the native owner-created source marker in this disposable database.
        with connect() as db:
            db.execute("UPDATE hq_work_records SET source_request_id=? WHERE id=?", ("HQ-" + self.job, self.job))
        change_work_status(self.job, "in_progress", 0)
        self.command("start")
        sid = snapshot(self.job)["id"]
        for action in ("completed", "cancelled"):
            with self.assertRaisesRegex(ValueError, "open job timer"):
                change_work_status(self.job, action, 1)
        with self.assertRaisesRegex(ValueError, "open job timer"):
            set_work_archived(self.job, "archive", 1, uuid.uuid4().hex)
        self.now.return_value += 120
        self.command("pause", 0, sid)
        with self.assertRaisesRegex(ValueError, "open job timer"):
            change_work_status(self.job, "completed", 1)
        self.command("finish", 1, sid)
        with self.assertRaisesRegex(ValueError, "open job timer"):
            change_work_status(self.job, "completed", 1)
        append_work_time(self.job, {"operation_id":sid, "minutes":"2", "stage":"repair",
                                    "hourly_cost_pence":"", "note":""}, 1, "owner")
        self.assertEqual(change_work_status(self.job, "completed", 2), "completed")
        self.assertEqual(set_work_archived(self.job, "archive", 3, uuid.uuid4().hex), 4)
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"], 2)

    def test_active_work_cannot_overlap_across_jobs_but_paused_work_is_recoverable(self):
        other = create_test_job()
        with connect() as db:
            db.execute("UPDATE hq_work_records SET status='approved' WHERE id=?", (other,))
        self.command("start")
        sid = snapshot(self.job)["id"]
        with self.assertRaisesRegex(ValueError, "other job"):
            transition(other, "start", -1, uuid.uuid4().hex)
        self.command("pause", 0, sid)
        transition(other, "start", -1, uuid.uuid4().hex)
        other_sid = snapshot(other)["id"]
        with self.assertRaisesRegex(ValueError, "other job"):
            self.command("resume", 1, sid)
        self.assertEqual(snapshot(self.job)["state"], "paused")
        transition(other, "pause", 0, uuid.uuid4().hex, other_sid)
        self.command("resume", 1, sid)
        self.assertEqual(snapshot(self.job)["state"], "running")

    def test_edit_allowance_preserves_elapsed_breaks_and_replay_history(self):
        self.command("start", allowance="390")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 75
        op = uuid.uuid4().hex
        self.command("allowance", 0, sid, operation=op, allowance="420")
        self.command("allowance", 0, sid, operation=op, allowance="420")
        with self.assertRaises(StaleWorkRecordError) as exc:
            self.command("allowance", 0, sid, operation=op, allowance="421")
        self.assertIn("details changed", str(exc.exception))
        row = snapshot(self.job)
        self.assertEqual((row["revision"], row["display_seconds"], row["allowance_minutes"]), (1,75,420))
        self.command("pause", 1, sid)
        self.now.return_value += 120
        self.command("allowance", 2, sid, allowance="450")
        self.now.return_value += 60
        row = snapshot(self.job)
        self.assertEqual((row["display_seconds"],row["display_break"]), (75,180))
        with self.assertRaises(ValueError):
            self.command("allowance", 3, sid, allowance="")
        self.command("resume", 3, sid)
        self.now.return_value += 45
        self.command("finish", 4, sid)
        with self.assertRaises(ValueError):
            self.command("allowance", 5, sid, allowance="500")
        self.command("discard", 5, sid, reason="Synthetic correction")
        evidence = history(self.job)
        self.assertEqual(evidence["sessions"][0]["state"], "discarded")
        self.assertEqual(evidence["sessions"][0]["active_seconds"], 120)
        self.assertEqual(evidence["sessions"][0]["break_seconds"], 180)
        self.assertEqual([e["detail"] for e in evidence["events"] if e["action"]=="allowance"], ["450","420"])
        self.assertEqual(evidence["events"][0]["detail"], "Synthetic correction")
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"], 0)

    def test_owner_allowance_form_and_history(self):
        c=app.test_client()
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"]=issue_session(db,"owner")
            s["csrf"]="test"
        self.command("start", allowance="390")
        sid=snapshot(self.job)["id"]
        url="/hq/work/"+self.job+"/timer"
        data={"action":"allowance","timer_revision":"0","timer_session":sid,
              "operation_id":uuid.uuid4().hex,"allowance_minutes":"415"}
        self.assertEqual(c.post(url,data=data).status_code,403)
        data["csrf"]="test"
        self.assertEqual(c.post(url,data=data).status_code,303)
        page=c.get("/hq/work/"+self.job+"/time")
        self.assertIn(b'Save estimated allowance',page.data)
        self.assertIn(b'data-allowance="415"',page.data)
        self.assertIn(b'Persistent timer audit history',page.data)
        self.assertIn(b'415 min allowance',page.data)
        anonymous=app.test_client()
        self.assertEqual(anonymous.get("/hq/work/"+self.job+"/time").status_code,302)

    def test_next_session_suggests_last_saved_allowance_not_discarded(self):
        self.command("start", allowance="390")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 120
        self.command("finish", 0, sid)
        append_work_time(self.job, {"operation_id":sid, "minutes":"2", "stage":"repair",
                                    "hourly_cost_pence":"", "note":""}, 0, "owner")
        self.now.return_value += 1
        self.command("start", allowance="600")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 15
        self.command("finish", 0, sid)
        self.command("discard", 1, sid, reason="Synthetic mistaken start")
        self.assertEqual(history(self.job)["previous_saved_allowance"], 390)
        c=app.test_client()
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"]=issue_session(db,"owner")
            s["csrf"]="test"
        page=c.get("/hq/work/"+self.job+"/time")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'placeholder="e.g. 390" value="390"',page.data)
        self.assertIn(b'Previous saved session estimate suggested',page.data)
        self.assertIn(b'2 minutes</strong> already saved against this job',page.data)
        self.assertIn(b'Change job',page.data)
        self.assertIsNone(snapshot(self.job))
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"],2)

    def test_recovery_queue_survives_search_and_excludes_finished_sessions(self):
        self.command("start")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 120
        self.command("pause", 0, sid)
        other = create_test_job()
        with connect() as db:
            db.execute("UPDATE hq_work_records SET status='approved' WHERE id=?", (other,))
        transition(other, "start", -1, uuid.uuid4().hex)
        self.assertEqual([(r["id"],r["state"]) for r in open_timer_jobs()["records"]],
                         [(other,"running"), (self.job,"paused")])
        anonymous = app.test_client()
        self.assertEqual(anonymous.get("/hq/timer").status_code, 302)
        c = app.test_client()
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db,"owner")
            s["csrf"] = "test"
        page = c.get("/hq/timer?q=not-a-real-synthetic-job")
        self.assertEqual(page.status_code,200)
        self.assertIn(b"Open running timer",page.data)
        self.assertIn(b"Open paused timer",page.data)
        self.assertIn(("/hq/work/"+self.job+"/time").encode(),page.data)
        self.assertIn(b"No matching HQ jobs",page.data)
        self.assertEqual(snapshot(self.job)["state"],"paused")
        self.command("finish", 1, sid)
        self.assertIn(b"Review time",c.get("/hq/timer").data)
        append_work_time(self.job, {"operation_id":sid, "minutes":"2", "stage":"repair",
                                  "hourly_cost_pence":"", "note":""}, 0, "owner")
        self.assertEqual([r["id"] for r in open_timer_jobs()["records"]],[other])
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"],2)

    def test_review_correction_is_atomic_audited_and_replay_safe(self):
        self.command("start")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 600
        self.command("finish", 0, sid)
        payload = {"operation_id":sid, "minutes":"8", "stage":"repair",
                   "hourly_cost_pence":"", "note":""}
        with self.assertRaises(ValueError):
            append_work_time(self.job, payload, 0, "owner")
        payload["timer_correction_reason"] = "Synthetic missed two-minute break"
        with self.assertRaises(ValueError):
            append_work_time(self.job, dict(payload, minutes="11"), 0, "owner")
        with self.assertRaises(ValueError):
            append_work_time(self.job, dict(payload, minutes="10"), 0, "owner")
        c = app.test_client()
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db,"owner")
            s["csrf"] = "test"
        url = "/hq/work/" + self.job + "/time"
        page = c.get(url)
        self.assertIn(b'name="timer_correction_reason"', page.data)
        self.assertIn(b'Measured 10 whole minutes', page.data)
        # Mirror bind_session: audited writes require the authenticated owner subject.
        with c.session_transaction() as s:
            s["google_sub"] = "owner"
        data = dict(payload, csrf="test", revision="0")
        response = c.post(url, data=data)
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True))
        self.assertEqual(c.post(url, data=data).status_code,303)
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"],8)
        self.assertIsNone(time_ledger_snapshot(self.job)["actual_labour_cost_pence"])
        evidence = history(self.job)
        self.assertEqual(evidence["sessions"][0]["active_seconds"],600)
        self.assertEqual(evidence["sessions"][0]["state"],"saved")
        corrections = [e for e in evidence["events"] if e["action"] == "review_correction"]
        self.assertEqual(len(corrections),1)
        self.assertIn("600 measured seconds; 8 approved minutes",corrections[0]["detail"])
        self.assertIn(payload["timer_correction_reason"],corrections[0]["detail"])
        self.assertIn(b'Synthetic missed two-minute break',c.get(url).data)
        with self.assertRaises(StaleWorkRecordError):
            append_work_time(self.job,dict(payload,minutes="7"),0,"owner")
        with self.assertRaises(ValueError):
            append_work_time(self.job,dict(payload,operation_id=uuid.uuid4().hex),1,"owner")

    def test_timer_review_pounds_rate_posts_exact_pence_and_legacy_replay(self):
        self.command("start")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 1800
        self.command("finish", 0, sid)
        c = app.test_client()
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["csrf"] = "test"
            s["google_sub"] = "owner"
        url = "/hq/work/" + self.job + "/time"
        page = c.get(url)
        self.assertIn(b'name="hourly_cost_pounds"', page.data)
        self.assertIn(b'id="labourCostPreview" data-saved-cost="0" data-unknown-rates="0" data-max-minutes="30"', page.data)
        self.assertIn(b'Projected recorded job labour cost after saving', page.data)
        self.assertEqual(time_ledger_snapshot(self.job)["entry_count"], 0)
        data = {"operation_id": sid, "minutes": "30", "stage": "repair",
                "hourly_cost_pounds": "18.25", "note": "", "csrf": "test", "revision": "0"}
        for invalid in ("18.255", "-1", "1e2", "10000.01"):
            self.assertEqual(c.post(url, data=dict(data, hourly_cost_pounds=invalid)).status_code, 400)
        self.assertEqual(c.post(url, data=dict(data, hourly_cost_pence="1825")).status_code, 400)
        self.assertEqual(time_ledger_snapshot(self.job)["entry_count"], 0)
        response = c.post(url, data=data)
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True))
        legacy = dict(data, hourly_cost_pence="1825")
        del legacy["hourly_cost_pounds"]
        self.assertEqual(c.post(url, data=legacy).status_code, 303)
        ledger = time_ledger_snapshot(self.job)
        self.assertEqual(ledger["entry_count"], 1)
        self.assertEqual(ledger["entries"][0]["hourly_cost_pence"], 1825)
        self.assertEqual(ledger["actual_labour_cost_pence"], 913)
        self.assertIn("£9.13", c.get(url).get_data(as_text=True))
