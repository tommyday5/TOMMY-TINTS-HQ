"""Synthetic, owner-only invoice allocation tests; no external settlement."""
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_core_records import _schema
from hq_receivables import save as save_invoice
from hq_receivable_payments import save as save_allocation


class AllocationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name,
            "HQ_SESSION_SECRET": "synthetic",
            "GOOGLE_CLIENT_ID": "test",
            "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        for target in ("hq_receivables.storage_ready", "hq_receivable_payments.storage_ready"):
            mocked = patch(target, return_value=True)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.client = app.test_client()
        self.customer = uuid.uuid4().hex
        self.invoice_id = uuid.uuid4().hex
        with connect() as db:
            _schema(db)
            db.execute("INSERT INTO hq_customers VALUES(?,?,?,?,?,?)",
                       (self.customer, None, "Synthetic Customer", "", "", "2026-11-22"))
        self.invoice = dict(id=self.invoice_id, operation_id=uuid.uuid4().hex,
                            revision=0, customer_id=self.customer, work_id=None,
                            invoice_ref="TEST-ALLOCATION-001", issue_date="2026-11-22",
                            due_date="2026-12-10", amount_pence=35000,
                            status="issued", memo="", archived=False)
        self.assertEqual(save_invoice(self.invoice), 1)

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess["hq_sid"] = issue_session(db, "synthetic-owner")
            sess["google_sub"] = "synthetic-owner"
            sess["csrf"] = "test"

    def payload(self, **updates):
        data = dict(id=uuid.uuid4().hex, invoice_id=self.invoice_id,
                    operation_id=uuid.uuid4().hex, revision=0,
                    amount_pence=20000, received_on="2026-11-22",
                    method="bank_reported", evidence_ref="SIMULATED-REF",
                    memo="Unverified evidence, synthetic", status="reported")
        data.update(updates)
        return data

    def post(self, payload, csrf="test"):
        return self.client.post("/api/hq/finance/receipt-allocations", json=payload,
                                headers={"X-HQ-CSRF": csrf})

    def test_owner_create_edit_reverse_history_and_isolation(self):
        self.assertEqual(self.client.get("/hq/finance/receipt-allocations").status_code, 302)
        self.assertEqual(self.client.get("/api/hq/finance/receipt-allocations").status_code, 302)
        self.login()
        page = self.client.get("/hq/finance/receipt-allocations")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"not verified bank receipts", page.data)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertIn(b"receipt-allocations", self.client.get("/hq/finance/receivables").data)
        first = self.payload()
        self.assertEqual(self.post(first, csrf="incorrect").status_code, 403)
        with patch("requests.post") as external_post, patch("requests.get") as external_get:
            response = self.post(first)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertFalse(response.json["bank_verified"])
            self.assertEqual(self.post(first).json["revision"], 1)
            external_post.assert_not_called()
            external_get.assert_not_called()
        initial = self.client.get("/api/hq/finance/receipt-allocations").json
        self.assertEqual(initial["allocations"][0]["invoice_id"], self.invoice_id)
        self.assertEqual(initial["invoices"][0]["owner_reported_allocated_pence"], 20000)
        self.assertEqual(initial["invoices"][0]["unallocated_owner_reported_pence"], 15000)
        self.assertFalse(initial["receipts_verified"])
        self.assertFalse(initial["outstanding_verified"])
        self.assertIsNone(initial["collected_income"])
        revised = {**first, "operation_id": uuid.uuid4().hex, "revision": 1,
                   "memo": "Reviewed manual reference"}
        self.assertEqual(self.post(revised).json["revision"], 2)
        # Original token is a legitimate idempotent replay; a *new* token is stale.
        self.assertEqual(self.post(first).json["revision"], 1)
        self.assertEqual(self.post({**first, "operation_id": uuid.uuid4().hex}).status_code, 409)
        reversed_entry = {**revised, "operation_id": uuid.uuid4().hex,
                          "revision": 2, "status": "reversed"}
        self.assertEqual(self.post(reversed_entry).json["revision"], 3)
        self.assertEqual(self.post({**reversed_entry, "operation_id": uuid.uuid4().hex,
                                    "revision": 3}).status_code, 400)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocation_events").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocation_operations").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivables").fetchone()[0], 1)
        after = self.client.get("/api/hq/finance/receipt-allocations").json
        self.assertEqual(after["invoices"][0]["owner_reported_allocated_pence"], 0)

    def test_amount_bounds_invoice_state_evidence_immutability(self):
        self.login()
        self.assertEqual(self.post(self.payload(amount_pence=True)).status_code, 400)
        self.assertEqual(self.post(self.payload(amount_pence=0)).status_code, 400)
        self.assertEqual(self.post(self.payload(invoice_id=uuid.uuid4().hex)).status_code, 400)
        self.assertEqual(self.post(self.payload(received_on="2026-02-30")).status_code, 400)
        self.assertEqual(self.post(self.payload(method="bank_verified")).status_code, 400)
        self.assertEqual(self.post(self.payload(evidence_ref="X"*121)).status_code, 400)
        self.assertEqual(self.post(self.payload(status="reversed")).status_code, 400)
        first = self.payload()
        self.assertEqual(self.post(first).status_code, 200)
        self.assertEqual(self.post(self.payload(amount_pence=15001)).status_code, 400)
        self.assertEqual(self.post({**first, "operation_id": uuid.uuid4().hex,
                                    "revision": 1, "amount_pence": 21000}).status_code, 400)
        self.assertEqual(self.post({**first, "amount_pence": 100}).status_code, 400)  # replay conflict
        # A report cannot silently be erased by reducing or voiding the invoice.
        changed = {**self.invoice, "operation_id": uuid.uuid4().hex, "revision": 1,
                   "amount_pence": 15000}
        with self.assertRaises(ValueError):
            save_invoice(changed)
        changed["status"] = "void"
        changed["amount_pence"] = 35000
        with self.assertRaises(ValueError):
            save_invoice(changed)

    def test_concurrent_retry_is_atomic_and_draft_is_not_payable(self):
        payload = self.payload()
        with ThreadPoolExecutor(max_workers=2) as executor:
            revisions = list(executor.map(save_allocation, (payload, payload)))
        self.assertEqual(revisions, [1, 1])
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocations").fetchone()[0], 1)
        # The source invoice has to have been reported issued/disputed first.
        pending_id = uuid.uuid4().hex
        pending = {**self.invoice, "id": pending_id, "operation_id": uuid.uuid4().hex,
                   "invoice_ref": "SYNTH-DRAFT", "status": "draft"}
        save_invoice(pending)
        with self.assertRaises(ValueError):
            save_allocation(self.payload(invoice_id=pending_id))


    def test_square_exact_evidence_persists_without_duplicate_receipt(self):
        import re
        self.login()
        payload = self.payload(method="card_reported", evidence_ref="square-test-123")
        self.assertEqual(self.post(payload).status_code, 200)
        url = "/hq/finance/square-invoice-evidence"
        form = dict(allocation=payload["id"], csrf="test", action="review")
        evidence = dict(id="square-test-123", status="COMPLETED", total_minor=20000,
                        refunded_minor=0, currency="GBP",
                        created_at="2026-11-22T10:00:00+00:00",
                        updated_at="2026-11-22T10:01:00+00:00")
        with patch.dict(os.environ, {"HQ_SQUARE_READ_TOKEN":"test", "HQ_SQUARE_LOCATION_ID":"test-loc"}), \
                patch("hq_square_invoice_evidence.storage_ready", return_value=True), \
                patch("hq_square_invoice_evidence.inspect_exact_payment", return_value=evidence) as provider:
            self.assertEqual(self.client.get(url+"?allocation="+payload["id"]).status_code, 200)
            provider.assert_not_called()
            self.assertEqual(self.client.post(url, data=dict(form, csrf="bad")).status_code, 403)
            provider.assert_not_called()
            page = self.client.post(url, data=form)
            self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
            manifest = re.search(rb'name="manifest" value="([0-9a-f]{64})"', page.data)[1].decode()
            approval = dict(form, action="save", manifest=manifest, confirm="yes")
            self.assertEqual(self.client.post(url, data=dict(approval, confirm="")).status_code, 400)
            provider.return_value = dict(evidence, refunded_minor=None)
            self.assertEqual(self.client.post(url, data=approval).status_code, 400)
            provider.return_value = dict(evidence, updated_at="2026-11-22T10:02:00+00:00")
            self.assertEqual(self.client.post(url, data=approval).status_code, 409)
            provider.return_value = evidence
            self.assertEqual(self.client.post(url, data=approval).status_code, 200)
            self.assertEqual(self.client.post(url, data=approval).status_code, 200)
            self.assertIn(b"Retained provider observation",
                          self.client.get(url+"?allocation="+payload["id"]).data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_square_invoice_observations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocations").fetchone()[0], 1)
        self.assertEqual(self.post(self.payload(amount_pence=100, evidence_ref="SQUARE-TEST-123")).status_code, 400)
        self.assertEqual(self.post(dict(payload, operation_id=uuid.uuid4().hex, revision=1,
                                        evidence_ref="other")).status_code, 400)
        self.assertEqual(self.post(dict(payload, operation_id=uuid.uuid4().hex, revision=1,
                                        status="reversed")).status_code, 200)
        self.assertEqual(self.post(self.payload(amount_pence=100, evidence_ref="square-test-123")).status_code, 400)
        with patch("hq_square_invoice_evidence.storage_ready", return_value=True):
            self.assertIn(b"no longer matches the observed revision",
                          self.client.get(url+"?allocation="+payload["id"]).data)
        after = self.client.get("/api/hq/finance/receipt-allocations").json
        self.assertFalse(after["receipts_verified"])
        self.assertFalse(after["outstanding_verified"])
        self.assertIsNone(after["collected_income"])

    def test_provider_receipt_recognition_approval_stale_refund_and_withdrawal(self):
        import re
        url = "/hq/finance/provider-confirmed-receipts"
        evidence_url = "/hq/finance/square-invoice-evidence"
        self.assertEqual(self.client.get(url).status_code, 302)
        self.login()
        payload = self.payload(method="card_reported", evidence_ref="square-recognition-test")
        self.assertEqual(self.post(payload).status_code, 200)
        form = dict(allocation=payload["id"], csrf="test", action="review")
        evidence = dict(id="square-recognition-test", status="COMPLETED", total_minor=20000,
                        refunded_minor=0, currency="GBP",
                        created_at="2026-11-22T10:00:00+00:00",
                        updated_at="2026-11-22T10:01:00+00:00")
        with patch.dict(os.environ, {"HQ_SQUARE_READ_TOKEN":"test", "HQ_SQUARE_LOCATION_ID":"test-loc"}), \
                patch("hq_square_invoice_evidence.storage_ready", return_value=True), \
                patch("hq_provider_receipts.storage_ready", return_value=True), \
                patch("hq_square_invoice_evidence.inspect_exact_payment", return_value=evidence) as provider, \
                patch("requests.post") as external_post:
            page = self.client.post(evidence_url, data=form)
            original = re.search(rb'name="manifest" value="([0-9a-f]{64})"', page.data)[1].decode()
            self.assertEqual(self.client.post(evidence_url, data=dict(form, action="save",
                manifest=original, confirm="yes")).status_code, 200)
            provider.reset_mock()
            self.assertEqual(self.client.get(url).status_code, 200)
            provider.assert_not_called()
            preview = dict(form, action="preview")
            self.assertEqual(self.client.post(url, data=dict(preview, csrf="bad")).status_code, 403)
            provider.assert_not_called()
            page = self.client.post(url, data=preview)
            self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
            manifest = re.search(rb'name="manifest" value="([0-9a-f]{64})"', page.data)[1].decode()
            approval = dict(preview, action="save", manifest=manifest, confirm="yes")
            self.assertEqual(self.client.post(url, data=dict(approval, confirm="")).status_code, 400)
            provider.return_value = dict(evidence, refunded_minor=None)
            self.assertEqual(self.client.post(url, data=approval).status_code, 400)
            provider.return_value = dict(evidence, updated_at="2026-11-22T10:02:00+00:00")
            self.assertEqual(self.client.post(url, data=approval).status_code, 409)
            provider.return_value = evidence
            self.assertEqual(self.client.post(url, data=approval).status_code, 303)
            self.assertEqual(self.client.post(url, data=approval).status_code, 409)
            provider.reset_mock()
            again = app.test_client()
            with again.session_transaction() as sess, auth_db() as db:
                sess.update(hq_sid=issue_session(db,"synthetic-owner"),google_sub="synthetic-owner",csrf="test")
            page = again.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.headers["Cache-Control"], "no-store")
            self.assertIn(b"Provider-confirmed at recorded time", page.data)
            self.assertIn(b"200.00", page.data)
            provider.assert_not_called()
            provider.return_value = dict(evidence, refunded_minor=5000,
                                         updated_at="2026-11-22T10:03:00+00:00")
            self.assertEqual(self.client.post(evidence_url, data=dict(form, action="recheck",
                confirm="yes")).status_code, 200)
            self.assertIn(b"Review required", again.get(url).data)
            withdrawal = dict(form, action="withdraw", revision="1", reason="Synthetic refund",
                              confirm="yes")
            self.assertEqual(again.post(url, data=dict(withdrawal, revision="0")).status_code, 409)
            self.assertEqual(again.post(url, data=withdrawal).status_code, 303)
            self.assertIn(b"Withdrawn", again.get(url).data)
            self.assertEqual(again.post(url, data=withdrawal).status_code, 409)
            external_post.assert_not_called()
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_square_receipt_recognition").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT amount_pence FROM hq_receivable_allocations").fetchone()[0], 20000)
        after = self.client.get("/api/hq/finance/receipt-allocations").json
        self.assertFalse(after["receipts_verified"])
        self.assertIsNone(after["collected_income"])
        self.assertNotIn(b"square-recognition-test", app.test_client().get("/book").data)


    def test_provider_processing_fee_review_exact_signed_and_reversible(self):
        import re
        url = "/hq/finance/provider-confirmed-receipts"
        evidence_url = "/hq/finance/square-invoice-evidence"
        self.assertEqual(self.client.get(url).status_code, 302)
        self.login()
        payload = self.payload(method="card_reported", evidence_ref="square-fee-test")
        self.assertEqual(self.post(payload).status_code, 200)
        form = dict(allocation=payload["id"], csrf="test", action="review")
        evidence = dict(id="square-fee-test", status="COMPLETED", total_minor=20000,
                        refunded_minor=0, currency="GBP",
                        created_at="2026-10-31T10:00:00+00:00",
                        updated_at="2026-11-22T10:01:00+00:00")
        def fee(kind, amount, currency="GBP"):
            return dict(type=kind, effective_at=("2026-10-31T10:01:00Z" if kind == "INITIAL" else "2026-11-22T10:01:00Z"),
                        amount_money=dict(amount=amount, currency=currency))
        items = [fee("INITIAL", 500), fee("ADJUSTMENT", -100)]
        payment = dict(id=evidence["id"], status="COMPLETED", location_id="test-loc",
                       source_type="CARD", total_money=dict(amount=20000,currency="GBP"),
                       refunded_money=dict(amount=0,currency="GBP"),
                       created_at=evidence["created_at"], updated_at=evidence["updated_at"])
        with patch.dict(os.environ, {"HQ_SQUARE_READ_TOKEN":"test", "HQ_SQUARE_LOCATION_ID":"test-loc"}), \
                patch("hq_square_invoice_evidence.storage_ready", return_value=True), \
                patch("hq_provider_receipts.storage_ready", return_value=True), \
                patch("hq_square_invoice_evidence.inspect_exact_payment", return_value=evidence) as provider, \
                patch("hq_square_payments.requests.get") as get, \
                patch("requests.post") as external_post:
            def result(rows):
                get.return_value.json.return_value = {"payment": dict(payment, processing_fee=rows)}
            def manifest(page):
                self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
                return re.search(rb'name="manifest" value="([0-9a-f]{64})"', page.data)[1].decode()
            initial = manifest(self.client.post(evidence_url, data=form))
            self.assertEqual(self.client.post(evidence_url, data=dict(form, action="save",
                manifest=initial, confirm="yes")).status_code, 200)
            f = dict(form, action="fee-preview")
            self.assertEqual(self.client.post(url, data=f).status_code, 409)
            get.assert_not_called()
            receipt = manifest(self.client.post(url, data=dict(form, action="preview")))
            self.assertEqual(self.client.post(url, data=dict(form, action="save",
                manifest=receipt, confirm="yes")).status_code, 303)
            self.assertEqual(self.client.get(url).status_code, 200)
            no_fees = self.client.get("/hq/finance/provider-receipt-month?month=2026-10")
            self.assertIn("Recognised receipts subtotal: <strong>£200.00".encode(), no_fees.data)
            self.assertIn(b"Known processing fees subtotal: <strong>Unknown", no_fees.data)
            self.assertIn(b"1 with unknown fees", no_fees.data)
            self.assertEqual(self.client.post(url, data=dict(f, csrf="bad")).status_code, 403)
            get.assert_not_called()
            for invalid in (None, [], [fee("INITIAL", True)], [fee("INITIAL", 500, "USD")],
                            [fee("INITIAL", -100)], [fee("ADJUSTMENT", 100)],
                            [fee("INITIAL", 500), fee("INITIAL", 500)],
                            [fee("INITIAL", 500), fee("ADJUSTMENT", -600)]):
                result(invalid)
                self.assertEqual(self.client.post(url, data=f).status_code, 400)
            result(items)
            preview = self.client.post(url, data=f)
            exact = manifest(preview)
            self.assertIn("£4.00".encode(), preview.data)
            self.assertIn("£196.00".encode(), preview.data)
            approval = dict(f, action="fee-save", manifest=exact, confirm="yes")
            self.assertEqual(self.client.post(url, data=dict(approval, confirm="")).status_code, 400)
            result([fee("INITIAL", 500)])
            self.assertEqual(self.client.post(url, data=approval).status_code, 409)
            result(list(reversed(items)))
            self.assertEqual(self.client.post(url, data=approval).status_code, 303)
            self.assertEqual(self.client.post(url, data=approval).status_code, 409)
            get.reset_mock()
            again = app.test_client()
            with again.session_transaction() as sess, auth_db() as db:
                sess.update(hq_sid=issue_session(db,"synthetic-owner"), google_sub="synthetic-owner",csrf="test")
            page = again.get(url)
            self.assertIn(b"Provider fee evidence recorded at review time", page.data)
            self.assertIn("£4.00".encode(), page.data)
            get.assert_not_called()

            month_url = "/hq/finance/provider-receipt-month"
            self.assertEqual(app.test_client().get(month_url).status_code, 302)
            report = again.get(month_url+"?month=2026-10")
            self.assertEqual(report.status_code, 200)
            self.assertEqual(report.headers["Cache-Control"], "no-store")
            self.assertIn("Recognised receipts subtotal: <strong>£200.00".encode(), report.data)
            self.assertIn("Known processing fees subtotal: <strong>£4.00".encode(), report.data)
            self.assertIn("<strong>£196.00".encode(), report.data)
            self.assertIn(b"1 included", report.data)
            self.assertIn(b"0 with unknown fees", report.data)
            self.assertIn("Signed known fee subtotal: <strong>£5.00".encode(), report.data)
            adjustment = again.get(month_url+"?month=2026-11")
            self.assertEqual(adjustment.status_code, 200)
            self.assertIn("Recognised receipts subtotal: <strong>Unknown".encode(), adjustment.data)
            self.assertIn("Signed known fee subtotal: <strong>£-1.00".encode(), adjustment.data)
            self.assertIn(b"ADJUSTMENT", adjustment.data)
            self.assertIn(b"Payment 2026-10-31", adjustment.data)
            self.assertIn(b"No retained classifications", again.get(month_url+"?month=2026-09").data)
            self.assertEqual(again.get(month_url+"?month=2026-13").status_code, 409)
            with connect() as db:
                from hq_provider_receipts import monthly_receipt_report
                snapshot = monthly_receipt_report(db, "2026-10")
                self.assertEqual((snapshot["gross"], snapshot["known_fees"], snapshot["net"]), (20000, 400, 19600))
            get.assert_not_called()

            settlement = "/hq/finance/settlement-review"
            entry = dict(ref="tx-fee-crosscheck", pence=19600, day="2026-11-23",
                         evidence="a"*64, legacy_evidence="b"*64)
            settle = dict(allocation=payload["id"], csrf="test", action="preview",
                          source=entry["ref"], fee="4.00", reference="Synthetic statement",
                          operation=uuid.uuid4().hex)
            with patch("hq_settlement_review.storage_ready", return_value=True), \
                    patch("hq_settlement_review.current_entry", return_value=entry) as source:
                source.return_value = dict(entry, pence=19500)
                mismatch = again.post(settlement, data=dict(settle, fee="5.00"))
                self.assertEqual(mismatch.status_code, 409)
                self.assertIn(b"differs from retained provider fees", mismatch.data)
                source.return_value = entry
                compared = again.post(settlement, data=settle)
                stamp = manifest(compared)
                self.assertIn(b"All retained provider fees agree", compared.data)
                self.assertEqual(again.post(settlement, data=dict(settle, action="save",
                    confirm="yes", manifest=stamp)).status_code, 303)
                self.assertIn(b"All retained provider fees agree", again.get(settlement).data)
                with connect() as db:
                    from hq_settlement_review import provider_fee_comparison
                    members = [dict(allocation_id=payload["id"], payment_id=evidence["id"], gross=20000),
                               dict(allocation_id="f"*32, payment_id="other", gross=10000)]
                    compared = provider_fee_comparison(db, members, 700)
                    self.assertEqual(compared["missing"], 1)
                    self.assertEqual(compared["total_pence"], 400)
                    self.assertEqual(len(compared["known"]), 1)
                    import json
                    saved = json.loads(db.execute("SELECT evidence FROM hq_square_settlement_reviews").fetchone()[0])
                    self.assertEqual(saved["provider_fees"]["total_pence"], 400)
                    self.assertEqual(saved["provider_fees"]["known"][0]["revision"], 1)
            get.assert_not_called()
            provider.return_value = dict(evidence, refunded_minor=5000,
                                         updated_at="2026-11-22T10:03:00+00:00")
            self.assertEqual(self.client.post(evidence_url, data=dict(form, action="recheck",
                confirm="yes")).status_code, 200)
            self.assertIn(b"Fee review required", again.get(url).data)
            stale_report = again.get(month_url+"?month=2026-10")
            self.assertIn(b"Recognised receipts subtotal: <strong>Unknown", stale_report.data)
            self.assertIn(b"1 withdrawn or require review", stale_report.data)
            self.assertIn(b"Signed known fee subtotal: <strong>Unknown", again.get(month_url+"?month=2026-11").data)
            with connect() as db:
                from hq_settlement_review import provider_fee_comparison
                with self.assertRaises(ValueError):
                    provider_fee_comparison(db, members[:1], 400)
            undo = dict(f, action="fee-withdraw", revision="1", reason="Synthetic correction",confirm="yes")
            self.assertEqual(again.post(url, data=dict(undo, revision="0")).status_code, 409)
            self.assertEqual(again.post(url, data=undo).status_code, 303)
            self.assertEqual(again.post(url, data=undo).status_code, 409)
            self.assertIn(b"Fee review withdrawn", again.get(url).data)
            with connect() as db:
                unknown = provider_fee_comparison(db, members[:1], 400)
                self.assertEqual(unknown["missing"], 1)
                self.assertIsNone(unknown["total_pence"])
            external_post.assert_not_called()
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_square_fee_reviews").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT amount_pence FROM hq_receivable_allocations").fetchone()[0], 20000)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocations").fetchone()[0], 1)
        after = self.client.get("/api/hq/finance/receipt-allocations").json
        self.assertFalse(after["receipts_verified"])
        self.assertIsNone(after["collected_income"])
        self.assertNotIn(b"square-fee-test", app.test_client().get("/book").data)

    def test_square_evidence_rejects_duplicate_reference_wrong_amount_and_noncard(self):
        self.login()
        url = "/hq/finance/square-invoice-evidence"
        bank = self.payload(amount_pence=100, evidence_ref="bank-test")
        card = self.payload(method="card_reported", amount_pence=100, evidence_ref="card-test")
        other = self.payload(method="card_reported", amount_pence=100, evidence_ref="card-test")
        self.assertEqual(self.post(bank).status_code, 200)
        self.assertEqual(self.post(card).status_code, 200)
        form = dict(allocation=card["id"], csrf="test", action="review")
        evidence = dict(id="card-test", status="COMPLETED", total_minor=99, refunded_minor=0, currency="GBP")
        with patch.dict(os.environ, {"HQ_SQUARE_READ_TOKEN":"test", "HQ_SQUARE_LOCATION_ID":"test-loc"}), \
                patch("hq_square_invoice_evidence.storage_ready", return_value=True), \
                patch("hq_square_invoice_evidence.inspect_exact_payment", return_value=evidence) as provider:
            self.assertEqual(self.client.post(url, data=dict(form, allocation=bank["id"])).status_code, 400)
            provider.assert_not_called()
            self.assertEqual(self.client.post(url, data=form).status_code, 400)
            self.assertEqual(self.post(other).status_code, 200)
            self.assertEqual(self.client.post(url, data=form).status_code, 409)
            self.assertEqual(provider.call_count, 1)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_square_invoice_observations").fetchone()[0], 0)


    def test_square_refund_recheck_retains_changed_evidence_without_cash_changes(self):
        import re
        payload = self.payload(method="card_reported", evidence_ref="square-refund-test")
        url = "/hq/finance/square-invoice-evidence"
        form = dict(allocation=payload["id"], csrf="test", action="review")
        check = dict(form, action="recheck", confirm="yes")
        initial = dict(id="square-refund-test", status="COMPLETED", total_minor=20000,
                       refunded_minor=0, currency="GBP",
                       created_at="2026-11-22T10:00:00+00:00",
                       updated_at="2026-11-22T10:01:00+00:00")
        with patch.dict(os.environ, {"HQ_SQUARE_READ_TOKEN":"test", "HQ_SQUARE_LOCATION_ID":"test-loc"}), \
                patch("hq_square_invoice_evidence.storage_ready", return_value=True), \
                patch("hq_square_invoice_evidence.inspect_exact_payment", return_value=initial) as provider:
            self.assertEqual(self.client.post(url, data=check).status_code, 302)
            provider.assert_not_called()
            self.login()
            self.assertEqual(self.post(payload).status_code, 200)
            self.assertEqual(self.client.post(url, data=check).status_code, 400)
            provider.assert_not_called()
            review = self.client.post(url, data=form)
            self.assertEqual(review.status_code, 200)
            manifest = re.search(rb'name="manifest" value="([0-9a-f]{64})"', review.data)[1].decode()
            self.assertEqual(self.client.post(url, data=dict(form, action="save",
                manifest=manifest, confirm="yes")).status_code, 200)
            provider.reset_mock()
            self.assertEqual(self.client.post(url, data=dict(check, csrf="bad")).status_code, 403)
            self.assertEqual(self.client.post(url, data=dict(check, confirm="")).status_code, 400)
            with patch.dict(os.environ, {"HQ_SQUARE_LOCATION_ID":"other-location"}):
                self.assertEqual(self.client.post(url, data=check).status_code, 409)
            provider.assert_not_called()
            self.assertEqual(self.client.post(url, data=check).status_code, 200)
            provider.return_value = dict(initial, refunded_minor=5000,
                                         updated_at="2026-11-22T10:02:00+00:00")
            result = self.client.post(url, data=check)
            self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
            self.assertIn(b"Review required", result.data)
            self.assertIn("£50.00".encode(), result.data)
            self.assertEqual(self.client.post(url, data=check).status_code, 200)
            provider.return_value = initial
            self.assertEqual(self.client.post(url, data=check).status_code, 409)
            provider.return_value = dict(initial, refunded_minor=None,
                                         updated_at="2026-11-22T10:03:00+00:00")
            self.assertEqual(self.client.post(url, data=check).status_code, 200)
            provider.reset_mock()
            page = self.client.get(url+"?allocation="+payload["id"])
            self.assertIn(b"unknown (not zero)", page.data)
            self.assertIn("£50.00".encode(), page.data)
            provider.assert_not_called()
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_square_invoice_observations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_square_invoice_rechecks").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT amount_pence FROM hq_receivable_allocations").fetchone()[0], 20000)
        after = self.client.get("/api/hq/finance/receipt-allocations").json
        self.assertFalse(after["receipts_verified"])
        self.assertFalse(after["outstanding_verified"])
        self.assertIsNone(after["collected_income"])


    def test_square_full_refund_approval_reverses_only_reported_allocation(self):
        import re
        payload = self.payload(method="card_reported", evidence_ref="square-full-refund")
        url = "/hq/finance/square-invoice-evidence"
        form = dict(allocation=payload["id"], csrf="test", action="review")
        check = dict(form, action="recheck", confirm="yes")
        review = dict(form, action="refund-review")
        initial = dict(id="square-full-refund", status="COMPLETED", total_minor=20000,
                       refunded_minor=0, currency="GBP",
                       created_at="2026-11-22T10:00:00+00:00",
                       updated_at="2026-11-22T10:01:00+00:00")
        partial = dict(initial, refunded_minor=5000, updated_at="2026-11-22T10:02:00+00:00")
        full = dict(initial, refunded_minor=20000, updated_at="2026-11-22T10:03:00+00:00")
        with patch.dict(os.environ, {"HQ_SQUARE_READ_TOKEN":"test", "HQ_SQUARE_LOCATION_ID":"test-loc"}), \
                patch("hq_square_invoice_evidence.storage_ready", return_value=True), \
                patch("hq_square_invoice_evidence.inspect_exact_payment", return_value=initial) as provider, \
                patch("requests.post") as external_post:
            self.assertEqual(self.client.post(url, data=review).status_code, 302)
            provider.assert_not_called()
            self.login()
            self.assertEqual(self.post(payload).status_code, 200)
            page = self.client.post(url, data=form)
            manifest = re.search(rb'name="manifest" value="([0-9a-f]{64})"', page.data)[1].decode()
            self.assertEqual(self.client.post(url, data=dict(form, action="save", manifest=manifest, confirm="yes")).status_code, 200)
            provider.return_value = partial
            self.assertEqual(self.client.post(url, data=check).status_code, 200)
            self.assertEqual(self.client.post(url, data=review).status_code, 400)
            provider.return_value = full
            self.assertEqual(self.client.post(url, data=check).status_code, 200)
            page = self.client.post(url, data=review)
            self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
            self.assertIn(b"Approve exact full refund correction", page.data)
            manifest = re.search(rb'name="action" value="refund-save"><input type="hidden" name="manifest" value="([0-9a-f]{64})"', page.data)[1].decode()
            approval = dict(form, action="refund-save", confirm="yes", manifest=manifest)
            self.assertEqual(self.client.post(url, data=dict(approval, confirm="")).status_code, 400)
            self.assertEqual(self.client.post(url, data=dict(approval, csrf="bad")).status_code, 403)
            provider.return_value = dict(full, refunded_minor=None)
            self.assertEqual(self.client.post(url, data=approval).status_code, 409)
            before = self.client.get("/api/hq/finance/receipt-allocations").json
            self.assertEqual(before["invoices"][0]["owner_reported_allocated_pence"], 20000)
            provider.return_value = full
            with patch.dict(os.environ, {"HQ_SQUARE_LOCATION_ID":"other"}):
                self.assertEqual(self.client.post(url, data=approval).status_code, 409)
            page = self.client.post(url, data=approval)
            self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
            self.assertIn(b"Full refund correction retained", page.data)
            provider.reset_mock()
            self.assertEqual(self.client.post(url, data=approval).status_code, 200)
            self.assertIn(b"Full refund correction retained", self.client.get(url+"?allocation="+payload["id"]).data)
            provider.assert_not_called()
            external_post.assert_not_called()
        after = self.client.get("/api/hq/finance/receipt-allocations").json
        self.assertEqual(after["invoices"][0]["amount_pence"], 35000)
        self.assertEqual(after["invoices"][0]["owner_reported_allocated_pence"], 0)
        self.assertEqual(after["invoices"][0]["unallocated_owner_reported_pence"], 35000)
        self.assertFalse(after["receipts_verified"])
        self.assertFalse(after["outstanding_verified"])
        self.assertIsNone(after["collected_income"])
        self.assertEqual(self.post(self.payload(amount_pence=100, evidence_ref="square-full-refund")).status_code, 400)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_square_full_refund_corrections").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocation_events").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT amount_pence,status FROM hq_receivable_allocations").fetchone()[:], (20000,"reversed"))

    def test_square_partial_refunds_reduce_only_increment_and_allow_final_full_refund(self):
        import re
        self.login()
        payload = self.payload(method="card_reported", evidence_ref="square-partial-refund")
        self.assertEqual(self.post(payload).status_code, 200)
        url = "/hq/finance/square-invoice-evidence"
        form = dict(allocation=payload["id"], csrf="test", action="review")
        check = dict(form, action="recheck", confirm="yes")
        initial = dict(id="square-partial-refund", status="COMPLETED", total_minor=20000,
                       refunded_minor=0, currency="GBP",
                       created_at="2026-11-22T10:00:00+00:00",
                       updated_at="2026-11-22T10:01:00+00:00")
        def approval(action, page):
            self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
            manifest = re.search(rb'name="manifest" value="([0-9a-f]{64})"', page.data)[1].decode()
            return dict(form, action=action, confirm="yes", manifest=manifest)
        def allocated():
            data = self.client.get("/api/hq/finance/receipt-allocations").json
            self.assertFalse(data["receipts_verified"])
            self.assertFalse(data["outstanding_verified"])
            self.assertIsNone(data["collected_income"])
            self.assertEqual(data["invoices"][0]["amount_pence"], 35000)
            return data["invoices"][0]["owner_reported_allocated_pence"]
        with patch.dict(os.environ, {"HQ_SQUARE_READ_TOKEN":"test", "HQ_SQUARE_LOCATION_ID":"test-loc"}), \
                patch("hq_square_invoice_evidence.storage_ready", return_value=True), \
                patch("hq_square_invoice_evidence.inspect_exact_payment", return_value=initial) as provider, \
                patch("requests.post") as external_post:
            self.assertEqual(self.client.post(url, data=approval("save", self.client.post(url, data=form))).status_code, 200)
            self.assertEqual(allocated(), 20000)
            first = dict(initial, refunded_minor=5000, updated_at="2026-11-22T10:02:00+00:00")
            provider.return_value = first
            self.assertEqual(self.client.post(url, data=check).status_code, 200)
            review = dict(form, action="partial-review")
            page = self.client.post(url, data=review)
            self.assertIn(b"Approve exact partial refund correction", page.data)
            approved = approval("partial-save", page)
            self.assertEqual(self.client.post(url, data=dict(approved, confirm="")).status_code, 400)
            self.assertEqual(self.client.post(url, data=dict(approved, csrf="bad")).status_code, 403)
            provider.return_value = dict(first, refunded_minor=None)
            self.assertEqual(self.client.post(url, data=approved).status_code, 409)
            self.assertEqual(allocated(), 20000)
            provider.return_value = first
            self.assertEqual(self.client.post(url, data=approved).status_code, 200)
            self.assertEqual(allocated(), 15000)
            provider.reset_mock()
            self.assertEqual(self.client.post(url, data=approved).status_code, 200)
            provider.assert_not_called()
            self.assertIn(b"Approved partial refund corrections", self.client.get(url+"?allocation="+payload["id"]).data)
            self.assertNotEqual(self.client.post(url, data=review).status_code, 200)
            second = dict(initial, refunded_minor=8000, updated_at="2026-11-22T10:03:00+00:00")
            provider.return_value = second
            self.assertEqual(self.client.post(url, data=check).status_code, 200)
            approved2 = approval("partial-save", self.client.post(url, data=review))
            self.assertEqual(self.client.post(url, data=approved2).status_code, 200)
            self.assertEqual(allocated(), 12000)
            self.assertEqual(self.client.post(url, data=approved).status_code, 200)
            self.assertEqual(allocated(), 12000)
            full = dict(initial, refunded_minor=20000, updated_at="2026-11-22T10:04:00+00:00")
            provider.return_value = full
            self.assertEqual(self.client.post(url, data=check).status_code, 200)
            final = approval("refund-save", self.client.post(url, data=dict(form, action="refund-review")))
            self.assertEqual(self.client.post(url, data=final).status_code, 200)
            self.assertEqual(allocated(), 0)
            external_post.assert_not_called()
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocation_events").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_square_partial_refund_corrections").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_square_full_refund_corrections").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT amount_pence,status FROM hq_receivable_allocations").fetchone()[:], (12000,"reversed"))




    def test_dna_payout_owner_preview_save_replay_and_conflicts(self):
        import re
        from hq_monzo_invoice_matches import require_source_not_settled, Conflict
        from hq_settlement_review import check_source
        url = "/hq/finance/dna-payout-review"
        self.assertEqual(self.client.get(url).status_code, 302)
        self.login()
        entry = dict(ref="tx-dna-synthetic", pence=19500, day="2026-11-23",
                     evidence="a"*64, legacy_evidence="b"*64)
        form = dict(source=entry["ref"], payout="DNA-Test-001", gross="200.00",
                    fee="5.00", reference="Synthetic independent payout statement",
                    operation=uuid.uuid4().hex, action="preview", csrf="test")
        with patch("hq_settlement_review.storage_ready", return_value=True), \
                patch("hq_settlement_review.current_entry", return_value=entry) as source, \
                patch("requests.post") as external_post:
            self.assertEqual(self.client.get(url).status_code, 200)
            source.assert_not_called()
            self.assertEqual(self.client.post(url, data=dict(form, csrf="bad")).status_code, 403)
            self.assertEqual(self.client.post(url, data=dict(form, fee="4")).status_code, 409)
            self.assertEqual(self.client.post(url, data=dict(form, fee="")).status_code, 409)
            page = self.client.post(url, data=form)
            self.assertEqual(page.status_code, 200, page.data)
            self.assertIn("£195.00".encode(), page.data)
            stamp = re.search(rb'name="manifest" value="([0-9a-f]{64})"', page.data)[1].decode()
            approval = dict(form, action="save", confirm="yes", manifest=stamp)
            self.assertEqual(self.client.post(url, data=dict(approval, confirm="")).status_code, 409)
            source.return_value = dict(entry, evidence="c"*64)
            self.assertEqual(self.client.post(url, data=approval).status_code, 409)
            source.return_value = entry
            self.assertEqual(self.client.post(url, data=approval).status_code, 303)
            self.assertEqual(self.client.post(url, data=approval).status_code, 303)
            self.assertEqual(self.client.post(url, data=dict(approval, reference="Other")).status_code, 409)
            self.assertEqual(self.client.post(url, data=form).status_code, 409)
            self.assertIn(b"DNA-Test-001", self.client.get(url).data)
            with connect() as db:
                with self.assertRaisesRegex(Conflict, "active DNA settlement"):
                    require_source_not_settled(db, entry["ref"])
                with self.assertRaisesRegex(ValueError, "DNA payout"):
                    check_source(db, entry, [])
                require_source_not_settled(db, "unrelated")
                event = db.execute("SELECT id FROM hq_dna_payout_reviews").fetchone()[0]
            other = dict(entry, ref="tx-dna-other")
            source.return_value = other
            self.assertEqual(self.client.post(url, data=dict(form, source=other["ref"],
                payout="dna-test-001")).status_code, 409)
            source.return_value = entry
            undo = dict(csrf="test", action="withdraw", confirm="yes",
                        event=str(event), operation=uuid.uuid4().hex)
            self.assertEqual(self.client.post(url, data=undo).status_code, 303)
            self.assertEqual(self.client.post(url, data=undo).status_code, 303)
            self.assertEqual(self.client.post(url, data=approval).status_code, 409)
            with connect() as db:
                require_source_not_settled(db, entry["ref"])
                db.execute("""INSERT INTO hq_square_batch_reviews
                    (source_id,manifest,state,evidence,actor,created,operation)
                    VALUES(?,?,'proposed',?,?,?,?)""",
                    (entry["ref"],"a"*64,'{"members":[]}',"synthetic","2026-11-23",uuid.uuid4().hex))
            self.assertEqual(self.client.post(url, data=form).status_code, 409)
            external_post.assert_not_called()
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_dna_payout_reviews").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_receivable_allocations").fetchone()[0], 0)
        after = self.client.get("/api/hq/finance/receipt-allocations").json
        self.assertFalse(after["receipts_verified"])
        self.assertIsNone(after["collected_income"])


    def test_single_settlement_proposal_is_private_exact_and_reversible(self):
        import re
        url = "/hq/finance/settlement-review"
        self.assertEqual(self.client.get(url).status_code, 302)
        self.login()
        payload = self.payload(method="card_reported", evidence_ref="square-net-test")
        self.assertEqual(self.post(payload).status_code, 200)
        square = "/hq/finance/square-invoice-evidence"
        form = dict(allocation=payload["id"], csrf="test", action="review")
        evidence = dict(id="square-net-test", status="COMPLETED", total_minor=20000,
                        refunded_minor=0, currency="GBP",
                        created_at="2026-11-22T10:00:00+00:00",
                        updated_at="2026-11-22T10:01:00+00:00")
        with patch.dict(os.environ, {"HQ_SQUARE_READ_TOKEN":"test", "HQ_SQUARE_LOCATION_ID":"test-loc"}), \
                patch("hq_square_invoice_evidence.storage_ready", return_value=True), \
                patch("hq_square_invoice_evidence.inspect_exact_payment", return_value=evidence):
            review = self.client.post(square, data=form)
            manifest = re.search(rb'name="manifest" value="([0-9a-f]{64})"', review.data)[1].decode()
            self.assertEqual(self.client.post(square, data=dict(form, action="save",
                manifest=manifest, confirm="yes")).status_code, 200)
        entry = dict(ref="tx-synthetic", pence=19500, day="2026-11-23",
                     evidence="a"*64, legacy_evidence="b"*64)
        form = dict(allocation=payload["id"], csrf="test", action="preview",
                    source=entry["ref"], fee="5.00", reference="Synthetic payout statement",
                    operation=uuid.uuid4().hex)
        with patch("hq_settlement_review.storage_ready", return_value=True), \
                patch("hq_settlement_review.current_entry", return_value=entry) as source, \
                patch("requests.post") as external_post:
            self.assertEqual(self.client.get(url).status_code, 200)
            source.assert_not_called()
            self.assertEqual(self.client.post(url, data=dict(form, csrf="bad")).status_code, 403)
            self.assertEqual(self.client.post(url, data=dict(form, fee="4")).status_code, 409)
            review = self.client.post(url, data=form)
            self.assertEqual(review.status_code, 200, review.get_data(as_text=True))
            self.assertIn("£195.00".encode(), review.data)
            manifest = re.search(rb'name="manifest" value="([0-9a-f]{64})"', review.data)[1].decode()
            approval = dict(form, action="save", confirm="yes", manifest=manifest)
            self.assertEqual(self.client.post(url, data=dict(approval, confirm="")).status_code, 409)
            source.return_value = dict(entry, evidence="c"*64)
            self.assertEqual(self.client.post(url, data=approval).status_code, 409)
            source.return_value = entry
            self.assertEqual(self.client.post(url, data=approval).status_code, 303)
            self.assertEqual(self.client.post(url, data=approval).status_code, 303)
            # A replay cannot claim success with changed evidence or source data.
            self.assertEqual(self.client.post(url, data=dict(approval, fee="4.00")).status_code, 409)
            self.assertEqual(self.client.post(url, data=dict(approval, reference="Different statement")).status_code, 409)
            source.return_value = dict(entry, evidence="d"*64)
            self.assertEqual(self.client.post(url, data=approval).status_code, 409)
            source.return_value = entry
            self.assertEqual(self.client.post(url, data=form).status_code, 409)
            self.assertIn(b"Synthetic payout statement", self.client.get(url).data)
            with connect() as db:
                event = db.execute("SELECT id FROM hq_square_settlement_reviews").fetchone()[0]
            undo = dict(allocation=payload["id"], csrf="test", action="withdraw",
                        confirm="yes", event=str(event), operation=uuid.uuid4().hex)
            self.assertEqual(self.client.post(url, data=undo).status_code, 303)
            self.assertEqual(self.client.post(url, data=undo).status_code, 303)
            self.assertEqual(self.client.post(url, data=form).status_code, 200)
            external_post.assert_not_called()
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_square_settlement_reviews").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT amount_pence,status FROM hq_receivable_allocations").fetchone()[:],
                             (20000,"reported"))
        after = self.client.get("/api/hq/finance/receipt-allocations").json
        self.assertFalse(after["receipts_verified"])
        self.assertIsNone(after["collected_income"])




    def test_direct_monzo_match_rejects_active_settlement_source_and_allows_withdrawal(self):
        from hq_monzo_invoice_matches import require_source_not_settled, Conflict, tables
        from hq_settlement_review import tables as settlement_tables
        entry = dict(ref="tx-conflict-synthetic", pence=1000, day="2026-11-23",
                     evidence="a"*64, legacy_evidence="b"*64)
        self.login()
        url = "/hq/finance/monzo-invoice-match"
        form = dict(ref=entry["ref"], invoice_ref=self.invoice["invoice_ref"],
                    csrf="test", action="review", candidate_amount="10")
        with connect() as db:
            tables(db)
            # Before settlement schema exists, existing source checks still work.
            require_source_not_settled(db, entry["ref"])
            settlement_tables(db)
        with patch("hq_monzo_invoice_matches.storage_ready", return_value=True), \
                patch("hq_monzo_invoice_matches.current_entry", return_value=entry):
            for table, key in (("hq_square_settlement_reviews", "allocation_id"),
                               ("hq_square_batch_reviews", None)):
                with connect() as db:
                    columns = "source_id,manifest,state,evidence,actor,created,operation"
                    values = [entry["ref"], "a"*64, "proposed", "{}", "synthetic", "2026-11-23", uuid.uuid4().hex]
                    if key:
                        columns = key + "," + columns
                        values.insert(0, "a"*32)
                    sql = f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in values)})"
                    db.execute(sql, values)
                    with self.assertRaises(Conflict):
                        require_source_not_settled(db, entry["ref"])
                    require_source_not_settled(db, "unrelated-source")
                response = self.client.post(url, data=form)
                self.assertEqual(response.status_code, 409)
                self.assertIn(b"active Square settlement proposal", response.data)
                # Also guard the report path even if an old conflicting candidate exists.
                from hq_monzo_invoice_matches import report_preview
                with connect() as db:
                    with self.assertRaisesRegex(Conflict, "active Square settlement"):
                        report_preview(db, entry, self.invoice["invoice_ref"], 1, "other-ref")
                    values[-1] = uuid.uuid4().hex
                    values[3 if key else 2] = "withdrawn"
                    db.execute(sql, values)
                    require_source_not_settled(db, entry["ref"])
                # The same source may be reviewed again after withdrawal.
                from hq_monzo_invoice_matches import review
                with connect() as db:
                    self.assertEqual(review(db, entry, self.invoice["invoice_ref"], 1000)["amount"], 1000)


    def test_combined_settlement_proposal_exact_pence_replay_and_cross_conflicts(self):
        import re
        url = "/hq/finance/settlement-batches"
        single = "/hq/finance/settlement-review"
        self.assertEqual(self.client.get(url).status_code, 302)
        self.login()
        payments = [self.payload(method="card_reported", amount_pence=n,
                    evidence_ref="square-batch-"+str(n)) for n in (20000,10000)]
        square = "/hq/finance/square-invoice-evidence"
        with patch.dict(os.environ, {"HQ_SQUARE_READ_TOKEN":"test", "HQ_SQUARE_LOCATION_ID":"test-loc"}), \
                patch("hq_square_invoice_evidence.storage_ready", return_value=True), \
                patch("hq_square_invoice_evidence.inspect_exact_payment") as provider:
            for payment in payments:
                self.assertEqual(self.post(payment).status_code, 200)
                provider.return_value = dict(id=payment["evidence_ref"], status="COMPLETED",
                    total_minor=payment["amount_pence"], refunded_minor=0, currency="GBP",
                    created_at="2026-11-22T10:00:00+00:00", updated_at="2026-11-22T10:01:00+00:00")
                form = dict(allocation=payment["id"], csrf="test", action="review")
                review = self.client.post(square, data=form)
                self.assertEqual(review.status_code, 200, review.get_data(as_text=True))
                manifest = re.search(rb'name="manifest" value="([0-9a-f]{64})"', review.data)[1].decode()
                self.assertEqual(self.client.post(square, data=dict(form, action="save",
                    manifest=manifest, confirm="yes")).status_code, 200)
        entry = dict(ref="tx-batch-synthetic", pence=29300, day="2026-11-23",
                     evidence="a"*64, legacy_evidence="b"*64)
        form = dict(allocations="\n".join(p["id"] for p in payments), csrf="test",
                    action="preview", source=entry["ref"], fee="7.00",
                    reference="Synthetic combined statement", operation=uuid.uuid4().hex)
        with patch("hq_settlement_review.storage_ready", return_value=True), \
                patch("hq_settlement_review.current_entry", return_value=entry) as source, \
                patch("requests.post") as external_post:
            self.assertEqual(self.client.get(url).status_code, 200)
            source.assert_not_called()
            self.assertEqual(self.client.post(url, data=dict(form,csrf="bad")).status_code, 403)
            self.assertEqual(self.client.post(url, data=dict(form,fee="6.99")).status_code, 409)
            self.assertEqual(self.client.post(url, data=dict(form,allocations=payments[0]["id"]+" "+payments[0]["id"])).status_code, 409)
            review = self.client.post(url, data=form)
            self.assertEqual(review.status_code, 200, review.get_data(as_text=True))
            self.assertIn("£293.00".encode(), review.data)
            manifest = re.search(rb'name="manifest" value="([0-9a-f]{64})"', review.data)[1].decode()
            approval = dict(form,action="save",confirm="yes",manifest=manifest)
            self.assertEqual(self.client.post(url,data=dict(approval,confirm="")).status_code,409)
            source.return_value = dict(entry,evidence="c"*64)
            self.assertEqual(self.client.post(url,data=approval).status_code,409)
            source.return_value = entry
            self.assertEqual(self.client.post(url,data=approval).status_code,303)
            self.assertEqual(self.client.post(url,data=approval).status_code,303)
            self.assertEqual(self.client.post(url,data=dict(approval,reference="changed")).status_code,409)
            self.assertEqual(self.client.post(url,data=form).status_code,409)
            self.assertIn(b"Synthetic combined statement",self.client.get(url).data)
            single_entry = dict(entry,ref="tx-single-synthetic",pence=19500)
            source.return_value = single_entry
            one = dict(allocation=payments[0]["id"],csrf="test",action="preview",
                       source=single_entry["ref"],fee="5",reference="Other statement",
                       operation=uuid.uuid4().hex)
            self.assertEqual(self.client.post(single,data=one).status_code,409)
            with connect() as db:
                event = db.execute("SELECT id FROM hq_square_batch_reviews").fetchone()[0]
            undo = dict(action="withdraw",event=str(event),csrf="test",confirm="yes",operation=uuid.uuid4().hex)
            self.assertEqual(self.client.post(url,data=undo).status_code,303)
            self.assertEqual(self.client.post(url,data=undo).status_code,303)
            preview = self.client.post(single,data=one)
            self.assertEqual(preview.status_code,200,preview.get_data(as_text=True))
            one_manifest = re.search(rb'name="manifest" value="([0-9a-f]{64})"',preview.data)[1].decode()
            self.assertEqual(self.client.post(single,data=dict(one,action="save",confirm="yes",manifest=one_manifest)).status_code,303)
            source.return_value = entry
            self.assertEqual(self.client.post(url,data=form).status_code,409)
            external_post.assert_not_called()
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hq_square_batch_reviews").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT SUM(amount_pence) FROM hq_receivable_allocations").fetchone()[0],30000)
        after = self.client.get("/api/hq/finance/receipt-allocations").json
        self.assertFalse(after["receipts_verified"])
        self.assertIsNone(after["collected_income"])


if __name__ == "__main__":
    unittest.main()
