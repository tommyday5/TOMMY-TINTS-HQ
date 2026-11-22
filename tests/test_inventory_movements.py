"""Synthetic, owner-only stock journal, persistence and customer basket parity."""
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_commerce import (basket_quote, database, record_stock_movement, save_product)


class StockMovementTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": tmp.name, "HQ_SESSION_SECRET": "synthetic-stock-only",
            "GOOGLE_CLIENT_ID": "fixture", "GOOGLE_CLIENT_SECRET": "fixture",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.product = {
            "name": "Synthetic microfibre", "variant": "40 pack",
            "description": "Only for regression", "retail": 2999,
            "trade": 1999, "cost": 1000, "stock": 3,
            "delivery": 250, "published": 1,
        }
        self.ref = save_product(self.product)

    def op(self):
        return uuid.uuid4().hex

    def stock(self):
        with database() as db:
            return db.execute("SELECT stock,version FROM products WHERE id=?", (self.ref,)).fetchone()

    def test_receipt_usage_replay_and_basket_availability_after_reload(self):
        self.assertEqual(tuple(self.stock()), (3, 1))
        with database() as db:
            opening = db.execute("SELECT movement,after_quantity FROM commerce_stock_movements").fetchone()
            self.assertEqual(tuple(opening), ("declared_opening", 3))
        receipt = self.op()
        self.assertEqual(record_stock_movement(self.ref, receipt, 1, "received", 5,
                                               "Synthetic supplier delivery"), 2)
        self.assertEqual(record_stock_movement(self.ref, receipt, 1, "received", 5,
                                               "Synthetic supplier delivery"), 2)
        self.assertEqual(tuple(self.stock()), (8, 2))
        self.assertEqual(record_stock_movement(self.ref, self.op(), 2, "used", 4,
                                               "Synthetic workshop consumption"), 3)
        self.assertEqual(tuple(self.stock()), (4, 3))
        self.assertEqual(basket_quote([{"id": self.ref, "quantity": 4}],
                                      "collection")["total_pence"], 4 * 2999)
        with self.assertRaises(ValueError):
            basket_quote([{"id": self.ref, "quantity": 5}], "collection")
        with database() as db:
            rows = db.execute("""SELECT movement,delta,before_quantity,after_quantity
                FROM commerce_stock_movements ORDER BY version_after,id""").fetchall()
            self.assertEqual(len(rows), 3)
            self.assertEqual(sum(x["delta"] for x in rows), 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM commerce_audit WHERE action='stock_movement'").fetchone()[0], 2)

    def test_stale_duplicate_and_negative_stock_fail_closed(self):
        operation = self.op()
        record_stock_movement(self.ref, operation, 1, "used", 2, "Synthetic sale")
        with self.assertRaisesRegex(ValueError, "different stock change"):
            record_stock_movement(self.ref, operation, 1, "used", 1, "Synthetic sale")
        with self.assertRaisesRegex(ValueError, "changed elsewhere"):
            record_stock_movement(self.ref, self.op(), 1, "received", 1, "Another delivery")
        with self.assertRaisesRegex(ValueError, "Insufficient stock"):
            record_stock_movement(self.ref, self.op(), 2, "used", 2, "Too many units")
        self.assertEqual(tuple(self.stock()), (1, 2))
        with database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM commerce_stock_movements").fetchone()[0], 2)

    def test_manual_catalogue_edit_is_also_journalled_not_silent(self):
        save_product({**self.product, "id": self.ref, "version": 1, "stock": 9})
        self.assertEqual(tuple(self.stock()), (9, 2))
        with database() as db:
            event = db.execute("""SELECT movement,delta,before_quantity,after_quantity
                FROM commerce_stock_movements WHERE movement='catalogue_edit'""").fetchone()
            self.assertEqual(tuple(event), ("catalogue_edit", 6, 3, 9))
        with self.assertRaises(ValueError):
            save_product({**self.product, "id": self.ref, "version": 1, "stock": 1})

    def test_legacy_catalogue_quantity_snapshotted_not_treated_as_verified(self):
        legacy = self.op()
        with database() as db:
            db.execute("""INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                       (legacy, "Legacy", "", "", 100, None, 50, 7, 0, 1, 4))
        self.assertEqual(record_stock_movement(legacy, self.op(), 4, "correction_out", 2,
                                               "Physical stocktake correction"), 5)
        with database() as db:
            rows = db.execute("""SELECT movement,delta,before_quantity,after_quantity
                FROM commerce_stock_movements WHERE product_id=? ORDER BY version_after,id""",
                (legacy,)).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["movement"], "legacy_baseline")
            self.assertEqual(rows[1]["after_quantity"], 5)

    def test_owner_only_csrf_and_customer_live_stock(self):
        client = app.test_client()
        self.assertEqual(client.get("/hq/shop/stock").status_code, 302)
        self.assertEqual(client.post("/hq/shop/stock", data={}).status_code, 302)
        self.assertIn(b"3 available", client.get("/book/shop").data)
        with client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        csrf="synthetic-owner-csrf", google_sub="synthetic-owner")
        self.assertEqual(client.get("/hq/shop/stock").status_code, 200)
        data = {
            "product_id": self.ref, "operation_id": self.op(), "version": "1",
            "movement": "received", "quantity": "2", "note": "Test shipment",
        }
        self.assertEqual(client.post("/hq/shop/stock", data=data).status_code, 403)
        bad = client.post("/hq/shop/stock", data={**data, "csrf": "synthetic-owner-csrf",
                                                  "quantity": "-1"})
        self.assertEqual(bad.status_code, 400)
        response = client.post("/hq/shop/stock", data={**data, "csrf": "synthetic-owner-csrf"})
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True))
        self.assertIn(b"5 available", client.get("/book/shop").data)
        self.assertEqual(tuple(self.stock()), (5, 2))
        self.assertNotIn(b"Test shipment", client.get("/book/shop").data)
        self.assertNotIn(b"1000", client.get("/book/shop").data)
        self.assertIn(b"Test shipment", client.get("/hq/shop/stock").data)


if __name__ == "__main__":
    unittest.main()
