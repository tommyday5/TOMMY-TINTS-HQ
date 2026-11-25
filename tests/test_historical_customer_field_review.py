"""Synthetic, owner-only historical Customers field review integration tests.

No external Sheets reads, canonical customer mutations, imports or live bookings.
"""
import copy
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
from hq_core_records import _schema
from hq_historical_customer_links import (
    source_stamp, stage_customer_candidate, withdraw_customer_candidate,
)
from hq_historical_customer_field_review import _read_snapshot, save_field_review, _tables
from hq_customer_account_types import _tables as tier_tables
from hq_customer_postcodes import save_postcode

ROOT = Path(__file__).resolve().parents[1]


def snapshots():
    jobs=[["Job ID","Date","Customer","Customer Type","Vehicle/Reg","Service 1"]
          +[""]*10+["Quoted Price","Status"],
          ["J0123","","Synthetic Owner Customer","Trader","","Window Tinting"]
          +[""]*10+[125,"Quoted"]]
    customers=[["Customer","Type","Postcode"],
               ["Synthetic Owner Customer","Trader","ZZ0 0AA"]]
    def data(j,p):
        return {"valueRanges":[
            {"range":"Jobs!A4:AZ1000","values":j},
            {"range":"Customers!A4:AF1000","values":p},
        ]}
    return data(jobs,customers), data(copy.deepcopy(jobs),copy.deepcopy(customers))


class CustomerFieldReviewTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ, {
            "HQ_DATA_DIR":tmp.name,
            "HQ_SESSION_SECRET":"synthetic-customer-field-review-secret",
            "GOOGLE_CLIENT_ID":"synthetic",
            "GOOGLE_CLIENT_SECRET":"synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL":"owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        ctx=app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.customer=uuid.uuid4().hex
        self.formula,self.computed=snapshots()
        self.original=copy.deepcopy((self.formula,self.computed))
        with connect() as db:
            _schema(db)
            tier_tables(db)
            db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                       (self.customer,None,"Synthetic Owner Customer",
                        "owner@example.invalid","", "2026-01-01"))
            db.execute("""INSERT INTO hq_customer_account_types
                VALUES(?,?,?,?,?)""",(self.customer,"trade",
                "trade_evidence_reviewed",1,"2026-01-01"))
        self.candidate=stage_customer_candidate(
            formula=self.formula,computed=self.computed,sheet_row=5,
            expected_source_stamp=source_stamp(self.formula,self.computed,5),
            customer_id=self.customer,basis="name_crosscheck",
            operation_id=uuid.uuid4().hex,owner_identity="synthetic-owner")
        self.client=app.test_client()
        self.url="/hq/migration/customers/"+self.candidate+"/fields"

    def login(self):
        with self.client.session_transaction() as sess,auth_db() as db:
            sess.update(hq_sid=issue_session(db,"synthetic-owner"),
                        google_sub="synthetic-owner",csrf="test-csrf")

    def data(self,**overrides):
        data={"csrf":"test-csrf","revision":"0","operation_id":"a"*32,
              "name_decision":"candidate_agreement",
              "account_type_decision":"candidate_agreement",
              "postcode_decision":"not_mapped"}
        data.update(overrides)
        return data

    def live_source(self):
        return (patch.dict(app.extensions,
                {"hq_sheets_connection":lambda:("fixture",9999999999)}),
                patch("hq_historical_customer_links._read_live",
                      return_value=(self.formula,self.computed)))

    def test_owner_ui_save_reload_idempotency_and_unmodified_source(self):
        self.assertEqual(self.client.get(self.url).status_code,302)
        self.login()
        a,b=self.live_source()
        with a,b as read:
            page=self.client.get(self.url)
            self.assertEqual(page.status_code,200)
            self.assertIn(b"Synthetic Owner Customer",page.data)
            self.assertIn(b"Not mapped in core HQ customer",page.data)
            self.assertIn("no-store",page.headers["Cache-Control"])
            self.assertEqual(self.client.post(self.url,data=self.data(csrf="bad")).status_code,403)
            self.assertEqual(self.client.post(self.url,data=self.data(revision="x")).status_code,400)
            saved=self.client.post(self.url,data=self.data())
            self.assertEqual(saved.status_code,303)
            self.assertEqual(self.client.post(self.url,data=self.data()).status_code,303)
            self.assertIn(b"field review was saved",
                          self.client.get(self.url+"?saved=1").data)
            self.assertEqual(read.call_count,4)
        with connect() as db:
            result=db.execute("""SELECT * FROM hq_historical_customer_field_reviews
                WHERE candidate_id=?""",(self.candidate,)).fetchone()
            self.assertEqual((result["revision"],result["name_decision"],
                result["account_type_decision"],result["postcode_decision"]),
                (1,"candidate_agreement","candidate_agreement","not_mapped"))
            self.assertEqual(db.execute("""SELECT count(*) FROM
                hq_historical_customer_field_events""").fetchone()[0],1)
            self.assertEqual(db.execute("""SELECT count(*) FROM
                hq_historical_customer_field_operations""").fetchone()[0],1)
            values=db.execute("SELECT name,email FROM hq_customers WHERE id=?",
                              (self.customer,)).fetchone()
            self.assertEqual(tuple(values),("Synthetic Owner Customer","owner@example.invalid"))
            self.assertNotIn("Synthetic Owner Customer",str(dict(result)))
            self.assertNotIn("ZZ0 0AA",str(dict(result)))
            self.assertIsNone(db.execute("""SELECT 1 FROM sqlite_master
                WHERE name='hq_historical_customer_imports'""").fetchone())
        self.assertEqual((self.formula,self.computed),self.original)

    def test_conflict_stale_revision_and_valid_second_version(self):
        self.login()
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.post(self.url,data=self.data()).status_code,303)
            self.assertEqual(self.client.post(self.url,data=self.data(
                postcode_decision="requires_followup")).status_code,409)
            self.assertEqual(self.client.post(self.url,data=self.data(
                operation_id="b"*32,postcode_decision="requires_followup")).status_code,409)
            self.assertEqual(self.client.post(self.url,data=self.data(
                operation_id="c"*32,revision="1",
                postcode_decision="requires_followup")).status_code,303)
            self.assertEqual(self.client.post(self.url,data=self.data(
                operation_id="d"*32,revision="2",
                postcode_decision="candidate_agreement")).status_code,409)
        with connect() as db:
            events=list(db.execute("""SELECT revision FROM hq_historical_customer_field_events
                ORDER BY revision"""))
            self.assertEqual([row[0] for row in events],[1,2])

    def test_source_native_and_tier_drift_are_fail_closed(self):
        self.login()
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.post(self.url,data=self.data()).status_code,303)
            with connect() as db:
                db.execute("UPDATE hq_customer_account_types SET tier='retail',revision=2 WHERE customer_id=?",
                           (self.customer,))
            page=self.client.get(self.url)
            self.assertEqual(page.status_code,200)
            self.assertIn(b"account classification changed",page.data)
            self.assertEqual(self.client.post(self.url,data=self.data(
                revision="1",operation_id="e"*32)).status_code,409)
        with connect() as db:
            db.execute("UPDATE hq_customers SET phone='07700 900000' WHERE id=?",
                       (self.customer,))
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.get(self.url).status_code,409)
        with connect() as db:
            db.execute("UPDATE hq_customers SET phone='' WHERE id=?",
                       (self.customer,))
        formula,computed=snapshots()
        formula["valueRanges"][1]["values"][1][2]="ZZ1 1BB"
        computed["valueRanges"][1]["values"][1][2]="ZZ1 1BB"
        self.formula,self.computed=formula,computed
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.get(self.url).status_code,409)
        with connect() as db:
            self.assertEqual(db.execute("""SELECT count(*) FROM
                hq_historical_customer_field_events""").fetchone()[0],1)

    def test_native_postcode_visible_to_owner_but_not_verified_match(self):
        save_postcode(self.customer,"SW1A 1AA",0,"f"*32,"synthetic-owner")
        self.login()
        a,b=self.live_source()
        with a,b:
            page=self.client.get(self.url)
            self.assertEqual(page.status_code,200)
            self.assertIn(b"SW1A 1AA",page.data)
            self.assertIn(b"owner-recorded, not independently verified",page.data)
            self.assertEqual(self.client.post(self.url,data=self.data(
                postcode_decision="candidate_agreement")).status_code,409)
        self.assertEqual((self.formula,self.computed),self.original)

    def test_approved_historical_link_requires_snapshot_and_exact_approval(self):
        from hq_private_recovery import create_snapshot
        from hq_approved_customer_links import history
        self.login()
        withdraw_customer_candidate(self.candidate,0,"synthetic-owner")
        candidate=stage_customer_candidate(
            formula=self.formula,computed=self.computed,sheet_row=5,
            expected_source_stamp=source_stamp(self.formula,self.computed,5),
            customer_id=self.customer,basis="owner_document_review",
            operation_id=uuid.uuid4().hex,owner_identity="synthetic-owner")
        field_url="/hq/migration/customers/"+candidate+"/fields"
        url="/hq/migration/customers/"+candidate+"/approved-link"
        self.assertEqual(app.test_client().get(url).status_code,302)
        old_snapshot=create_snapshot(uuid.uuid4().hex)
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.post(field_url,data=self.data()).status_code,303)
            snapshot=create_snapshot(uuid.uuid4().hex)
            prepare=dict(csrf="test-csrf",action="prepare",snapshot_id=snapshot)
            self.assertEqual(self.client.post(url,data=dict(prepare,csrf="bad")).status_code,403)
            self.assertEqual(self.client.post(url,data=dict(prepare,snapshot_id=old_snapshot)).status_code,409)
            page=self.client.post(url,data=prepare)
            self.assertEqual(page.status_code,200)
            self.assertIn(b"Exact approval manifest",page.data)
            import re
            stamp=re.search(rb'name="stamp" value="([0-9a-f]{64})"',page.data).group(1).decode()
            self.assertEqual(history(),[])
            apply=dict(prepare,action="apply",stamp=stamp,confirm="yes")
            self.assertEqual(self.client.post(url,data=dict(apply,confirm="")).status_code,409)
            self.assertEqual(self.client.post(url,data=dict(apply,stamp="0"*64)).status_code,409)
            self.computed["valueRanges"][1]["values"][1][2]="ZZ1 1BB"
            self.assertEqual(self.client.post(url,data=apply).status_code,409)
            self.computed["valueRanges"][1]["values"][1][2]="ZZ0 0AA"
            self.assertEqual(self.client.post(url,data=apply).status_code,200)
            self.assertEqual(self.client.post(url,data=apply).status_code,200)
            self.assertEqual(len(history()),1)
            self.assertIsNone(history()[0]["reversed_at"])
            with self.assertRaises(ValueError):
                withdraw_customer_candidate(candidate,0,"synthetic-owner")
            self.assertIn(b"Approved historical link",self.client.get(
                "/hq/migration/customers/approved-links").data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hq_customers").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT name FROM hq_customers").fetchone()[0],
                             "Synthetic Owner Customer")
        # Reversal works without a Sheets connection and survives another request.
        reversal=dict(csrf="test-csrf",action="reverse",stamp=stamp,confirm="yes")
        self.assertEqual(self.client.post(url,data=reversal).status_code,200)
        self.assertEqual(self.client.post(url,data=reversal).status_code,200)
        self.assertIsNotNone(history()[0]["reversed_at"])
        self.assertEqual((self.formula,self.computed),self.original)

    def test_withdrawal_and_security_gates(self):
        self.login()
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.get(self.url+"nonsense").status_code,404)
            self.assertEqual(self.client.post(self.url,data=self.data(
                name_decision="invalid")).status_code,409)
            self.assertEqual(self.client.post(self.url,data=self.data(
                account_type_decision="not_mapped")).status_code,303)
            withdraw_customer_candidate(self.candidate,0,"synthetic-owner")
            self.assertEqual(self.client.get(self.url).status_code,409)
            self.assertEqual(self.client.post(self.url,data=self.data(
                operation_id="f"*32,revision="1")).status_code,409)


    def test_bounded_postcode_import_and_guarded_reversal(self):
        import re
        from hq_private_recovery import create_snapshot
        from hq_customer_postcodes import postcode_status
        self.login()
        withdraw_customer_candidate(self.candidate,0,"synthetic-owner")
        candidate=stage_customer_candidate(
            formula=self.formula,computed=self.computed,sheet_row=5,
            expected_source_stamp=source_stamp(self.formula,self.computed,5),
            customer_id=self.customer,basis="owner_document_review",
            operation_id=uuid.uuid4().hex,owner_identity="synthetic-owner")
        base="/hq/migration/customers/"+candidate
        url=base+"/postcode-import"
        self.assertEqual(app.test_client().get(url).status_code,302)
        a,b=self.live_source()
        def stamp(page):
            self.assertEqual(page.status_code,200)
            return re.search(rb'name="stamp" value="([0-9a-f]{64})"',page.data).group(1).decode()
        with a,b:
            self.assertEqual(self.client.post(base+"/fields",data=self.data()).status_code,303)
            before=create_snapshot(uuid.uuid4().hex)
            prepare=dict(csrf="test-csrf",action="prepare",snapshot_id=before)
            identity_stamp=stamp(self.client.post(base+"/approved-link",data=prepare))
            self.assertEqual(self.client.post(base+"/approved-link",data=dict(
                prepare,action="apply",stamp=identity_stamp,confirm="yes")).status_code,200)
            self.assertIn(self.client.post(url,data=prepare).status_code,(409,503))
            snapshot=create_snapshot(uuid.uuid4().hex)
            prepare["snapshot_id"]=snapshot
            self.assertEqual(self.client.post(url,data=dict(prepare,csrf="bad")).status_code,403)
            page=self.client.post(url,data=prepare)
            self.assertIn(b"Never edited",page.data)
            exact=stamp(page)
            apply=dict(prepare,action="apply",stamp=exact,confirm="yes")
            with connect() as db:
                self.assertEqual(postcode_status(db,self.customer)["revision"],0)
            self.assertEqual(self.client.post(url,data=dict(apply,confirm="")).status_code,409)
            self.computed["valueRanges"][1]["values"][1][2]="ZZ1 1BB"
            self.assertEqual(self.client.post(url,data=apply).status_code,409)
            self.computed["valueRanges"][1]["values"][1][2]="ZZ0 0AA"
            self.assertEqual(self.client.post(url,data=apply).status_code,200)
            self.assertEqual(self.client.post(url,data=apply).status_code,200)
            with connect() as db:
                self.assertEqual(postcode_status(db,self.customer)["postcode"],"ZZ0 0AA")
                self.assertEqual(postcode_status(db,self.customer)["revision"],1)
                self.assertEqual(db.execute("SELECT count(*) FROM hq_customer_postcode_events").fetchone()[0],1)
            self.assertIn(b"ZZ0 0AA",self.client.get("/hq/customers/"+self.customer+"/postcode").data)
        reversal=dict(csrf="test-csrf",action="reverse",stamp=exact,confirm="yes")
        identity_reversal=dict(reversal,stamp=identity_stamp)
        self.assertEqual(self.client.post(base+"/approved-link",data=identity_reversal).status_code,409)
        self.assertEqual(self.client.post(url,data=reversal).status_code,200)
        self.assertEqual(self.client.post(url,data=reversal).status_code,200)
        with connect() as db:
            self.assertEqual(postcode_status(db,self.customer)["postcode"],"")
            self.assertEqual(postcode_status(db,self.customer)["revision"],2)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_customer_postcode_events").fetchone()[0],2)
        self.assertEqual(self.client.post(base+"/approved-link",data=identity_reversal).status_code,200)
        self.assertEqual((self.formula,self.computed),self.original)


    def test_missing_customer_batch_exact_approval_and_archive(self):
        import re
        from hq_private_recovery import create_snapshot
        from hq_historical_customer_creation import _rows
        from hq_customer_archive import archive_status
        url="/hq/migration/customers/create-batch"
        self.assertEqual(self.client.get(url).status_code,302)
        self.login()
        for payload in (self.formula,self.computed):
            payload["valueRanges"][1]["values"].extend([
                ["Synthetic Missing Alpha","Retail","ZZ1 1AA"],
                ["Synthetic Missing Beta","Trader","ZZ2 2BB"]])
        original=copy.deepcopy((self.formula,self.computed))
        snapshot=create_snapshot(uuid.uuid4().hex)
        batch=uuid.uuid4().hex
        data=dict(csrf="test-csrf",action="prepare",batch_id=batch,
                  rows="6,7",snapshot_id=snapshot)
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.post(url,data=dict(data,csrf="bad")).status_code,403)
            self.assertEqual(self.client.post(url,data=dict(data,rows="5,6")).status_code,409)
            self.assertEqual(self.client.post(url,data=dict(data,rows="6,6")).status_code,409)
            page=self.client.post(url,data=data)
            self.assertEqual(page.status_code,200)
            self.assertIn(b"Synthetic Missing Alpha",page.data)
            self.assertIn(b"Synthetic Missing Beta",page.data)
            stamp=re.search(rb'name="stamp" value="([0-9a-f]{64})"',page.data).group(1).decode()
            with connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM hq_customers").fetchone()[0],1)
            apply=dict(data,action="apply",stamp=stamp,confirm="yes")
            self.assertEqual(self.client.post(url,data=dict(apply,confirm="")).status_code,409)
            self.computed["valueRanges"][1]["values"][2][0]="Changed"
            self.assertEqual(self.client.post(url,data=apply).status_code,409)
            self.computed["valueRanges"][1]["values"][2][0]="Synthetic Missing Alpha"
            self.assertEqual(self.client.post(url,data=apply).status_code,200)
            self.assertEqual(self.client.post(url,data=apply).status_code,200)
            self.assertEqual(self.client.post(url,data=dict(apply,rows="6")).status_code,409)
            self.assertEqual(self.client.post(url,data=dict(data,batch_id=uuid.uuid4().hex)).status_code,409)
        with connect() as db:
            rows=db.execute("""SELECT c.id,c.name,c.email,c.phone,c.source_request_id
                FROM hq_customers c JOIN hq_historical_created_customers h
                ON h.customer_id=c.id ORDER BY h.sheet_row""").fetchall()
            self.assertEqual([r["name"] for r in rows],["Synthetic Missing Alpha","Synthetic Missing Beta"])
            self.assertTrue(all(not r["email"] and not r["phone"] and r["source_request_id"] is None for r in rows))
            self.assertEqual(db.execute("SELECT count(*) FROM hq_historical_customer_creation_batches").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_vehicles").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_work_records").fetchone()[0],0)
        self.assertIn(b"2 customers",self.client.get(url).data)
        reversal=dict(csrf="test-csrf",action="reverse",batch_id=batch,stamp=stamp,confirm="yes")
        # A dependent vehicle blocks the entire reversal, including the untouched customer.
        vid=uuid.uuid4().hex
        with connect() as db:
            db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                       (vid,rows[1]["id"],"Synthetic","Test","","","2026-01-01"))
        self.assertEqual(self.client.post(url,data=reversal).status_code,409)
        self.assertEqual(archive_status(rows[0]["id"]),(False,0))
        with connect() as db:
            db.execute("DELETE FROM hq_vehicles WHERE id=?",(vid,))
        self.assertEqual(self.client.post(url,data=reversal).status_code,200)
        self.assertEqual(self.client.post(url,data=reversal).status_code,200)
        for row in rows:
            self.assertEqual(archive_status(row["id"]),(True,1))
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hq_customers").fetchone()[0],3)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_historical_created_customers").fetchone()[0],2)
        self.assertEqual((self.formula,self.computed),original)
        self.assertRaises(ValueError,_rows,"5,6,7,8,9,10")


    def test_missing_customer_batch_optional_postcodes_exact_and_reversible(self):
        import re
        from hq_private_recovery import create_snapshot
        from hq_customer_postcodes import postcode_status
        from hq_customer_archive import archive_status
        url="/hq/migration/customers/create-batch"
        self.login()
        for payload in (self.formula,self.computed):
            payload["valueRanges"][1]["values"].extend([
                ["Synthetic Missing Alpha","Retail","zz1 1aa"],
                ["Synthetic Missing Beta","Trader",""]])
        snapshot=create_snapshot(uuid.uuid4().hex)
        batch=uuid.uuid4().hex
        data=dict(csrf="test-csrf",action="prepare",batch_id=batch,
                  rows="6,7",snapshot_id=snapshot,include_postcodes="yes")
        a,b=self.live_source()
        with a,b:
            for payload in (self.formula,self.computed):
                payload["valueRanges"][1]["values"][2][2]="bad postcode"
            self.assertEqual(self.client.post(url,data=data).status_code,409)
            for payload in (self.formula,self.computed):
                payload["valueRanges"][1]["values"][2][2]="zz1 1aa"
            page=self.client.post(url,data=data)
            self.assertEqual(page.status_code,200)
            self.assertIn(b"ZZ1 1AA",page.data)
            self.assertIn(b"1 postcodes populated",page.data)
            stamp=re.search(rb'name="stamp" value="([0-9a-f]{64})"',page.data).group(1).decode()
            apply=dict(data,action="apply",stamp=stamp,confirm="yes")
            self.assertEqual(self.client.post(url,data=dict(apply,include_postcodes="")).status_code,409)
            self.computed["valueRanges"][1]["values"][2][2]="ZZ2 2BB"
            self.assertEqual(self.client.post(url,data=apply).status_code,409)
            self.computed["valueRanges"][1]["values"][2][2]="zz1 1aa"
            self.assertEqual(self.client.post(url,data=apply).status_code,200)
            self.assertEqual(self.client.post(url,data=apply).status_code,200)
            self.assertEqual(self.client.post(url,data=dict(apply,include_postcodes="")).status_code,409)
        with connect() as db:
            ids=[r[0] for r in db.execute("SELECT customer_id FROM hq_historical_created_customers ORDER BY sheet_row")]
            self.assertEqual(len(ids),2)
            self.assertEqual(postcode_status(db,ids[0])["postcode"],"ZZ1 1AA")
            self.assertEqual(postcode_status(db,ids[0])["revision"],1)
            self.assertEqual(postcode_status(db,ids[1])["revision"],0)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_customer_postcode_events").fetchone()[0],1)
            audit=db.execute("SELECT manifest FROM hq_historical_customer_creation_batches WHERE id=?",(batch,)).fetchone()[0]
            self.assertNotIn("ZZ1 1AA",audit)
        self.assertIn(b"ZZ1 1AA",self.client.get("/hq/customers/"+ids[0]+"/postcode").data)
        reverse=dict(csrf="test-csrf",action="reverse",batch_id=batch,stamp=stamp,confirm="yes")
        self.assertEqual(self.client.post(url,data=reverse).status_code,200)
        self.assertEqual(self.client.post(url,data=reverse).status_code,200)
        self.assertEqual(archive_status(ids[0]),(True,1))
        self.assertEqual(archive_status(ids[1]),(True,1))
        with connect() as db:
            self.assertEqual(postcode_status(db,ids[0])["postcode"],"ZZ1 1AA")

    def test_imported_customer_postcode_edit_blocks_batch_reversal(self):
        import re
        from hq_private_recovery import create_snapshot
        from hq_customer_archive import archive_status
        self.login()
        url="/hq/migration/customers/create-batch"
        for payload in (self.formula,self.computed):
            payload["valueRanges"][1]["values"].append(["Synthetic Missing","Retail","ZZ1 1AA"])
        data=dict(csrf="test-csrf",action="prepare",batch_id=uuid.uuid4().hex,
                  rows="6",snapshot_id=create_snapshot(uuid.uuid4().hex),include_postcodes="yes")
        a,b=self.live_source()
        with a,b:
            page=self.client.post(url,data=data)
            self.assertEqual(page.status_code,200)
            stamp=re.search(rb'name="stamp" value="([0-9a-f]{64})"',page.data).group(1).decode()
            self.assertEqual(self.client.post(url,data=dict(data,action="apply",stamp=stamp,confirm="yes")).status_code,200)
        with connect() as db:
            cid=db.execute("SELECT customer_id FROM hq_historical_created_customers").fetchone()[0]
        save_postcode(cid,"ZZ2 2BB",1,uuid.uuid4().hex,"synthetic-owner")
        self.assertEqual(self.client.post(url,data=dict(data,action="reverse",stamp=stamp,confirm="yes")).status_code,409)
        self.assertEqual(archive_status(cid),(False,0))


    def test_historical_vehicle_link_owner_approval_stale_source_and_reversal(self):
        import re
        from hq_private_recovery import create_snapshot
        from hq_historical_customer_creation import review as create_review
        self.login()
        for payload in (self.formula,self.computed):
            payload["valueRanges"][1]["values"].append(["Synthetic Imported Driver","Retail",""])
            payload["valueRanges"][0]["values"].append(
                ["J0456","","Synthetic Imported Driver","Retail","AB22 CDE","Window Tinting"]
                +[""]*10+[125,"Quoted"])
        source=(self.formula,self.computed)
        batch=uuid.uuid4().hex
        snap=create_snapshot(uuid.uuid4().hex)
        manifest,stamp=create_review([6],snap,batch,source,"synthetic-owner")
        create_review([6],snap,batch,source,"synthetic-owner",stamp)
        cid=manifest["entries"][0]["customer_id"]
        vehicle=uuid.uuid4().hex
        other=uuid.uuid4().hex
        with connect() as db:
            db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                       (vehicle,cid,"Hyundai","i30","2022","AB22 CDE","2026-01-01"))
            db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                       (other,self.customer,"Ford","Focus","2022","ZZ22 XYZ","2026-01-01"))
        recovery=create_snapshot(uuid.uuid4().hex)
        url="/hq/migration/customers/vehicle-links"
        self.assertEqual(app.test_client().get(url).status_code,302)
        data=dict(csrf="test-csrf",action="prepare",operation=uuid.uuid4().hex,
                  row="6",ref="J0456",customer_row="6",customer_id=cid,
                  vehicle_id=vehicle,snapshot_id=recovery)
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.post(url,data=dict(data,csrf="bad")).status_code,403)
            self.assertEqual(self.client.post(url,data=dict(data,vehicle_id=other)).status_code,409)
            page=self.client.post(url,data=data)
            self.assertEqual(page.status_code,200)
            self.assertIn(b"Literal registration matches",page.data)
            stamp=re.search(rb'name="stamp" value="([0-9a-f]{64})"',page.data).group(1).decode()
            apply=dict(data,action="apply",stamp=stamp,confirm="yes")
            self.assertEqual(self.client.post(url,data=dict(apply,confirm="")).status_code,409)
            self.computed["valueRanges"][0]["values"][2][4]="AB22 XYZ"
            self.assertEqual(self.client.post(url,data=apply).status_code,409)
            self.computed["valueRanges"][0]["values"][2][4]="AB22 CDE"
            self.assertEqual(self.client.post(url,data=apply).status_code,200)
            self.assertEqual(self.client.post(url,data=apply).status_code,200)
            self.assertEqual(self.client.post(url,data=dict(data,operation=uuid.uuid4().hex)).status_code,409)
        with connect() as db:
            links=db.execute("SELECT * FROM hq_historical_vehicle_links").fetchall()
            self.assertEqual(len(links),1)
            self.assertEqual(links[0]["vehicle_id"],vehicle)
            self.assertNotIn("AB22 CDE",links[0]["manifest"])
            self.assertEqual(db.execute("SELECT count(*) FROM hq_work_records").fetchone()[0],0)
        self.assertIn(b"J0456",self.client.get(url).data)
        reversal=dict(csrf="test-csrf",action="reverse",operation=data["operation"],stamp=stamp,confirm="yes")
        self.assertEqual(self.client.post(url,data=reversal).status_code,200)
        self.assertEqual(self.client.post(url,data=reversal).status_code,200)
        with connect() as db:
            self.assertIsNotNone(db.execute("SELECT reversed_at FROM hq_historical_vehicle_links").fetchone()[0])
            self.assertEqual(db.execute("SELECT count(*) FROM hq_vehicles").fetchone()[0],2)



    def test_reviewed_historical_vehicle_creation_and_guarded_rollback(self):
        import re
        from hq_private_recovery import create_snapshot
        from hq_historical_customer_creation import review as create_customer
        self.login()
        for payload in (self.formula,self.computed):
            payload["valueRanges"][1]["values"].append(["Synthetic Imported Driver","Retail",""])
            payload["valueRanges"][0]["values"].append(
                ["J0456","","Synthetic Imported Driver","Retail","AB22 CDE","Window Tinting"]
                +[""]*10+[125,"Quoted"])
        source=(self.formula,self.computed)
        batch=uuid.uuid4().hex
        snap=create_snapshot(uuid.uuid4().hex)
        manifest,approval=create_customer([6],snap,batch,source,"synthetic-owner")
        create_customer([6],snap,batch,source,"synthetic-owner",approval)
        cid=manifest["entries"][0]["customer_id"]
        recovery=create_snapshot(uuid.uuid4().hex)
        url="/hq/migration/customers/create-vehicle"
        self.assertEqual(app.test_client().get(url).status_code,302)
        data=dict(csrf="test-csrf",action="prepare",operation=uuid.uuid4().hex,
                  row="6",ref="J0456",customer_row="6",customer_id=cid,
                  make="Hyundai",model="i30",model_year="2022",registration="AB22 CDE",
                  snapshot_id=recovery)
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.post(url,data=dict(data,csrf="bad")).status_code,403)
            self.assertEqual(self.client.post(url,data=dict(data,registration="AB22 XYZ")).status_code,409)
            page=self.client.post(url,data=data)
            self.assertEqual(page.status_code,200)
            self.assertIn(b"Literal registration matches",page.data)
            stamp=re.search(rb'name="stamp" value="([0-9a-f]{64})"',page.data).group(1).decode()
            apply=dict(data,action="apply",stamp=stamp,confirm="yes")
            self.assertEqual(self.client.post(url,data=dict(apply,confirm="")).status_code,409)
            self.assertEqual(self.client.post(url,data=dict(apply,model="i20")).status_code,409)
            self.computed["valueRanges"][0]["values"][2][4]="AB22 XYZ"
            self.assertEqual(self.client.post(url,data=apply).status_code,409)
            self.computed["valueRanges"][0]["values"][2][4]="AB22 CDE"
            self.assertEqual(self.client.post(url,data=apply).status_code,200)
            self.assertEqual(self.client.post(url,data=apply).status_code,200)
            self.assertEqual(self.client.post(url,data=dict(data,operation=uuid.uuid4().hex)).status_code,409)
        with connect() as db:
            imported=db.execute("SELECT * FROM hq_historical_created_vehicles").fetchone()
            vid=imported["vehicle_id"]
            vehicle=db.execute("SELECT * FROM hq_vehicles WHERE id=?",(vid,)).fetchone()
            self.assertEqual((vehicle["customer_id"],vehicle["registration"]),(cid,"AB22 CDE"))
            self.assertNotIn("AB22 CDE",imported["manifest"])
            self.assertEqual(db.execute("SELECT count(*) FROM hq_work_records").fetchone()[0],0)
        self.assertIn(b"AB22 CDE",self.client.get("/hq/vehicles/"+vid+"/edit").data)
        reversal=dict(csrf="test-csrf",action="reverse",operation=data["operation"],stamp=stamp,confirm="yes")
        with connect() as db:
            db.execute("CREATE TABLE synthetic_vehicle_dependency(vehicle_id TEXT)")
            db.execute("INSERT INTO synthetic_vehicle_dependency VALUES(?)",(vid,))
        self.assertEqual(self.client.post(url,data=reversal).status_code,409)
        with connect() as db:
            db.execute("DELETE FROM synthetic_vehicle_dependency")
        self.assertEqual(self.client.post(url,data=reversal).status_code,200)
        self.assertEqual(self.client.post(url,data=reversal).status_code,200)
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hq_vehicles WHERE id=?",(vid,)).fetchone()[0],0)
            self.assertIsNotNone(db.execute("SELECT reversed_at FROM hq_historical_created_vehicles").fetchone()[0])

    def test_missing_historical_quote_create_and_reverse(self):
        import re
        from hq_private_recovery import create_snapshot
        from hq_historical_customer_creation import review as create_customer
        from hq_historical_vehicle_links import review as link_vehicle, reverse as reverse_vehicle
        from hq_work_archive import set_work_archived
        self.login()
        for payload in (self.formula,self.computed):
            payload["valueRanges"][1]["values"].append(["Synthetic Imported Driver","Retail",""])
            payload["valueRanges"][0]["values"].append(
                ["J0456","","Synthetic Imported Driver","Retail","AB22 CDE","Window Tinting"]
                +[""]*10+[125,"Quoted"])
        source=(self.formula,self.computed)
        batch=uuid.uuid4().hex
        snap=create_snapshot(uuid.uuid4().hex)
        manifest,stamp=create_customer([6],snap,batch,source,"synthetic-owner")
        create_customer([6],snap,batch,source,"synthetic-owner",stamp)
        cid=manifest["entries"][0]["customer_id"]
        vid=uuid.uuid4().hex
        with connect() as db:
            db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                       (vid,cid,"Hyundai","i30","2022","AB22 CDE","2026-01-01"))
        snap=create_snapshot(uuid.uuid4().hex)
        link=uuid.uuid4().hex
        _,link_stamp=link_vehicle(source,6,"J0456",6,cid,vid,snap,link,"synthetic-owner")
        link_vehicle(source,6,"J0456",6,cid,vid,snap,link,"synthetic-owner",link_stamp)
        url="/hq/migration/customers/create-work"
        self.assertEqual(app.test_client().get(url).status_code,302)
        self.assertEqual(self.client.get(url).status_code,200)
        snap=create_snapshot(uuid.uuid4().hex)
        data=dict(csrf="test-csrf",action="prepare",operation=uuid.uuid4().hex,
                  link_id=link,service="Window Tinting",snapshot_id=snap)
        a,b=self.live_source()
        with a,b:
            self.assertEqual(self.client.post(url,data=dict(data,csrf="bad")).status_code,403)
            page=self.client.post(url,data=data)
            self.assertEqual(page.status_code,200,page.data)
            self.assertIn(b"125.00",page.data)
            approval=re.search(rb'name="stamp" value="([0-9a-f]{64})"',page.data).group(1).decode()
            apply=dict(data,action="apply",stamp=approval,confirm="yes")
            self.assertEqual(self.client.post(url,data=dict(apply,confirm="")).status_code,409)
            self.assertEqual(self.client.post(url,data=dict(apply,service="Vehicle Wrapping")).status_code,409)
            self.computed["valueRanges"][0]["values"][2][16]=126
            self.assertEqual(self.client.post(url,data=apply).status_code,409)
            self.computed["valueRanges"][0]["values"][2][16]=125
            self.assertEqual(self.client.post(url,data=apply).status_code,200)
            self.assertEqual(self.client.post(url,data=apply).status_code,200)
            self.assertEqual(self.client.post(url,data=dict(data,operation=uuid.uuid4().hex)).status_code,409)
        with connect() as db:
            r=db.execute("SELECT * FROM hq_historical_created_work").fetchone()
            wid=r["work_id"]
            work=db.execute("SELECT * FROM hq_work_records").fetchone()
            self.assertEqual((work["customer_id"],work["vehicle_id"],work["kind"],work["status"],work["price_pence"]),(cid,vid,"quote","draft",12500))
            self.assertEqual(work["deposit_pence"],0)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_job_sheet_links").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_job_sync_outbox").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT job_ref FROM hq_historical_j_candidates").fetchone()[0],"J0456")
        self.assertEqual(self.client.get("/hq/work/"+wid+"/edit").status_code,200)
        with self.assertRaises(ValueError):
            reverse_vehicle(link,link_stamp,"synthetic-owner")
        reversal=dict(csrf="test-csrf",action="reverse",operation=data["operation"],stamp=approval,confirm="yes")
        with connect() as db:
            db.execute("CREATE TABLE synthetic_work_dependency(work_id TEXT)")
            db.execute("INSERT INTO synthetic_work_dependency VALUES(?)",(wid,))
        self.assertEqual(self.client.post(url,data=reversal).status_code,409)
        with connect() as db:
            db.execute("DELETE FROM synthetic_work_dependency")
        self.assertEqual(self.client.post(url,data=reversal).status_code,200)
        self.assertEqual(self.client.post(url,data=reversal).status_code,200)
        with connect() as db:
            self.assertEqual(db.execute("SELECT is_archived FROM hq_work_archive_state WHERE work_id=?",(wid,)).fetchone()[0],1)
            self.assertIsNotNone(db.execute("SELECT withdrawn_at FROM hq_historical_j_candidates").fetchone()[0])
        with self.assertRaises(ValueError):
            set_work_archived(wid,"restore",1,uuid.uuid4().hex)
        reverse_vehicle(link,link_stamp,"synthetic-owner")

    def test_repeat_vehicle_historical_drafts_and_original_reference_lookup(self):
        import re
        from hq_private_recovery import create_snapshot
        from hq_historical_customer_creation import review as create_customer
        from hq_historical_vehicle_links import review as link_vehicle
        from hq_core_records import search_work_records
        self.login()
        for payload in (self.formula,self.computed):
            payload["valueRanges"][1]["values"].append(["Synthetic Repeat Driver","Retail",""])
            for ref,price in (("J0456",125),("J0457",175)):
                payload["valueRanges"][0]["values"].append(
                    [ref,"","Synthetic Repeat Driver","Retail","AB22 CDE","Window Tinting"]
                    +[""]*10+[price,"Quoted"])
        source=(self.formula,self.computed)
        snap=create_snapshot(uuid.uuid4().hex)
        batch=uuid.uuid4().hex
        manifest,stamp=create_customer([6],snap,batch,source,"synthetic-owner")
        create_customer([6],snap,batch,source,"synthetic-owner",stamp)
        cid=manifest["entries"][0]["customer_id"]
        vid=uuid.uuid4().hex
        with connect() as db:
            db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                       (vid,cid,"Hyundai","i30","2022","AB22 CDE","2026-01-01"))
        links=[]
        for row,ref in ((6,"J0456"),(7,"J0457")):
            snap=create_snapshot(uuid.uuid4().hex)
            link=uuid.uuid4().hex
            _,stamp=link_vehicle(source,row,ref,6,cid,vid,snap,link,"synthetic-owner")
            link_vehicle(source,row,ref,6,cid,vid,snap,link,"synthetic-owner",stamp)
            links.append(link)
        url="/hq/migration/customers/create-work"
        self.assertEqual(self.client.get(url).status_code,200)
        def payload(link,snapshot):
            return dict(csrf="test-csrf",action="prepare",operation=uuid.uuid4().hex,
                        link_id=link,service="Window Tinting",snapshot_id=snapshot)
        def approved(data,page):
            self.assertEqual(page.status_code,200,page.data)
            stamp=re.search(rb'name="stamp" value="([0-9a-f]{64})"',page.data).group(1).decode()
            return dict(data,action="apply",confirm="yes",stamp=stamp)
        a,b=self.live_source()
        with a,b:
            data=payload(links[0],create_snapshot(uuid.uuid4().hex))
            first=approved(data,self.client.post(url,data=data))
            self.assertEqual(self.client.post(url,data=first).status_code,200)
            data=payload(links[1],create_snapshot(uuid.uuid4().hex))
            page=self.client.post(url,data=data)
            second=approved(data,page)
            self.assertIn(b"Existing verified historical drafts for this vehicle",page.data)
            self.assertIn(b"J0456",page.data)
            # Fresh source checks cover earlier work too.
            self.computed["valueRanges"][0]["values"][2][16]=126
            self.assertEqual(self.client.post(url,data=second).status_code,409)
            self.computed["valueRanges"][0]["values"][2][16]=125
            # Any unrelated native work on the same vehicle blocks creation.
            other=uuid.uuid4().hex
            with connect() as db:
                db.execute("""INSERT INTO hq_work_records
                    (id,source_request_id,customer_id,vehicle_id,kind,service,notes,
                    preferred_date,preferred_time,status,created_at)
                    VALUES(?,?,?,?,'quote','Window Tinting','','','','draft','2026-01-01')""",
                    (other,"HQ-"+other,cid,vid))
            self.assertEqual(self.client.post(url,data=second).status_code,409)
            with connect() as db:
                db.execute("DELETE FROM hq_work_records WHERE id=?",(other,))
            self.assertEqual(self.client.post(url,data=second).status_code,200)
            self.assertEqual(self.client.post(url,data=second).status_code,200)
        first_records=search_work_records(query="j0456",kind="quote",status="draft")
        second_records=search_work_records(query="J0457",kind="quote")
        self.assertEqual((first_records["total"],second_records["total"]),(1,1))
        one,two=first_records["records"][0],second_records["records"][0]
        self.assertNotEqual(one["id"],two["id"])
        self.assertEqual((one["price_pence"],two["price_pence"]),(12500,17500))
        self.assertEqual(search_work_records(query="J0457",kind="job")["total"],0)
        self.assertEqual(search_work_records(query="J045",kind="quote")["total"],0)
        page=self.client.get("/hq/work/"+two["id"]+"/edit")
        self.assertEqual(page.status_code,200)
        self.assertIn(b"Original historical reference: J0457",page.data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hq_historical_created_work").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_job_sheet_links").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_job_sync_outbox").fetchone()[0],0)
            # Owner note changes cannot erase the original reference.
            db.execute("UPDATE hq_work_records SET notes='Edited synthetic notes',revision=1 WHERE id=?",(two["id"],))
        self.assertEqual(search_work_records(query="J0457")["total"],1)
        self.assertIn(b"Original historical reference: J0457",
                      self.client.get("/hq/work/"+two["id"]+"/edit").data)
        self.assertEqual(app.test_client().get("/hq/work/"+two["id"]+"/edit").status_code,302)


    def test_historical_nonquote_status_is_evidence_not_live_finance_or_booking(self):
        import re
        import json
        from hq_private_recovery import create_snapshot
        from hq_historical_customer_creation import review as create_customer
        from hq_historical_vehicle_links import review as link_vehicle
        from hq_core_records import change_work_status, set_draft_financials, work_summary, search_work_records
        self.login()
        examples = (("J0456", "Paid", 125), ("J0457", "Completed", 175),
                    ("J0458", "Booked", 200), ("J0459", "Awaiting Payment", ""),
                    ("J0460", "Enquiry", ""), ("J0461", "Declined", 225))
        for payload in (self.formula,self.computed):
            payload["valueRanges"][1]["values"].append(["Synthetic History Driver","Retail",""])
            for ref,status,price in examples:
                payload["valueRanges"][0]["values"].append(
                    [ref,"","Synthetic History Driver","Retail","AB22 CDE","Window Tinting"]
                    +[""]*10+[price,status])
        for payload in (self.formula,self.computed):
            rows=payload["valueRanges"][0]["values"]
            rows[0].extend([""]*(32-len(rows[0]))+
                ["Actual Hours","Actual Materials £","Actual Consumables £","Actual Extra Costs £"])
            rows[2].extend([""]*(32-len(rows[2]))+[2.1,12.34,0,""])
        source=(self.formula,self.computed)
        snap=create_snapshot(uuid.uuid4().hex)
        batch=uuid.uuid4().hex
        manifest,stamp=create_customer([6],snap,batch,source,"synthetic-owner")
        create_customer([6],snap,batch,source,"synthetic-owner",stamp)
        cid=manifest["entries"][0]["customer_id"]
        vid=uuid.uuid4().hex
        with connect() as db:
            db.execute("INSERT INTO hq_vehicles VALUES(?,?,?,?,?,?,?)",
                       (vid,cid,"Hyundai","i30","2022","AB22 CDE","2026-01-01"))
        links=[]
        for row,(ref,status,price) in enumerate(examples,6):
            snap=create_snapshot(uuid.uuid4().hex)
            link=uuid.uuid4().hex
            _,stamp=link_vehicle(source,row,ref,6,cid,vid,snap,link,"synthetic-owner")
            link_vehicle(source,row,ref,6,cid,vid,snap,link,"synthetic-owner",stamp)
            links.append(link)
        url="/hq/migration/customers/create-work"
        self.assertEqual(self.client.get(url).status_code,200)
        before=work_summary()
        applied=[]
        a,b=self.live_source()
        with a,b:
            for i,(link,(ref,status,price)) in enumerate(zip(links,examples)):
                data=dict(csrf="test-csrf",action="prepare",operation=uuid.uuid4().hex,
                          link_id=link,service="Window Tinting",snapshot_id=create_snapshot(uuid.uuid4().hex))
                page=self.client.post(url,data=data)
                self.assertEqual(page.status_code,200,page.data)
                self.assertIn(("Source status: "+status).encode(),page.data)
                self.assertIn(b"without a live price or operational status",page.data)
                stamp=re.search(rb'name="stamp" value="([0-9a-f]{64})"',page.data).group(1).decode()
                apply=dict(data,action="apply",confirm="yes",stamp=stamp)
                self.assertEqual(self.client.post(url,data=dict(apply,confirm="")).status_code,409)
                if i==0:
                    self.computed["valueRanges"][0]["values"][2][17]="Completed"
                    self.assertEqual(self.client.post(url,data=apply).status_code,409)
                    self.computed["valueRanges"][0]["values"][2][17]=status
                self.assertEqual(self.client.post(url,data=apply).status_code,200)
                self.assertEqual(self.client.post(url,data=apply).status_code,200)
                applied.append(apply)
        for ref,status,price in examples:
            kind="quote" if status in ("Enquiry", "Declined") else "job"
            result=search_work_records(query=ref,kind=kind,status="draft")
            self.assertEqual(result["total"],1)
            wid=result["records"][0]["id"]
            with connect() as db:
                work=db.execute("SELECT * FROM hq_work_records WHERE id=?",(wid,)).fetchone()
                self.assertEqual((work["kind"],work["status"],work["price_pence"],work["deposit_pence"],
                                  work["preferred_date"],work["preferred_time"]),
                                 (kind,"draft",None,0,"",""))
                evidence=json.loads(db.execute("SELECT manifest FROM hq_historical_created_work WHERE work_id=?",
                                               (wid,)).fetchone()[0])
                self.assertEqual(evidence["source_status"],status)
                self.assertEqual(evidence["price_pence"],price*100 if price!="" else None)
            page=self.client.get("/hq/work/"+wid+"/edit")
            self.assertEqual(page.status_code,200)
            self.assertIn(("Original workbook status: "+status).encode(),page.data)
            self.assertIn(b"blocked pending independent reconciliation",page.data)
            with self.assertRaises(ValueError):
                change_work_status(wid,"ready_for_review",0)
            with self.assertRaises(ValueError):
                set_draft_financials(wid,12500,0,0)
            self.assertEqual(app.test_client().get("/hq/work/"+wid+"/edit").status_code,302)
        wid=search_work_records(query="J0456",kind="job",status="draft")["records"][0]["id"]
        cost_url="/hq/migration/work/"+wid+"/actual-costs"
        self.assertEqual(app.test_client().get(cost_url).status_code,302)
        self.assertEqual(self.client.get(cost_url).status_code,200)
        a,b=self.live_source()
        with a,b:
            prepare=dict(csrf="test-csrf",action="prepare",operation=uuid.uuid4().hex)
            self.assertEqual(self.client.post(cost_url,data=dict(prepare,csrf="bad")).status_code,403)
            page=self.client.post(cost_url,data=prepare)
            self.assertEqual(page.status_code,200,page.data)
            for value in (b"2.1 hours",b"12.34",b"0.00",b"Unknown (blank)"):
                self.assertIn(value,page.data)
            stamp=re.search(rb'name="stamp" value="([0-9a-f]{64})"',page.data).group(1).decode()
            save=dict(prepare,action="save",stamp=stamp,confirm="yes")
            self.assertEqual(self.client.post(cost_url,data=dict(save,confirm="")).status_code,409)
            self.computed["valueRanges"][0]["values"][2][33]=12.35
            self.assertEqual(self.client.post(cost_url,data=save).status_code,409)
            self.computed["valueRanges"][0]["values"][2][33]=12.34
            self.assertEqual(self.client.post(cost_url,data=save).status_code,200)
            self.assertEqual(self.client.post(cost_url,data=save).status_code,200)
            self.assertEqual(self.client.post(cost_url,data=dict(prepare,operation=uuid.uuid4().hex)).status_code,409)
        page=self.client.get(cost_url)
        self.assertEqual(page.headers["Cache-Control"],"no-store")
        self.assertIn(b"12.34",page.data)
        with connect() as db:
            rows=db.execute("SELECT * FROM hq_historical_cost_observations WHERE work_id=?",(wid,)).fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(json.loads(rows[0]["manifest"])["costs"],
                dict(hours="2.1",materials=1234,consumables=0,extra=None))
        withdraw=dict(csrf="test-csrf",action="withdraw",operation=uuid.uuid4().hex,stamp=stamp,confirm="yes")
        self.assertEqual(self.client.post(cost_url,data=withdraw).status_code,200)
        self.assertEqual(self.client.post(cost_url,data=withdraw).status_code,200)
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hq_historical_cost_observations WHERE work_id=?",(wid,)).fetchone()[0],2)
            self.assertEqual(db.execute("SELECT revision FROM hq_work_records WHERE id=?",(wid,)).fetchone()[0],0)
        after=work_summary()
        for key in ("priced_draft_value_pence","outstanding_recorded_pence",
                    "approved_price_total_pence","planned_deposit_total_pence",
                    "verified_cash_received_pence","verified_cash_outstanding_pence"):
            self.assertEqual(after[key],before[key],key)
        with connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM hq_job_sheet_links").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM hq_job_sync_outbox").fetchone()[0],0)
        last=applied[-1]
        reversal=dict(csrf="test-csrf",action="reverse",operation=last["operation"],
                      stamp=last["stamp"],confirm="yes")
        self.assertEqual(self.client.post(url,data=reversal).status_code,200)
        self.assertEqual(search_work_records(query="J0461",kind="quote")["total"],0)


if __name__=="__main__":
    unittest.main()
