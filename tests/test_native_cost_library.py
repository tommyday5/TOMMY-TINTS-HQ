"""Native HQ cost assumptions: owner CRUD, revision safety and no Sheets writes."""
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_cost_library import list_items, get_item

ROOT = Path(__file__).resolve().parents[1]


class NativeCostLibraryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name,
            "HQ_SESSION_SECRET": "cost-library-synthetic-session-key",
            "GOOGLE_CLIENT_ID": "synthetic",
            "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner = app.test_client()
        self.unauthenticated = app.test_client()
        self.payload = {
            "csrf": "synthetic-cost-csrf",
            "operation_id": uuid.uuid4().hex,
            "action": "create",
            "service_name": "Front bumper repair & paint",
            "default_hours": "2.80",
            "material_cost": "14.00",
            "consumables_cost": "6.00",
            "retail_multiplier": "1.3500",
            "trade_multiplier": "1.0000",
            "maintenance_multiplier": "1.1000",
            "insurance_multiplier": "1.5500",
            "notes": "Labour and materials baseline only",
            "pricing_method": "Owner review",
            "approval_rule": "Approve before work",
            "breakdown_note": "Repair and refinish",
        }

    def login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="synthetic-cost-csrf", google_sub="synthetic-owner")

    def create(self):
        response = self.owner.post("/hq/cost-library/new", data=self.payload)
        self.assertEqual(response.status_code,303,response.data.decode()[:200])
        return response.headers["Location"].split("?")[0]

    def test_owner_only_navigation_and_csrf(self):
        self.assertEqual(self.unauthenticated.get("/hq/cost-library").status_code,302)
        self.assertEqual(self.unauthenticated.get("/hq/cost-library/new").status_code,302)
        self.assertEqual(self.unauthenticated.post("/hq/cost-library/new",
                                                   data=self.payload).status_code,302)
        self.login()
        index = self.owner.get("/hq/cost-library")
        self.assertEqual(index.status_code,200)
        self.assertIn(b"Create service cost assumption",index.data)
        self.assertIn(b"historical Google Sheets Cost Library remains read-only",index.data)
        self.assertIn("no-store",index.headers["Cache-Control"])
        self.assertIn(b"/hq/cost-library",self.owner.get("/").data)
        self.assertEqual(self.owner.get("/hq/cost-library/new").status_code,200)
        self.assertEqual(self.owner.post("/hq/cost-library/new",
                         data={**self.payload,"csrf":"incorrect"}).status_code,403)
        self.assertEqual(self.owner.get("/hq/cost-library/not-an-id").status_code,404)
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hq_cost_library_items").fetchone()[0],0)

    def test_create_save_reload_revisions_and_immutable_events(self):
        self.login()
        path=self.create()
        self.assertEqual(self.owner.post("/hq/cost-library/new",data=self.payload).status_code,303)
        reloaded = self.owner.get(path)
        self.assertEqual(reloaded.status_code,200)
        self.assertIn(b"Front bumper repair",reloaded.data)
        self.assertIn(b"1.3500",reloaded.data)
        item_id=path.rsplit("/",1)[-1]
        original=get_item(item_id)
        self.assertEqual(original["default_hours_hundredths"],280)
        self.assertEqual(original["material_pence"],1400)
        self.assertEqual(original["consumables_pence"],600)
        self.assertEqual(original["revision"],0)
        self.assertEqual(len(list_items()),1)
        next_payload={**self.payload,"action":"save",
                      "operation_id":uuid.uuid4().hex,"revision":"0",
                      "material_cost":"17.25","default_hours":"3.25"}
        self.assertEqual(self.owner.post(path,data=next_payload).status_code,303)
        self.assertEqual(self.owner.post(path,data=next_payload).status_code,303)
        self.assertEqual(self.owner.post(path,data={
            **next_payload,"operation_id":uuid.uuid4().hex}).status_code,409)
        revised=get_item(item_id)
        self.assertEqual(revised["revision"],1)
        self.assertEqual(revised["material_pence"],1725)
        self.assertEqual(revised["default_hours_hundredths"],325)
        self.assertIn(b"17.25",self.owner.get(path).data)
        with connect() as db:
            events=db.execute("""SELECT action,revision,before_state,after_state,
                                 owner_fingerprint FROM hq_cost_library_events ORDER BY revision""").fetchall()
            self.assertEqual([(r["action"],r["revision"]) for r in events],
                             [("create",0),("save",1)])
            self.assertEqual(len(events[1]["owner_fingerprint"]),64)
            self.assertNotIn("synthetic-owner",str(tuple(events[1])))
            self.assertEqual(db.execute("SELECT count(*) FROM hq_cost_library_operations").fetchone()[0],2)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_work_records'").fetchone())

    def test_archive_restore_and_duplicate_service_names(self):
        self.login()
        path=self.create()
        second={**self.payload,"operation_id":uuid.uuid4().hex,
                "service_name":"  FRONT   BUMPER REPAIR & PAINT "}
        self.assertEqual(self.owner.post("/hq/cost-library/new",data=second).status_code,409)
        archived={"csrf":"synthetic-cost-csrf","action":"archive",
                  "operation_id":uuid.uuid4().hex,"revision":"0"}
        self.assertEqual(self.owner.post(path,data=archived).status_code,303)
        self.assertEqual(self.owner.post(path,data=archived).status_code,303)
        self.assertEqual(list_items(),[])
        self.assertEqual(len(list_items(archived=True)),1)
        self.assertIn(b"Restore record",self.owner.get(path).data)
        self.assertEqual(self.owner.post("/hq/cost-library/new",data=second).status_code,409)
        self.assertEqual(self.owner.post(path,data={**self.payload,"action":"save",
                        "revision":"1","operation_id":uuid.uuid4().hex}).status_code,409)
        restored={**archived,"action":"restore","revision":"1",
                  "operation_id":uuid.uuid4().hex}
        self.assertEqual(self.owner.post(path,data=restored).status_code,303)
        self.assertEqual(len(list_items()),1)
        self.assertEqual(get_item(path.rsplit("/",1)[-1])["revision"],2)
        with connect() as db:
            actions=[r[0] for r in db.execute("SELECT action FROM hq_cost_library_events ORDER BY revision")]
            self.assertEqual(actions,["create","archive","restore"])

    def test_validation_duplicate_op_and_search(self):
        self.login()
        for change in (
            {"service_name":""},{"default_hours":"-1"},
            {"default_hours":"2.888"},{"material_cost":"£14.00"},
            {"consumables_cost":"1.999"},{"retail_multiplier":"0"},
            {"trade_multiplier":"1.00001"},{"insurance_multiplier":"50"},
            {"notes":"x"*801},
        ):
            with self.subTest(change=change):
                response=self.owner.post("/hq/cost-library/new",
                    data={**self.payload,**change,"operation_id":uuid.uuid4().hex})
                self.assertEqual(response.status_code,409)
        path=self.create()
        self.assertEqual(self.owner.post("/hq/cost-library/new",data={
            **self.payload,"material_cost":"50"}).status_code,409)
        self.assertEqual(self.owner.get("/hq/cost-library?q=bumper").status_code,200)
        self.assertIn(b"Front bumper repair",self.owner.get("/hq/cost-library?q=bumper").data)
        self.assertNotIn(b"Front bumper repair",self.owner.get("/hq/cost-library?q=wheel").data)
        self.assertEqual(self.owner.get("/hq/cost-library?scope=invalid").status_code,400)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_cost_library_items").fetchone()[0],1)


if __name__=="__main__":
    unittest.main()
