"""Synthetic customer-to-HQ product-proceed request tests; never touch production."""
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_combined_baskets import (
    submit_combined, review_combined, create_product_only_quote,
    change_product_quote_status,
)
from hq_commerce import database as commerce_db, save_product
from hq_core_records import _schema
from hq_customer_portal import issue_customer_invite
from hq_product_quote_lines import save_product_quote_lines
from hq_product_proceed_requests import (
    request_to_proceed, customer_proceed_state, owner_proceed_requests,
    owner_review_proceed, owner_set_preparation,
    owner_create_product_order, owner_save_product_order,
    owner_save_product_handover,
)

ROOT = Path(__file__).resolve().parents[1]


class ProductProceedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name, "HQ_SESSION_SECRET": "synthetic-proceed-secret",
            "GOOGLE_CLIENT_ID": "fixture", "GOOGLE_CLIENT_SECRET": "fixture",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.owner, self.mine, self.other = (app.test_client() for _ in range(3))
        self.cid, self.stranger = uuid.uuid4().hex, uuid.uuid4().hex
        with connect() as db:
            _schema(db)
            for customer_id, name in ((self.cid, "Test Product Buyer"),
                                      (self.stranger, "Other Buyer")):
                db.execute("INSERT INTO hq_customers VALUES (?,?,?,?,?,?)",
                           (customer_id, None, name, "buyer@example.invalid",
                            "07000000000", "2026-11-23"))
        self.product = save_product({
            "name": "Synthetic Spray", "variant": "Sample",
            "description": "Unit test only", "retail": 3000, "trade": 2000,
            "cost": 1000, "stock": 5, "delivery": 0, "published": 1})
        self.basket = submit_combined(
            self.cid, uuid.uuid4().hex, None, "retail", [],
            [{"id": self.product, "quantity": 1}], "collection")
        review_combined(self.basket, 0, "quoted", "40.00", "Test product quote")
        self.quote = create_product_only_quote(self.basket, 1)
        save_product_quote_lines(self.basket, 0, ["40.00"])
        change_product_quote_status(self.basket, 1, "approved")

    def owner_login(self):
        with self.owner.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="owner-csrf", google_sub="synthetic-owner")

    def login_customer(self, client, customer_id):
        code = issue_customer_invite(customer_id, uuid.uuid4().hex)
        with client.session_transaction() as sess:
            sess["customer_portal_login_csrf"] = "login-csrf"
        response = client.post("/customer/login",
                               data={"csrf": "login-csrf", "code": code})
        self.assertEqual(response.status_code, 303)

    def test_customer_request_owner_approval_and_durable_customer_visibility(self):
        self.login_customer(self.mine, self.cid)
        self.login_customer(self.other, self.stranger)
        self.assertEqual(self.owner.get("/hq/product-proceed-requests").status_code, 302)
        page = self.mine.get("/customer/basket-requests")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Request to proceed with this product quotation", page.data)
        self.assertNotIn(self.basket.encode(), self.other.get("/customer/basket-requests").data)
        with self.mine.session_transaction() as sess:
            csrf = sess["customer_portal_csrf"]
        path = "/customer/basket-requests/" + self.basket + "/request-proceed"
        op = uuid.uuid4().hex
        self.assertEqual(self.mine.post(path, data={
            "csrf": csrf, "operation_id": op
        }).status_code, 303)
        ident = request_to_proceed(self.basket, self.cid, op)
        self.assertEqual(ident, request_to_proceed(self.basket, self.cid, uuid.uuid4().hex))
        self.assertEqual(len(owner_proceed_requests()), 1)
        self.assertEqual(customer_proceed_state(self.basket, self.cid)["status"], "pending")
        self.assertIsNone(customer_proceed_state(self.basket, self.stranger))
        self.assertIn(b"Request to proceed: Pending",
                      self.mine.get("/customer/basket-requests").data)
        self.owner_login()
        owner_path = "/hq/product-proceed-requests"
        self.assertEqual(self.owner.get(owner_path).status_code, 200)
        self.assertEqual(self.owner.post(owner_path, data={
            "csrf": "wrong", "request_id": ident, "revision": "0",
            "status": "approved"}).status_code, 403)
        self.assertEqual(self.owner.post(owner_path, data={
            "csrf": "owner-csrf", "request_id": ident, "revision": "0",
            "status": "approved", "public_note": "We will contact you to arrange next steps."
        }).status_code, 303)
        result = customer_proceed_state(self.basket, self.cid)
        self.assertEqual(result["status"], "approved")
        self.assertIn("arrange next steps", result["public_note"])
        self.assertIn(b"Request to proceed: Approved",
                      self.mine.get("/customer/basket-requests").data)
        self.assertNotIn(b"Request to proceed: Approved",
                         self.other.get("/customer/basket-requests").data)
        with connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_product_proceed_events"
            ).fetchone()[0], 2)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)
        with commerce_db() as db:
            self.assertEqual(db.execute(
                "SELECT stock FROM products WHERE id=?", (self.product,)).fetchone()[0], 5)

    def test_security_conflicts_and_immutable_approval_gate(self):
        self.login_customer(self.mine, self.cid)
        self.login_customer(self.other, self.stranger)
        path = "/customer/basket-requests/" + self.basket + "/request-proceed"
        with self.mine.session_transaction() as sess:
            csrf = sess["customer_portal_csrf"]
        with self.other.session_transaction() as sess:
            other_csrf = sess["customer_portal_csrf"]
        self.assertEqual(self.other.post(path, data={
            "csrf": other_csrf, "operation_id": uuid.uuid4().hex
        }).status_code, 404)
        self.assertEqual(self.mine.post(path, data={
            "csrf": "wrong", "operation_id": uuid.uuid4().hex
        }).status_code, 403)
        first_op = uuid.uuid4().hex
        ident = request_to_proceed(self.basket, self.cid, first_op)
        with self.assertRaisesRegex(ValueError, "previously used"):
            request_to_proceed(uuid.uuid4().hex, self.cid, first_op)
        with app.app_context(), self.assertRaises(ValueError):
            owner_review_proceed(ident, 55, "approved", "", "synthetic-owner")
        self.owner_login()
        with app.app_context(), self.assertRaisesRegex(ValueError, "customer-visible note|too long"):
            owner_review_proceed(ident, 0, "approved", "x" * 401, "synthetic-owner")
        change_product_quote_status(self.basket, 2, "withdrawn")
        with app.app_context(), self.assertRaisesRegex(ValueError, "changed or withdrawn"):
            owner_review_proceed(ident, 0, "approved", "", "synthetic-owner")
        with connect() as db:
            self.assertEqual(db.execute(
                "SELECT status FROM hq_product_proceed_requests WHERE id=?",
                (ident,)).fetchone()[0], "pending")
        with commerce_db() as db:
            self.assertEqual(db.execute(
                "SELECT stock FROM products WHERE id=?", (self.product,)).fetchone()[0], 5)


    def test_owner_preparation_ledger_is_editable_audited_and_customer_scoped(self):
        self.login_customer(self.mine, self.cid)
        self.login_customer(self.other, self.stranger)
        ident = request_to_proceed(self.basket, self.cid, uuid.uuid4().hex)
        self.owner_login()
        path = "/hq/product-proceed-requests"
        with self.assertRaisesRegex(ValueError, "Owner approval"):
            with app.app_context():
                owner_set_preparation(ident, 0, "planning", "", "synthetic-owner")
        self.assertEqual(self.owner.post(path, data={
            "csrf": "owner-csrf", "request_id": ident, "revision": "0",
            "status": "approved", "public_note": "Proceed approved."
        }).status_code, 303)
        self.assertIn(b"Save preparation progress", self.owner.get(path).data)
        self.assertEqual(self.owner.post(path, data={
            "csrf": "wrong", "action": "preparation", "request_id": ident,
            "revision": "0", "stage": "planning"
        }).status_code, 403)
        self.assertEqual(self.owner.post(path, data={
            "csrf": "owner-csrf", "action": "preparation", "request_id": ident,
            "revision": "0", "stage": "planning",
            "preparation_note": "We are planning your collection."
        }).status_code, 303)
        progress = customer_proceed_state(self.basket, self.cid)
        self.assertEqual(progress["preparation_stage"], "planning")
        self.assertEqual(progress["preparation_note"],
                         "We are planning your collection.")
        self.assertIsNone(customer_proceed_state(self.basket, self.stranger))
        self.assertIn(b"Product preparation: Planning",
                      self.mine.get("/customer/basket-requests").data)
        self.assertNotIn(b"Product preparation: Planning",
                         self.other.get("/customer/basket-requests").data)
        self.assertEqual(owner_proceed_requests()[0]["preparation_revision"], 1)
        with app.app_context():
            self.assertEqual(owner_set_preparation(
                ident, 0, "planning", "We are planning your collection.",
                "synthetic-owner"), 1)  # idempotent retry
            with self.assertRaisesRegex(ValueError, "requires collection"):
                owner_set_preparation(ident, 1, "ready_for_delivery", "",
                                      "synthetic-owner")
            with self.assertRaisesRegex(ValueError, "changed"):
                owner_set_preparation(ident, 0, "preparing", "", "synthetic-owner")
            with self.assertRaisesRegex(ValueError, "too long"):
                owner_set_preparation(ident, 1, "preparing", "x" * 401,
                                      "synthetic-owner")
        self.assertEqual(self.owner.post(path, data={
            "csrf": "owner-csrf", "action": "preparation", "request_id": ident,
            "revision": "1", "stage": "ready_for_collection",
            "preparation_note": "Products checked for collection."
        }).status_code, 303)
        self.assertIn(b"Product preparation: Ready For Collection",
                      self.mine.get("/customer/basket-requests").data)
        with connect() as db:
            events = db.execute("""SELECT revision,stage FROM
                hq_product_preparation_events WHERE request_id=?
                ORDER BY revision""", (ident,)).fetchall()
            self.assertEqual([(row[0], row[1]) for row in events],
                             [(1, "planning"), (2, "ready_for_collection")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)
        with commerce_db() as db:
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?",
                                        (self.product,)).fetchone()[0], 5)
        change_product_quote_status(self.basket, 2, "withdrawn")
        with app.app_context(), self.assertRaisesRegex(ValueError, "changed or withdrawn"):
            owner_set_preparation(ident, 2, "paused", "", "synthetic-owner")



    def test_owner_explicit_product_order_customer_visibility_and_audit(self):
        """Opening a canonical order is a separate owner action; no money or stock changes."""
        self.login_customer(self.mine, self.cid)
        self.login_customer(self.other, self.stranger)
        ident = request_to_proceed(self.basket, self.cid, uuid.uuid4().hex)
        self.owner_login()
        url = "/hq/product-proceed-requests"
        with app.app_context(), self.assertRaisesRegex(ValueError, "Owner approval"):
            owner_create_product_order(ident, 0, "synthetic-owner")
        self.assertEqual(self.owner.post(url, data={
            "csrf": "owner-csrf", "request_id": ident, "revision": "0",
            "status": "approved", "public_note": "Ready to organise."
        }).status_code, 303)
        self.assertIn(b"Create native product order record", self.owner.get(url).data)
        self.assertEqual(self.owner.post(url, data={
            "csrf": "invalid", "action": "order_create", "request_id": ident,
            "revision": "1"
        }).status_code, 403)
        payload = {"csrf": "owner-csrf", "action": "order_create",
                   "request_id": ident, "revision": "1"}
        self.assertEqual(self.owner.post(url, data=payload).status_code, 303)
        with app.app_context():
            order_id = owner_create_product_order(ident, 1, "synthetic-owner")
            self.assertEqual(order_id, owner_create_product_order(
                ident, 0, "synthetic-owner"))  # retry cannot duplicate
        current = customer_proceed_state(self.basket, self.cid)
        self.assertEqual(current["order_id"], order_id)
        self.assertEqual(current["order_status"], "open")
        self.assertIsNone(customer_proceed_state(self.basket, self.stranger))
        self.assertIn(b"Native product order", self.mine.get(
            "/customer/basket-requests").data)
        self.assertNotIn(order_id[:8].upper().encode(), self.other.get(
            "/customer/basket-requests").data)
        self.assertEqual(self.owner.post(url, data={
            "csrf": "owner-csrf", "action": "order_edit",
            "request_id": ident, "revision": "0",
            "order_status": "on_hold",
            "order_note": "Waiting for your collection preference."
        }).status_code, 303)
        self.assertEqual(customer_proceed_state(
            self.basket, self.cid)["order_status"], "on_hold")
        with app.app_context():
            self.assertEqual(owner_save_product_order(
                ident, 0, "on_hold", "Waiting for your collection preference.",
                "synthetic-owner"), 1)
            with self.assertRaisesRegex(ValueError, "Order changed"):
                owner_save_product_order(ident, 0, "open", "", "synthetic-owner")
            with self.assertRaisesRegex(ValueError, "too long"):
                owner_save_product_order(ident, 1, "open", "x" * 401,
                                         "synthetic-owner")
        self.assertEqual(self.owner.post(url, data={
            "csrf": "owner-csrf", "action": "order_edit", "request_id": ident,
            "revision": "1", "order_status": "closed",
            "order_note": "Administratively closed; payment not verified."
        }).status_code, 303)
        with app.app_context(), self.assertRaisesRegex(ValueError, "cannot be amended"):
            owner_save_product_order(ident, 2, "open", "", "synthetic-owner")
        with connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_product_orders").fetchone()[0], 1)
            events = db.execute("""SELECT revision,status FROM hq_product_order_events
                WHERE order_id=? ORDER BY revision""", (order_id,)).fetchall()
            self.assertEqual([tuple(row) for row in events],
                             [(0, "open"), (1, "on_hold"), (2, "closed")])
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)
        with commerce_db() as db:
            self.assertEqual(db.execute(
                "SELECT stock FROM products WHERE id=?", (self.product,)
            ).fetchone()[0], 5)

    def test_product_order_creation_rejects_withdrawn_quote(self):
        ident = request_to_proceed(self.basket, self.cid, uuid.uuid4().hex)
        with app.app_context():
            owner_review_proceed(ident, 0, "approved", "", "synthetic-owner")
        change_product_quote_status(self.basket, 2, "withdrawn")
        with app.app_context(), self.assertRaisesRegex(
                ValueError, "changed or withdrawn|Original product offer changed"):
            owner_create_product_order(ident, 1, "synthetic-owner")
        with connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_product_orders").fetchone()[0], 0)



    def test_owner_order_handover_is_audited_private_and_customer_visible(self):
        """Synthetic end-to-end owner record and dispute; no provider or inventory writes."""
        self.login_customer(self.mine, self.cid)
        self.login_customer(self.other, self.stranger)
        ident = request_to_proceed(self.basket, self.cid, uuid.uuid4().hex)
        self.owner_login()
        url = "/hq/product-proceed-requests"
        with app.app_context():
            with self.assertRaises(LookupError):
                owner_save_product_handover(ident, 0, "record",
                    "collection_confirmation", "proof-001",
                    datetime.now(timezone.utc).date().isoformat(), "", "owner")
            owner_review_proceed(ident, 0, "approved", "", "owner")
            order_id = owner_create_product_order(ident, 1, "owner")
        page = self.owner.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Record owner-reported handover evidence", page.data)
        self.assertEqual(self.owner.post(url, data={
            "csrf": "invalid", "action": "handover_record", "request_id": ident,
            "revision": "0", "evidence_type": "collection_confirmation",
            "evidence_reference": "proof-001",
            "handover_date": datetime.now(timezone.utc).date().isoformat(),
        }).status_code, 403)
        today = datetime.now(timezone.utc).date().isoformat()
        with app.app_context():
            with self.assertRaisesRegex(ValueError, "fulfilment method"):
                owner_save_product_handover(ident, 0, "record",
                    "carrier_handover_receipt", "proof-001", today, "", "owner")
            with self.assertRaisesRegex(ValueError, "future"):
                owner_save_product_handover(ident, 0, "record",
                    "collection_confirmation", "proof-001",
                    (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat(),
                    "", "owner")
        payload = {"csrf": "owner-csrf", "action": "handover_record",
                   "request_id": ident, "revision": "0",
                   "evidence_type": "collection_confirmation",
                   "evidence_reference": "private-collection-signature-001",
                   "handover_date": today,
                   "handover_note": "Your order was handed over."}
        self.assertEqual(self.owner.post(url, data=payload).status_code, 303)
        state = customer_proceed_state(self.basket, self.cid)
        self.assertEqual(state["handover_state"], "recorded")
        self.assertEqual(state["handover_date"], today)
        self.assertEqual(state["handover_note"], "Your order was handed over.")
        self.assertNotIn("evidence_reference", state)
        self.assertIsNone(customer_proceed_state(self.basket, self.stranger))
        customer_page = self.mine.get("/customer/basket-requests").data
        self.assertIn(b"Handover recorded by Tommy Tints", customer_page)
        self.assertNotIn(b"private-collection-signature-001", customer_page)
        self.assertNotIn(b"private-collection-signature-001",
                         self.other.get("/customer/basket-requests").data)
        with app.app_context():
            self.assertEqual(owner_save_product_handover(
                ident, 0, "record", "collection_confirmation",
                "private-collection-signature-001", today,
                "Your order was handed over.", "owner"), 1)
            with self.assertRaisesRegex(ValueError, "already recorded"):
                owner_save_product_handover(
                    ident, 1, "record", "collection_confirmation",
                    "another-proof", today, "", "owner")
            with self.assertRaisesRegex(ValueError, "changed"):
                owner_save_product_handover(ident, 0, "dispute", "", "", "",
                    "Earlier record needs checking with customer.", "owner")
        dispute = {"csrf": "owner-csrf", "action": "handover_dispute",
                   "request_id": ident, "revision": "1",
                   "handover_note": "Collection signature needs manual review."}
        self.assertEqual(self.owner.post(url, data=dispute).status_code, 303)
        self.assertEqual(customer_proceed_state(
            self.basket, self.cid)["handover_state"], "disputed")
        self.assertIn(b"Handover under review",
                      self.mine.get("/customer/basket-requests").data)
        self.assertIn(b"Handover audit history (2 events)", self.owner.get(url).data)
        with connect() as db:
            events = db.execute("""SELECT revision,state,evidence_reference
                FROM hq_product_order_handover_events
                WHERE order_id=? ORDER BY revision""", (order_id,)).fetchall()
            self.assertEqual([(r[0], r[1]) for r in events],
                             [(1, "recorded"), (2, "disputed")])
            self.assertEqual({r[2] for r in events},
                             {"private-collection-signature-001"})
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_work_records").fetchone()[0], 0)
        with commerce_db() as db:
            self.assertEqual(db.execute(
                "SELECT stock FROM products WHERE id=?",
                (self.product,)).fetchone()[0], 5)

    def test_product_handover_requires_current_approved_open_order(self):
        ident = request_to_proceed(self.basket, self.cid, uuid.uuid4().hex)
        with app.app_context():
            owner_review_proceed(ident, 0, "approved", "", "owner")
            owner_create_product_order(ident, 1, "owner")
            owner_save_product_order(ident, 0, "on_hold", "", "owner")
            with self.assertRaisesRegex(ValueError, "Only open approved"):
                owner_save_product_handover(ident, 0, "record",
                    "collection_confirmation", "proof-001",
                    datetime.now(timezone.utc).date().isoformat(), "", "owner")
            owner_save_product_order(ident, 1, "open", "", "owner")
        change_product_quote_status(self.basket, 2, "withdrawn")
        with app.app_context(), self.assertRaisesRegex(
                ValueError, "changed or withdrawn"):
            owner_save_product_handover(ident, 0, "record",
                "collection_confirmation", "proof-001",
                datetime.now(timezone.utc).date().isoformat(), "", "owner")
        with connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM hq_product_order_handover").fetchone()[0], 0)


    def test_product_deposit_plan_owner_edit_customer_scope_and_revision(self):
        ident = request_to_proceed(self.basket, self.cid, uuid.uuid4().hex)
        self.owner_login()
        self.login_customer(self.mine, self.cid)
        self.login_customer(self.other, self.stranger)
        with app.app_context():
            owner_review_proceed(ident, 0, "approved", "", "owner")
            order_id = owner_create_product_order(ident, 1, "owner")
        url = "/hq/product-proceed-requests"
        form = {"csrf":"owner-csrf","action":"deposit_plan","request_id":ident,
                "revision":"0","order_revision":"0","deposit_amount":"12.50"}
        self.assertIn(b"Save proposed deposit", self.owner.get(url).data)
        self.assertEqual(self.owner.post(url,data={**form,"csrf":"bad"}).status_code,403)
        self.assertNotEqual(self.other.post(url,data=form).status_code,303)
        for invalid in ("40.01","-1","1.001","NaN"):
            self.assertEqual(self.owner.post(url,data={**form,"deposit_amount":invalid}).status_code,409)
        self.assertEqual(self.owner.post(url,data=form).status_code,303)
        self.assertEqual(self.owner.post(url,data=form).status_code,303)
        self.assertIn(b"Proposed deposit: ", self.mine.get("/customer/basket-requests").data)
        self.assertNotIn(b"Proposed deposit:", self.other.get("/customer/basket-requests").data)
        state = customer_proceed_state(self.basket,self.cid)
        self.assertEqual(state["proposed_deposit_pence"],1250)
        self.assertEqual(state["conditional_remainder_pence"],2750)
        self.assertEqual(self.owner.post(url,data={**form,"deposit_amount":"1"}).status_code,409)
        self.assertEqual(self.owner.post(url,data={**form,"revision":"1","deposit_amount":""}).status_code,303)
        self.assertNotIn("proposed_deposit_pence",customer_proceed_state(self.basket,self.cid))
        self.assertEqual(self.owner.post(url,data={**form,"revision":"2","deposit_amount":"0"}).status_code,303)
        self.assertEqual(customer_proceed_state(self.basket,self.cid)["proposed_deposit_pence"],0)
        with app.app_context():
            owner_save_product_order(ident,0,"on_hold","","owner")
            owner_save_product_order(ident,1,"open","","owner")
        self.assertNotIn("proposed_deposit_pence",customer_proceed_state(self.basket,self.cid))
        self.assertEqual(self.owner.post(url,data={**form,"revision":"3"}).status_code,409)
        self.assertEqual(self.owner.post(url,data={**form,"revision":"3","order_revision":"2"}).status_code,303)
        change_product_quote_status(self.basket,2,"withdrawn")
        self.assertNotIn("proposed_deposit_pence",customer_proceed_state(self.basket,self.cid))
        self.assertEqual(self.owner.post(url,data={**form,"revision":"4","order_revision":"2"}).status_code,409)
        with connect() as db:
            events=db.execute("""SELECT revision,proposed_pence FROM hq_product_order_deposit_events
                WHERE order_id=? ORDER BY revision""",(order_id,)).fetchall()
            self.assertEqual([tuple(r) for r in events],[(1,1250),(2,None),(3,0),(4,1250)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0],0)
        with commerce_db() as db:
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?",(self.product,)).fetchone()[0],5)


    def test_customer_acknowledges_only_current_own_deposit_view(self):
        ident = request_to_proceed(self.basket,self.cid,uuid.uuid4().hex)
        self.owner_login()
        self.login_customer(self.mine,self.cid)
        self.login_customer(self.other,self.stranger)
        with app.app_context():
            owner_review_proceed(ident,0,"approved","","owner")
            order_id = owner_create_product_order(ident,1,"owner")
        owner_url = "/hq/product-proceed-requests"
        plan = dict(csrf="owner-csrf",action="deposit_plan",request_id=ident,
                    revision="0",order_revision="0",deposit_amount="12.50")
        self.assertEqual(self.owner.post(owner_url,data=plan).status_code,303)
        path = "/customer/basket-requests/"+self.basket+"/acknowledge-deposit"
        with self.mine.session_transaction() as sess:
            csrf = sess["customer_portal_csrf"]
        with self.other.session_transaction() as sess:
            other_csrf = sess["customer_portal_csrf"]
        form = dict(csrf=csrf,order_id=order_id,deposit_revision="1")
        self.assertIn(b"Acknowledge viewing deposit proposal",self.mine.get("/customer/basket-requests").data)
        self.assertNotIn(b"Acknowledge viewing deposit proposal",self.other.get("/customer/basket-requests").data)
        self.assertEqual(self.other.post(path,data=dict(form,csrf=other_csrf)).status_code,404)
        self.assertEqual(self.mine.post(path,data=dict(form,csrf="bad")).status_code,403)
        self.assertEqual(self.mine.post(path,data=dict(form,order_id=uuid.uuid4().hex)).status_code,404)
        self.assertEqual(self.mine.post(path,data=dict(form,deposit_revision="2")).status_code,409)
        self.assertEqual(self.mine.post(path,data=form).status_code,303)
        self.assertEqual(self.mine.post(path,data=form).status_code,303)
        self.assertTrue(customer_proceed_state(self.basket,self.cid)["deposit_acknowledged"])
        self.assertIn(b"acknowledged viewing this deposit proposal",self.mine.get("/customer/basket-requests").data)
        self.assertIn(b"Customer viewed",self.owner.get(owner_url).data)
        self.assertEqual(self.owner.post(owner_url,data=dict(plan,revision="1",deposit_amount="15")).status_code,303)
        self.assertFalse(customer_proceed_state(self.basket,self.cid)["deposit_acknowledged"])
        self.assertEqual(self.mine.post(path,data=form).status_code,409)
        self.assertEqual(self.mine.post(path,data=dict(form,deposit_revision="2")).status_code,303)
        self.assertEqual(self.owner.post(owner_url,data=dict(plan,revision="2",deposit_amount="")).status_code,303)
        self.assertNotIn("deposit_acknowledged",customer_proceed_state(self.basket,self.cid))
        self.assertEqual(self.mine.post(path,data=dict(form,deposit_revision="2")).status_code,409)
        with connect() as db:
            self.assertEqual([tuple(row) for row in db.execute(
                "SELECT order_id,deposit_revision,customer_id FROM hq_product_deposit_acknowledgements ORDER BY deposit_revision")],
                [(order_id,1,self.cid),(order_id,2,self.cid)])
            self.assertEqual(db.execute("SELECT revision FROM hq_product_orders WHERE id=?",(order_id,)).fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_work_records").fetchone()[0],0)
        with commerce_db() as db:
            self.assertEqual(db.execute("SELECT stock FROM products WHERE id=?",(self.product,)).fetchone()[0],5)


if __name__ == "__main__":
    unittest.main()
