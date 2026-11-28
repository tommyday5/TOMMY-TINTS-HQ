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
from hq_work_time import time_ledger_snapshot, append_work_time, void_work_time, correct_work_time_rate, correct_work_time_minutes

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

    def test_clear_timer_allowance_keeps_time_and_historical_alerts(self):
        from hq_timer_alerts import scan, inbox
        self.command("start", allowance="10")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 480
        self.assertEqual(scan(), 1)
        self.command("pause", 0, sid)
        self.now.return_value += 60
        c = app.test_client()
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["csrf"] = "test"
        page = "/hq/work/" + self.job + "/time"
        url = "/hq/work/" + self.job + "/timer"
        self.assertIn(b"Clear estimated allowance", c.get(page).data)
        data = dict(action="clear_allowance", timer_revision="1", timer_session=sid,
                    operation_id=uuid.uuid4().hex, reason="Synthetic estimate withdrawn")
        self.assertEqual(c.post(url, data=data).status_code, 403)
        self.assertEqual(c.post(url, data=dict(data, csrf="test", reason="")).status_code, 400)
        before = snapshot(self.job)
        self.assertEqual(c.post(url, data=dict(data, csrf="test")).status_code, 303)
        self.assertEqual(c.post(url, data=dict(data, csrf="test")).status_code, 303)
        after = snapshot(self.job)
        self.assertIsNone(after["allowance_minutes"])
        self.assertEqual(after["revision"], 2)
        for key in ("changed_at", "display_seconds", "display_break", "state"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(after["display_seconds"], 480)
        self.assertEqual(after["display_break"], 60)
        self.assertEqual(scan(), 0)
        self.assertFalse(inbox()["pending"][0]["same_open_allowance"])
        refreshed = c.get(page)
        self.assertNotIn(b"Clear estimated allowance", refreshed.data)
        self.assertIn(b'data-allowance="0"', refreshed.data)
        self.assertIn(b"Synthetic estimate withdrawn", refreshed.data)
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"], 0)
        self.assertEqual(open_timer_jobs()["records"][0]["budget_state"], "unknown")
        with self.assertRaises(StaleWorkRecordError):
            self.command("clear_allowance", 1, sid, operation=data["operation_id"],
                         reason="Different reason")
        self.command("resume", 2, sid)
        self.now.return_value += 120
        self.assertEqual(snapshot(self.job)["display_seconds"], 600)
        self.assertEqual(scan(), 0)
        self.command("allowance", 3, sid, allowance="20")
        self.assertEqual(snapshot(self.job)["allowance_minutes"], 20)
        self.command("clear_allowance", 4, sid, reason="Synthetic running estimate withdrawn")
        self.assertIsNone(snapshot(self.job)["allowance_minutes"])
        self.command("finish", 5, sid)
        with self.assertRaises(ValueError):
            self.command("clear_allowance", 6, sid, reason="Cannot change a reviewed session")
        self.assertEqual(snapshot(self.job)["active_seconds"], 600)

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

    def test_recovery_pages_expose_older_paused_sessions_independent_of_search(self):
        ids = [self.job] + [create_test_job() for _ in range(50)]
        with connect() as db:
            db.executemany("UPDATE hq_work_records SET status='approved' WHERE id=?",
                           [(job,) for job in ids])
        for job in ids:
            transition(job, "start", -1, uuid.uuid4().hex)
            self.now.return_value += 60
            transition(job, "pause", 0, uuid.uuid4().hex, snapshot(job)["id"])
        first = open_timer_jobs()
        older = open_timer_jobs(50)
        self.assertEqual(first["total"], 51)
        self.assertEqual(len(first["records"]), 50)
        self.assertTrue(first["has_more"])
        self.assertEqual([r["id"] for r in older["records"]], [self.job])
        self.assertFalse(older["has_more"])
        c = app.test_client()
        self.assertEqual(c.get("/hq/timer?recovery_offset=50").status_code, 302)
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["csrf"] = "test"
        first_page = c.get("/hq/timer?q=synthetic-no-match")
        self.assertIn(b"Next unfinished timers", first_page.data)
        self.assertIn(b"51 unfinished sessions", first_page.data)
        page = c.get("/hq/timer?q=synthetic-no-match&recovery_offset=50")
        self.assertEqual(page.status_code, 200)
        self.assertIn("no-store", page.headers["Cache-Control"])
        self.assertIn(b"Previous unfinished timers", page.data)
        self.assertIn(b"No matching HQ jobs", page.data)
        self.assertIn(("/hq/work/" + self.job + "/time").encode(), page.data)
        self.assertNotIn(("/hq/work/" + ids[-1] + "/time").encode(), page.data)
        self.assertIn(b"No unfinished timers on this page",
                      c.get("/hq/timer?recovery_offset=100").data)
        for invalid in ("-1", "100001", "not-a-number"):
            self.assertEqual(c.get("/hq/timer?recovery_offset=" + invalid).status_code, 400)
        self.assertEqual(snapshot(self.job)["state"], "paused")
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"], 0)

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

    def test_stale_manual_form_recovers_new_finished_timer_without_carrying_old_values(self):
        c = app.test_client()
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["csrf"] = "test"
            s["google_sub"] = "owner"
        url = "/hq/work/" + self.job + "/time"
        self.assertEqual(c.get(url).status_code, 200)
        stale = {"operation_id": uuid.uuid4().hex, "minutes": "17",
                 "stage": "paint", "hourly_cost_pounds": "25.00",
                 "note": "Old unsaved manual entry", "revision": "0", "csrf": "test"}
        # Simulate another authenticated device finishing a persistent session.
        self.command("start", allowance="390")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 600
        self.command("finish", 0, sid)
        response = c.post(url, data=stale)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        page = response.get_data(as_text=True)
        self.assertIn("A different timer is awaiting review", page)
        self.assertIn('name="operation_id" value="' + sid + '"', page)
        self.assertIn('value="10"', page)
        self.assertNotIn("Old unsaved manual entry", page)
        self.assertNotIn('value="25.00"', page)
        self.assertNotIn('value="paint" selected', page)
        self.assertEqual(time_ledger_snapshot(self.job)["entry_count"], 0)
        self.assertEqual(snapshot(self.job)["state"], "review")
        correct = dict(stale, operation_id=sid, minutes="10", stage="repair",
                       hourly_cost_pounds="", note="")
        self.assertEqual(c.post(url, data=correct).status_code, 303)
        self.assertEqual(c.post(url, data=correct).status_code, 303)
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"], 10)
        self.assertEqual(time_ledger_snapshot(self.job)["entry_count"], 1)
        self.assertIsNone(snapshot(self.job))

    def test_obsolete_timer_submission_cannot_restore_corrected_or_reversed_time(self):
        c = app.test_client()
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["csrf"] = "test"
            s["google_sub"] = "owner"
        url = "/hq/work/" + self.job + "/time"
        self.command("start")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 600
        self.command("finish", 0, sid)
        data = {"operation_id": sid, "minutes": "10", "stage": "repair",
                "hourly_cost_pounds": "25.00", "note": "Synthetic old timer",
                "csrf": "test", "revision": "0"}
        self.assertIn("saved=1", c.post(url, data=data).location)
        self.assertIn("saved=1", c.post(url, data=data).location)
        original = time_ledger_snapshot(self.job)["entries"][0]["entry_id"]
        successor = correct_work_time_minutes(self.job, original, {
            "operation_id": uuid.uuid4().hex, "minutes": "8",
            "reason": "Synthetic missed break"}, 1, "owner")
        for replay in (data, dict(data, minutes="9")):
            response = c.post(url, data=replay)
            self.assertEqual(response.status_code, 409)
            page = response
            self.assertEqual(page.headers["Cache-Control"], "no-store")
            self.assertIn(b"Previous submission not saved again", page.data)
            self.assertNotIn(b"Time entry saved to HQ", page.data)
            self.assertNotIn(('name="operation_id" value="' + sid + '"').encode(), page.data)
            self.assertIn(b'name="minutes" type="number" min="1" max="1440" step="1" required  value=""', page.data)
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"], 8)
        void_work_time(self.job, successor, 2, "owner")
        self.assertEqual(c.post(url, data=data).status_code, 409)
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"], 0)
        self.assertEqual(history(self.job)["sessions"][0]["active_seconds"], 600)

    def test_discarded_timer_review_replay_requires_fresh_ledger_review(self):
        c = app.test_client()
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["csrf"] = "test"
            s["google_sub"] = "owner"
        self.command("start")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 600
        self.command("finish", 0, sid)
        self.command("discard", 1, sid, reason="Synthetic mistaken session")
        url = "/hq/work/" + self.job + "/time"
        response = c.post(url, data={"operation_id": sid, "minutes": "10",
            "stage": "repair", "csrf": "test", "revision": "0"})
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertIn(b"Previous submission not saved again", response.data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn(('name="operation_id" value="' + sid + '"').encode(), response.data)
        self.assertEqual(time_ledger_snapshot(self.job)["entry_count"], 0)
        self.assertEqual(history(self.job)["sessions"][0]["state"], "discarded")

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


    def test_visible_activity_totals_include_older_entries_and_exclude_reversals(self):
        # Seed via the actual immutable ledger, including explicit zero and unknown costs.
        def add(rev, stage, minutes, rate):
            return append_work_time(self.job, {
                "operation_id": uuid.uuid4().hex, "minutes": str(minutes),
                "stage": stage, "hourly_cost_pence": rate, "note": "",
            }, rev, "owner")

        old = add(0, "repair", 30, "1825")
        for revision in range(1, 102):
            add(revision, "paint", 1, "0")
        missing = add(102, "repair", 15, "")
        ledger = time_ledger_snapshot(self.job)
        by_stage = {row["stage"]: row for row in ledger["stage_totals"]}
        self.assertEqual(len(ledger["entries"]), 100)
        self.assertTrue(ledger["history_truncated"])
        self.assertNotIn(old, [row["entry_id"] for row in ledger["entries"]])
        self.assertEqual(by_stage["repair"]["minutes_total"], 45)
        self.assertEqual(by_stage["repair"]["known_cost_subtotal_pence"], 913)
        self.assertIsNone(by_stage["repair"]["actual_labour_cost_pence"])
        self.assertEqual(by_stage["paint"]["actual_labour_cost_pence"], 0)
        self.assertEqual(sum(row["minutes_total"] for row in by_stage.values()), ledger["minutes_total"])

        c = app.test_client()
        url = "/hq/work/" + self.job + "/time"
        self.assertEqual(c.get(url).status_code, 302)
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["csrf"] = "test"
        page = c.get(url).get_data(as_text=True)
        self.assertIn('id="recordedStageTotals"', page)
        self.assertIn('Repair / bodywork</strong> · 45 min · 2 entries', page)
        self.assertIn('Known-rate subtotal only: £9.13', page)
        void_work_time(self.job, missing, 103, "owner")
        # Reverse enough entries that the active count drops below 100;
        # history still contains older immutable evidence.
        paint_entries = [row["entry_id"] for row in ledger["entries"] if row["stage"] == "paint"]
        for revision, entry_id in enumerate(paint_entries[:4], start=104):
            void_work_time(self.job, entry_id, revision, "owner")
        ledger = time_ledger_snapshot(self.job)
        by_stage = {row["stage"]: row for row in ledger["stage_totals"]}
        self.assertEqual(ledger["entry_count"], 98)
        self.assertTrue(ledger["history_truncated"])
        self.assertEqual(by_stage["repair"]["minutes_total"], 30)
        self.assertEqual(by_stage["repair"]["actual_labour_cost_pence"], 913)
        self.assertEqual(by_stage["paint"]["minutes_total"], 97)
        self.assertIn('Repair / bodywork</strong> · 30 min · 1 entries', c.get(url).get_data(as_text=True))


    def test_saved_timer_rate_correction_preserves_time_and_evidence(self):
        from hq_work_cost_overview import cost_overview_snapshot
        self.command("start", allowance="390")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 1800
        self.command("finish", 0, sid)
        original = append_work_time(self.job, {
            "operation_id": sid, "minutes": "30", "stage": "repair",
            "hourly_cost_pence": "", "note": "Synthetic repair",
        }, 0, "owner")
        c = app.test_client()
        url = "/hq/work/" + self.job + "/time/" + original + "/rate"
        payload = {"operation_id": uuid.uuid4().hex, "revision": "1",
                   "hourly_cost_pounds": "18.25", "reason": "Verified labour rate"}
        self.assertEqual(c.post(url, data=payload).status_code, 302)
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["google_sub"] = "owner"
            s["csrf"] = "test"
        self.assertEqual(c.post(url, data=payload).status_code, 403)
        payload["csrf"] = "test"
        self.assertIn("rate_corrected=1", c.post(url, data=payload).location)
        self.assertIn("rate_corrected=1", c.post(url, data=payload).location)
        ledger = time_ledger_snapshot(self.job)
        self.assertEqual((ledger["revision"], ledger["entry_count"], ledger["minutes_total"],
                          ledger["actual_labour_cost_pence"]), (2, 1, 30, 913))
        entries = {row["entry_id"]: row for row in ledger["entries"]}
        successor = entries[original]["replaced_by"]
        self.assertIsNotNone(entries[original]["voided_at"])
        self.assertIsNone(entries[original]["hourly_cost_pence"])
        self.assertEqual(entries[successor]["corrected_from"], original)
        self.assertEqual(entries[successor]["rate_correction_reason"], "Verified labour rate")
        self.assertEqual(entries[successor]["note"], "Synthetic repair")
        self.assertEqual(history(self.job)["sessions"][0]["entry_id"], original)
        self.assertEqual(cost_overview_snapshot(self.job)["labour_recorded_total_pence"], 913)
        page = c.get("/hq/work/" + self.job + "/time").get_data(as_text=True)
        self.assertIn("Correct internal hourly cost (keep minutes)", page)
        self.assertIn("Verified labour rate", page)
        stale = dict(payload, operation_id=uuid.uuid4().hex, hourly_cost_pounds="0")
        with self.assertRaises(StaleWorkRecordError):
            correct_work_time_rate(self.job, successor, stale, 1, "owner")
        with self.assertRaises(ValueError):
            correct_work_time_rate(self.job, successor, dict(stale, reason=""), 2, "owner")
        zero = correct_work_time_rate(self.job, successor, stale, 2, "owner")
        self.assertEqual(time_ledger_snapshot(self.job)["actual_labour_cost_pence"], 0)
        unknown = dict(stale, operation_id=uuid.uuid4().hex, hourly_cost_pounds="")
        correct_work_time_rate(self.job, zero, unknown, 3, "owner")
        final = time_ledger_snapshot(self.job)
        self.assertEqual((final["entry_count"], final["minutes_total"], final["revision"]), (1, 30, 4))
        self.assertIsNone(final["actual_labour_cost_pence"])
        self.assertIsNone(cost_overview_snapshot(self.job)["labour_recorded_total_pence"])


    def test_saved_minutes_correction_replays_and_preserves_original_timer(self):
        from hq_work_cost_overview import cost_overview_snapshot
        self.command("start", allowance="390")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 3600
        self.command("finish", 0, sid)
        original = append_work_time(self.job, {
            "operation_id": sid, "minutes": "60", "stage": "repair",
            "hourly_cost_pounds": "18.25", "note": "Synthetic repair",
        }, 0, "owner")
        c = app.test_client()
        url = "/hq/work/" + self.job + "/time/" + original + "/minutes"
        payload = {"operation_id": uuid.uuid4().hex, "revision": "1",
                   "minutes": "45", "reason": "Forgot to pause"}
        self.assertEqual(c.post(url, data=payload).status_code, 302)
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["google_sub"] = "owner"
            s["csrf"] = "test"
        self.assertEqual(c.post(url, data=payload).status_code, 403)
        payload["csrf"] = "test"
        for invalid in ("0", "60", "61", "1.5"):
            self.assertIn("minutes_invalid=1", c.post(url, data=dict(payload, minutes=invalid)).location)
        self.assertIn("minutes_invalid=1", c.post(url, data=dict(payload, reason="")).location)
        self.assertIn("minutes_corrected=1", c.post(url, data=payload).location)
        self.assertIn("minutes_corrected=1", c.post(url, data=payload).location)
        self.assertIn("conflict=1", c.post(url, data=dict(payload, minutes="44")).location)
        ledger = time_ledger_snapshot(self.job)
        self.assertEqual((ledger["revision"], ledger["entry_count"], ledger["minutes_total"],
                          ledger["actual_labour_cost_pence"]), (2, 1, 45, 1369))
        entries = {row["entry_id"]: row for row in ledger["entries"]}
        successor = entries[original]["replaced_by"]
        self.assertEqual(entries[original]["minutes"], 60)
        self.assertIsNotNone(entries[original]["voided_at"])
        self.assertEqual(entries[successor]["minutes_corrected_from"], original)
        self.assertEqual(entries[successor]["minute_correction_reason"], "Forgot to pause")
        self.assertEqual(entries[successor]["hourly_cost_pence"], 1825)
        self.assertEqual(entries[successor]["note"], "Synthetic repair")
        self.assertEqual(history(self.job)["sessions"][0]["entry_id"], original)
        self.assertEqual(history(self.job)["sessions"][0]["active_seconds"], 3600)
        self.assertEqual(cost_overview_snapshot(self.job)["labour_recorded_total_pence"], 1369)
        page = c.get("/hq/work/" + self.job + "/time").get_data(as_text=True)
        self.assertIn("Reduce saved working minutes", page)
        self.assertIn("Forgot to pause", page)
        with self.assertRaises(StaleWorkRecordError):
            correct_work_time_minutes(self.job, successor, dict(payload, operation_id=uuid.uuid4().hex, minutes="40"), 1, "owner")
        corrected_rate = correct_work_time_rate(self.job, successor, {
            "operation_id": uuid.uuid4().hex, "hourly_cost_pounds": "", "reason": "Rate unverified"
        }, 2, "owner")
        corrected_minutes = correct_work_time_minutes(self.job, corrected_rate, {
            "operation_id": uuid.uuid4().hex, "minutes": "40", "reason": "Additional break"
        }, 3, "owner")
        ledger = time_ledger_snapshot(self.job)
        self.assertEqual((ledger["revision"], ledger["entry_count"], ledger["minutes_total"]), (4, 1, 40))
        self.assertIsNone(ledger["actual_labour_cost_pence"])
        entries = {row["entry_id"]: row for row in ledger["entries"]}
        self.assertEqual(entries[successor]["replaced_by"], corrected_rate)
        self.assertEqual(entries[corrected_rate]["replaced_by"], corrected_minutes)


    def test_timer_cost_allocations_edit_in_pounds_and_reload(self):
        from hq_work_materials import append_work_material, material_ledger_snapshot
        from hq_work_expenses import append_work_expense, expense_ledger_snapshot
        from hq_work_cost_overview import cost_overview_snapshot
        with connect() as db:
            db.execute("UPDATE hq_work_records SET price_pence=55000 WHERE id=?", (self.job,))
        c = app.test_client()
        url = "/hq/work/" + self.job
        self.assertEqual(c.get(url + "/time").status_code, 302)
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["google_sub"] = "owner"
            s["csrf"] = "test"
        append_work_time(self.job, {"operation_id": uuid.uuid4().hex, "minutes": "60",
            "stage": "repair", "hourly_cost_pounds": "18.25"}, 0, "owner")
        page = c.get(url + "/time")
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(b"Combined total and price less costs are unknown", page.data)
        self.assertIn(b'name="actual_cost_pounds"', c.get(url + "/materials").data)
        self.assertIn(b'name="allocated_cost_pounds"', c.get(url + "/expenses").data)
        material = {"operation_id": uuid.uuid4().hex, "revision": "1", "csrf": "test",
                    "material_name": "Synthetic primer", "unit": "ml", "quantity": "150",
                    "actual_cost_pounds": "12.34"}
        expense = {"operation_id": uuid.uuid4().hex, "revision": "2", "csrf": "test",
                   "expense_type": "overhead", "description": "Synthetic allocation",
                   "allocated_cost_pounds": "5.01"}
        for invalid in ("12.345", "-1", "1000000.01"):
            self.assertEqual(c.post(url + "/materials", data=dict(material, actual_cost_pounds=invalid)).status_code, 400)
        self.assertEqual(c.post(url + "/materials", data=dict(material, actual_cost_pence="1234")).status_code, 400)
        self.assertEqual(c.post(url + "/materials", data=material).status_code, 303)
        legacy = dict(material, actual_cost_pence="1234")
        del legacy["actual_cost_pounds"]
        append_work_material(self.job, legacy, 1, "owner")
        self.assertEqual(c.post(url + "/expenses", data=dict(expense, allocated_cost_pounds="5.001")).status_code, 400)
        self.assertEqual(c.post(url + "/expenses", data=expense).status_code, 303)
        legacy_expense = dict(expense, allocated_cost_pence="501")
        del legacy_expense["allocated_cost_pounds"]
        append_work_expense(self.job, legacy_expense, 2, "owner")
        self.assertEqual(cost_overview_snapshot(self.job)["recorded_three_ledger_total_pence"], 3560)
        page = c.get(url + "/time").get_data(as_text=True)
        self.assertIn("£12.34", page)
        self.assertIn("£5.01", page)
        self.assertIn("£35.60", page)
        self.assertIn("£514.40", page)
        self.assertIn("not final profit", page)
        with patch("hq_native_work.cost_overview_snapshot", return_value={"revision": 999}):
            page = c.get(url + "/time").get_data(as_text=True)
            self.assertIn("Combined cost snapshot unavailable", page)
            self.assertNotIn("£514.40", page)
        with patch("hq_native_work.cost_overview_snapshot", side_effect=OSError("synthetic")):
            self.assertEqual(c.get(url + "/time").status_code, 200)


    def test_recovery_budget_uses_saved_active_minutes_and_excludes_breaks(self):
        entry = append_work_time(self.job, {"operation_id": uuid.uuid4().hex,
            "minutes": "60", "stage": "repair", "hourly_cost_pounds": "18.25"}, 0, "owner")
        self.command("start", allowance="100")
        sid = snapshot(self.job)["id"]
        c = app.test_client()
        self.assertEqual(c.get("/hq/timer").status_code, 302)
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["google_sub"] = "owner"
            s["csrf"] = "test"
        self.now.return_value += 1199
        row = open_timer_jobs()["records"][0]
        self.assertEqual((row["budget_state"], row["remaining_seconds"]), ("within", 1201))
        self.now.return_value += 1
        page = c.get("/hq/timer?q=unmatched-synthetic")
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(b"Approaching labour allowance", page.data)
        self.assertIn(b"20m 0s remaining", page.data)
        self.command("pause", 0, sid)
        self.now.return_value += 86400
        row = open_timer_jobs()["records"][0]
        self.assertEqual((row["total_seconds"], row["budget_state"]), (4800, "near"))
        self.command("resume", 1, sid)
        self.now.return_value += 1200
        self.assertIn(b"Allowance reached", c.get("/hq/timer").data)
        self.now.return_value += 61
        self.assertIn(b"Allowance exceeded</strong> by 1m 1s", c.get("/hq/timer").data)
        self.command("finish", 2, sid)
        self.now.return_value += 5000
        self.assertEqual(open_timer_jobs()["records"][0]["remaining_seconds"], -61)
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"], 60)
        self.assertEqual(snapshot(self.job)["state"], "review")
        # A second job without an allowance cannot generate an overrun.
        other = create_test_job()
        with connect() as db:
            db.execute("UPDATE hq_work_records SET status='approved' WHERE id=?", (other,))
        transition(other, "start", -1, uuid.uuid4().hex)
        self.assertEqual(open_timer_jobs()["records"][0]["budget_state"], "unknown")
        self.assertIn(b"Labour allowance not recorded", c.get("/hq/timer").data)


    def test_live_status_cross_device_revision_and_breaks(self):
        c = app.test_client()
        url = "/hq/work/" + self.job + "/timer/status"
        self.assertEqual(c.get(url).status_code, 302)
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["google_sub"] = "owner"
            s["csrf"] = "test"
        standby = c.get(url)
        self.assertEqual(standby.status_code, 200)
        self.assertEqual(standby.headers["Cache-Control"], "no-store")
        self.assertEqual(standby.json["state"], "standby")
        self.assertIsNone(standby.json["session_id"])
        self.assertEqual(c.get("/hq/work/" + uuid.uuid4().hex + "/timer/status").status_code, 404)
        self.command("start", allowance="390")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 121
        running = c.get(url).json
        self.assertEqual((running["active_seconds"], running["break_seconds"], running["timer_revision"]), (121, 0, 0))
        self.assertEqual(set(running), {"work_revision", "saved_minutes", "session_id",
            "timer_revision", "state", "active_seconds", "break_seconds", "allowance_minutes"})
        self.command("pause", 0, sid)
        self.now.return_value += 86400
        paused = c.get(url).json
        self.assertEqual((paused["active_seconds"], paused["break_seconds"], paused["timer_revision"]), (121, 86400, 1))
        self.command("resume", 1, sid)
        self.now.return_value += 59
        self.command("finish", 2, sid)
        self.assertEqual(c.get(url).json["state"], "review")
        append_work_time(self.job, {"operation_id": sid, "minutes": "3",
            "stage": "repair", "hourly_cost_pounds": ""}, 0, "owner")
        saved = c.get(url).json
        self.assertEqual((saved["state"], saved["saved_minutes"], saved["work_revision"]), ("standby", 3, 1))
        self.assertIsNone(saved["session_id"])
        page = c.get("/hq/work/" + self.job + "/time")
        self.assertIn(b'data-work-revision="1"', page.data)
        self.assertIn(b'data-status-url=', page.data)
        self.assertIn(b'id="serverSyncNotice"', page.data)


    def test_durable_timer_alerts_scan_acknowledge_and_no_live_job_changes(self):
        from hq_timer_alerts import scan, inbox
        c = app.test_client()
        url = "/hq/timer/alerts"
        self.assertEqual(c.get(url).status_code, 302)
        self.assertEqual(c.post(url, data={"action":"scan"}).status_code, 302)
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["google_sub"] = "owner"
            s["csrf"] = "test"
        self.assertEqual(c.post(url, data={"action":"scan"}).status_code, 403)
        self.command("start", allowance="10")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 479
        self.assertEqual(scan(), 0)
        self.now.return_value += 1
        self.assertEqual(scan(), 1)
        self.assertEqual(scan(), 0)
        pending = inbox()["pending"]
        self.assertEqual(len(pending), 1)
        near = pending[0]["id"]
        self.assertEqual((pending[0]["level"], pending[0]["observed_seconds"]), ("near",480))
        page = c.get(url)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(b"Approaching labour allowance", page.data)
        self.assertIn(b"Labour alerts", c.get("/hq/timer").data)
        data = {"action":"acknowledge","alert_id":near,"csrf":"test"}
        self.assertEqual(c.post(url,data=data).status_code,303)
        self.now.return_value += 1
        self.assertEqual(c.post(url,data=data).status_code,303)
        self.assertEqual(inbox()["pending"], [])
        ack = inbox()["recent"][0]
        self.assertEqual((ack["acknowledged_by"],ack["acknowledged_at"]),("owner",100480))
        self.command("pause",0,sid)
        self.now.return_value += 86400
        self.assertEqual(scan(),0)
        self.command("resume",1,sid)
        self.now.return_value += 119
        # CLI exercises the same scanner without a browser session.
        cli = app.test_cli_runner().invoke(args=["native_work","scan-timer-alerts"])
        self.assertEqual(cli.exit_code,0,cli.output)
        self.assertIn("Timer alerts created: 1",cli.output)
        limit = inbox()["pending"][0]
        self.assertEqual((limit["level"],limit["observed_seconds"]),("limit",600))
        self.assertEqual(c.post(url,data={"action":"scan","csrf":"test"}).status_code,303)
        self.assertEqual(len(inbox()["pending"]),1)
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"],0)
        self.assertEqual(snapshot(self.job)["state"],"running")
        self.command("allowance",2,sid,allowance="20")
        self.assertFalse(inbox()["pending"][0]["same_open_allowance"])
        self.assertEqual(scan(),0)
        self.assertEqual(c.post(url,data={"action":"acknowledge","alert_id":uuid.uuid4().hex,"csrf":"test"}).status_code,400)

    def test_timer_alert_scan_includes_saved_minutes_skips_unknown_and_does_not_backfill_near(self):
        from hq_timer_alerts import scan, inbox
        append_work_time(self.job, {"operation_id":uuid.uuid4().hex,"minutes":"9",
            "stage":"repair","hourly_cost_pounds":""},0,"owner")
        self.command("start")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 60
        self.assertEqual(scan(),0)
        self.command("allowance",0,sid,allowance="10")
        self.assertEqual(scan(),1)
        self.assertEqual([(x["level"],x["observed_seconds"]) for x in inbox()["pending"]],[("limit",600)])
        self.assertEqual(scan(),0)


    def test_background_scan_heartbeat_without_browser_and_manual_scan_not_health(self):
        from hq_timer_alerts import scan, inbox
        from hq_timer_background import run, INTERVAL_SECONDS
        from unittest.mock import Mock
        self.command("start", allowance="10")
        self.now.return_value += 479
        self.assertIsNone(inbox()["automatic_checked_utc"])
        self.assertEqual(scan(), 0)
        self.assertIsNone(inbox()["automatic_checked_utc"])
        self.now.return_value += 1
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        logger = Mock()
        run(stop, logger)
        stop.wait.assert_called_once_with(INTERVAL_SECONDS)
        logger.info.assert_called_once()
        data = inbox()
        self.assertTrue(data["automatic_recent"])
        self.assertEqual(len(data["pending"]), 1)
        heartbeat = data["automatic_checked_utc"]
        self.now.return_value += 181
        self.assertEqual(scan(), 1)
        self.assertFalse(inbox()["automatic_recent"])
        self.assertEqual(inbox()["automatic_checked_utc"], heartbeat)
        self.assertEqual(scan(automatic=True), 0)
        self.assertTrue(inbox()["automatic_recent"])
        self.assertEqual(len(inbox()["pending"]), 2)
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"], 0)
        self.assertEqual(snapshot(self.job)["state"], "running")

    def test_background_scan_retries_failure_and_worker_lifecycle_is_guarded(self):
        import sqlite3
        from types import SimpleNamespace
        from unittest.mock import Mock
        from hq_timer_background import run, start, shutdown
        stop, logger = Mock(), Mock()
        stop.is_set.side_effect = [False, False, True]
        with patch("hq_timer_background.hq_timer_alerts.scan",
                   side_effect=[sqlite3.OperationalError("private data"), 0]) as scanner:
            run(stop, logger)
        self.assertEqual(scanner.call_count, 2)
        logger.warning.assert_called_once_with(
            "Private timer alert scan failed (%s); retrying", "OperationalError")
        logger.info.assert_called_once()
        worker = SimpleNamespace(log=Mock())
        with patch("hq_timer_background.Thread") as thread, patch(
                "hq_timer_background.storage_ready", return_value=False):
            start(worker)
            thread.assert_not_called()
        with patch("hq_timer_background.Thread") as thread, patch(
                "hq_timer_background.storage_ready", return_value=True):
            start(worker)
            start(worker)
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
            shutdown(worker)
            self.assertTrue(worker._hq_timer_alert_stop.is_set())
            thread.return_value.join.assert_called_once_with(timeout=1)


    def test_timer_home_priority_alerts_full_counts_and_owner_acknowledgement(self):
        from hq_timer_alerts import scan, inbox
        c = app.test_client()
        self.assertEqual(c.get("/hq/timer").status_code, 302)
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["google_sub"] = "owner"
            s["csrf"] = "test"
        self.command("start", allowance="10")
        sid = snapshot(self.job)["id"]
        self.now.return_value += 480
        self.assertEqual(scan(automatic=True), 1)
        self.now.return_value += 120
        self.assertEqual(scan(automatic=True), 1)
        data = inbox()
        self.assertEqual((data["pending_count"], data["matching_count"],
                          data["matching_limit_count"], data["historical_count"]), (2,2,1,0))
        self.assertEqual([x["level"] for x in data["pending"]], ["limit","near"])
        limit = data["pending"][0]["id"]
        # Older sessions are still reviewable but cannot conceal an open limit alert.
        with connect() as db:
            for i in range(1, 53):
                db.execute("""INSERT INTO hq_timer_alerts
                    (id,session_id,work_id,allowance_minutes,level,observed_seconds,observed_at)
                    VALUES (?,?,?,?,?,?,?)""",
                    (uuid.uuid4().hex,sid,self.job,100+i,"near",480,self.now.return_value+i))
        data = inbox()
        self.assertEqual((data["pending_count"], data["matching_count"],
                          data["historical_count"]), (54,2,52))
        self.assertEqual(len(data["pending"]),50)
        self.assertTrue(data["has_more"])
        self.assertEqual(data["pending"][0]["id"],limit)
        page = c.get("/hq/timer")
        self.assertEqual(page.status_code,200)
        self.assertEqual(page.headers["Cache-Control"],"no-store")
        self.assertIn(b"54 unacknowledged observations",page.data)
        self.assertIn(b"Background scan recently verified",page.data)
        self.assertIn(b"Acknowledge alert",page.data)
        self.assertIn(limit.encode(),page.data)
        self.assertEqual(c.post("/hq/timer/alerts",data={
            "action":"acknowledge","alert_id":limit}).status_code,403)
        self.assertEqual(c.post("/hq/timer/alerts",data={
            "action":"acknowledge","alert_id":limit,"csrf":"test"}).status_code,303)
        self.assertEqual(inbox()["pending_count"],53)
        self.assertEqual(inbox()["matching_limit_count"],0)
        self.assertEqual(snapshot(self.job)["state"],"running")
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"],0)
        with patch("hq_timer_alerts.inbox",side_effect=RuntimeError("unavailable")):
            page = c.get("/hq/timer")
            self.assertEqual(page.status_code,200)
            self.assertIn(b"Alert summary unavailable",page.data)
            self.assertIn(b"Open running timer",page.data)



    def test_private_push_device_enrollment_revoke_and_no_delivery(self):
        import base64
        import hq_timer_push as push
        from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, SECP256R1
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        def b64(raw):
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")
        def point():
            return b64(generate_private_key(SECP256R1()).public_key().public_bytes(
                Encoding.X962, PublicFormat.UncompressedPoint))
        c = app.test_client()
        url = "/hq/timer/alerts/push/"
        self.assertEqual(c.get(url).status_code, 302)
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["google_sub"] = "owner"
            s["csrf"] = "test"
        application_key, client_key = point(), point()
        endpoint = "https://web.push.apple.com/synthetic-not-real"
        data = {"action":"enroll", "consent":"yes", "operation_id":uuid.uuid4().hex, "application_key":application_key,
                "endpoint":endpoint, "p256dh":client_key, "auth_key":b64(b"x"*16)}
        with patch.dict(os.environ, HQ_TIMER_PUSH_PUBLIC_KEY=""):
            self.assertIn(b"Device registration is not configured", c.get(url).data)
            self.assertEqual(c.post(url, data={**data,"csrf":"test"}).status_code, 400)
        with patch.dict(os.environ, HQ_TIMER_PUSH_PUBLIC_KEY=application_key):
            self.assertEqual(c.get(url).headers["Cache-Control"], "no-store")
            self.assertIn(b"Enable device registration", c.get(url).data)
            self.assertEqual(c.post(url, data=data).status_code, 403)
            data["csrf"] = "test"
            for bad in ({"consent":"no"}, {"application_key":client_key},
                        {"p256dh":"bad"}, {"auth_key":"bad"},
                        {"endpoint":"https://web.push.apple.com.evil.invalid/path"},
                        {"endpoint":"https://web.push.apple.com:443/path"},
                        {"endpoint":"https://web.push.apple.com@evil.invalid/path"},
                        {"endpoint":"http://web.push.apple.com/path"}):
                self.assertEqual(c.post(url, data={**data,**bad}).status_code, 400)
            self.assertEqual(c.post(url, data=data).json, {"enrolled":True})
            self.assertEqual(c.post(url, data=data).json, {"enrolled":True})
            records = push.devices("owner")
            self.assertEqual(len(records), 1)
            self.assertEqual(push.devices("other"), [])
            device = records[0]["id"]
            self.assertNotIn("endpoint", records[0])
            page = c.get(url)
            self.assertIn(b"Revoke HQ registration", page.data)
            self.assertNotIn(endpoint.encode(), page.data)
            self.assertNotIn(client_key.encode(), page.data)
            with self.assertRaises(LookupError):
                push.revoke("other", device, 0)
            revoke = {"action":"revoke","device_id":device,"revision":"0","csrf":"test"}
            self.assertEqual(c.post(url, data=revoke).status_code, 303)
            self.assertEqual(c.post(url, data=revoke).status_code, 303)
            self.assertEqual(push.devices("owner"), [])
            with connect() as db:
                row = db.execute("SELECT * FROM hq_timer_push_devices WHERE id=?", (device,)).fetchone()
                self.assertIsNone(row["endpoint"])
                self.assertIsNone(row["p256dh"])
                self.assertIsNone(row["auth_key"])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_timer_push_events").fetchone()[0], 2)
            self.assertEqual(c.post(url, data=data).status_code, 400)
            self.assertEqual(push.devices("owner"), [])
            data["operation_id"] = uuid.uuid4().hex
            self.assertEqual(c.post(url, data=data).status_code, 200)
            self.assertEqual(c.post(url, data=revoke).status_code, 400)
            self.assertEqual(push.devices("owner")[0]["revision"], 2)
        sw = app.test_client().get(url+"sw.js")
        self.assertEqual(sw.status_code, 200)
        self.assertEqual(sw.headers["Cache-Control"], "no-store")
        self.assertNotIn("Service-Worker-Allowed", sw.headers)
        self.assertIn(b"showNotification", sw.data)
        self.assertNotIn(b"fetch(", sw.data)
        self.assertIsNone(snapshot(self.job))
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"], 0)

    def test_push_preview_explicit_consent_future_alerts_dedupe_and_cancellation(self):
        import base64
        import hq_timer_push as push
        import hq_timer_push_preview as preview
        from hq_timer_alerts import scan, inbox, acknowledge
        from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, SECP256R1
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        def b64(raw):
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")
        key = b64(generate_private_key(SECP256R1()).public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint))
        c = app.test_client()
        url = "/hq/timer/alerts/push/"
        self.assertEqual(c.get(url).status_code, 302)
        with c.session_transaction() as s, auth_db() as db:
            s["hq_sid"] = issue_session(db, "owner")
            s["google_sub"] = "owner"
            s["csrf"] = "test"
        with patch.dict(os.environ, HQ_TIMER_PUSH_PUBLIC_KEY=key):
            device = push.enroll("owner", endpoint="https://web.push.apple.com/synthetic-preview",
                p256dh=key, auth_key=b64(b"x"*16), application_key=key,
                consent="yes", operation_id=uuid.uuid4().hex)
            self.command("start", allowance="10")
            self.now.return_value += 480
            self.assertEqual(scan(),1)
            self.assertEqual(preview.refresh(),0)
            self.assertFalse(preview.summary("owner")[device]["enabled"])
            data = {"action":"preview_consent","choice":"enable","device_id":device,
                    "revision":"0","consent_revision":"0"}
            self.assertEqual(c.post(url,data=data).status_code,403)
            self.assertEqual(c.post(url,data={**data,"csrf":"test"}).status_code,303)
            self.assertEqual(c.post(url,data={**data,"csrf":"test"}).status_code,400)
            self.assertEqual(preview.summary("other"),{})
            with self.assertRaises(ValueError):
                preview.set_consent("other",device,0,1,False)
            self.assertEqual(preview.refresh(),0) # no retroactive near alert
            self.now.return_value += 120
            self.assertEqual(scan(),1)
            self.assertEqual(preview.refresh(),1)
            self.assertEqual(preview.refresh(),0)
            self.assertEqual(preview.summary("owner")[device]["preview_count"],1)
            self.assertIn(b"Disable local alert preview",c.get(url).data)
            limit = next(a for a in inbox()["pending"] if a["level"]=="limit")
            acknowledge(limit["id"],"owner")
            self.assertEqual(preview.refresh(),0)
            self.assertEqual(preview.summary("owner")[device]["preview_count"],0)
            sid = snapshot(self.job)["id"]
            self.command("allowance",0,sid,allowance="12")
            self.assertEqual(scan(),1)
            self.assertEqual(preview.refresh(),1)
            self.assertEqual(c.post(url,data={**data,"choice":"disable","consent_revision":"1","csrf":"test"}).status_code,303)
            self.assertEqual(preview.summary("owner")[device]["preview_count"],0)
            self.assertEqual(preview.refresh(),0)
            self.assertEqual(c.post(url,data={**data,"consent_revision":"2","csrf":"test"}).status_code,303)
            self.assertEqual(preview.refresh(),0)
            self.command("allowance",1,sid,allowance="11")
            self.assertEqual(scan(),1)
            self.assertEqual(preview.refresh(),1)
            push.revoke("owner",device,0)
            self.assertEqual(preview.refresh(),0)
            self.assertEqual(preview.summary("owner"),{})
            push.enroll("owner", endpoint="https://web.push.apple.com/synthetic-preview",
                p256dh=key, auth_key=b64(b"x"*16), application_key=key,
                consent="yes", operation_id=uuid.uuid4().hex)
            self.assertFalse(preview.summary("owner")[device]["enabled"])
            self.assertEqual(preview.refresh(),0)
            with connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_timer_push_preview WHERE cancelled_at IS NULL").fetchone()[0],0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_timer_push_consent_events").fetchone()[0],3)
            self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"],0)
            self.assertEqual(snapshot(self.job)["state"],"running")

    def test_timer_search_combines_customer_vehicle_scope_and_literal_identity(self):
        from hq_core_records import search_work_records
        other = create_test_job()
        with connect() as db:
            db.execute("UPDATE hq_customers SET name='Synthetic Motors' WHERE id=(SELECT customer_id FROM hq_work_records WHERE id=?)", (self.job,))
            db.execute("UPDATE hq_vehicles SET make='Kia', model='Example', registration='AB12 XYZ' WHERE id=(SELECT vehicle_id FROM hq_work_records WHERE id=?)", (self.job,))
            db.execute("UPDATE hq_work_records SET notes='Black front wing repair <example>' WHERE id=?", (self.job,))
            db.execute("UPDATE hq_customers SET name='Synthetic Motors' WHERE id=(SELECT customer_id FROM hq_work_records WHERE id=?)", (other,))
            db.execute("UPDATE hq_vehicles SET make='Kia' WHERE id=(SELECT vehicle_id FROM hq_work_records WHERE id=?)", (other,))
            db.execute("UPDATE hq_work_records SET notes='Bonnet respray' WHERE id=?", (other,))
        def matches(q):
            return {row["id"] for row in search_work_records(query=q, kind="job", match_all_terms=True)["records"]}
        self.assertEqual(matches("motors kia black wing"), {self.job})
        self.assertEqual(matches("synthetic kia bonnet"), {other})
        self.assertEqual(matches("AB12XYZ"), {self.job})
        self.assertEqual(matches("AB12 XYZ"), {self.job})
        self.assertEqual(matches(self.job), {self.job})
        self.assertEqual(matches("black bonnet"), set())
        self.assertEqual(matches("%"), set())
        self.assertEqual(matches("_"), set())
        self.assertEqual(search_work_records(query="motors kia", kind="job")["total"], 0)
        c = app.test_client()
        url = "/hq/timer?q=motors+kia+black+wing"
        self.assertEqual(c.get(url).status_code, 302)
        with c.session_transaction() as session, auth_db() as db:
            session["hq_sid"] = issue_session(db, "owner")
            session["csrf"] = "test"
        page = c.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(self.job.encode(), page.data)
        self.assertNotIn(other.encode(), page.data)
        self.assertIn(b"Black front wing repair &lt;example&gt;", page.data)
        self.assertIsNone(snapshot(self.job))
        self.assertIsNone(snapshot(other))
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"], 0)

    def test_timer_job_search_pages_keep_query_and_do_not_start_sessions(self):
        from hq_core_records import search_work_records
        ids = [self.job] + [create_test_job() for _ in range(50)]
        with connect() as db:
            db.executemany("UPDATE hq_work_records SET notes='Synthetic pagination marker' WHERE id=?",
                           [(job,) for job in ids])
        c = app.test_client()
        with c.session_transaction() as session, auth_db() as db:
            session["hq_sid"] = issue_session(db, "owner")
            session["csrf"] = "test"
        first = c.get("/hq/timer?q=pagination+marker")
        second = c.get("/hq/timer?q=pagination+marker&offset=50")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIn(b"51 matching jobs", first.data)
        self.assertIn(b"Next jobs", first.data)
        self.assertIn(b"Previous jobs", second.data)
        self.assertNotIn(b"Next jobs", second.data)
        tail = search_work_records(query="pagination marker", kind="job", limit=50,
                                   offset=50, match_all_terms=True)["records"]
        self.assertEqual(len(tail), 1)
        self.assertNotIn(tail[0]["id"].encode(), first.data)
        self.assertIn(tail[0]["id"].encode(), second.data)
        for offset in ("-1", "abc", "100001"):
            self.assertEqual(c.get("/hq/timer?offset=" + offset).status_code, 400)
        self.assertIsNone(snapshot(self.job))
        self.assertEqual(time_ledger_snapshot(self.job)["minutes_total"], 0)
