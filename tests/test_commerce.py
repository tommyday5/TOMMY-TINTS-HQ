import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_commerce import save_product, basket_quote, create_package, visit_event, database


class CommerceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, HQ_DATA_DIR=folder.name, HQ_SESSION_SECRET="test",
                         GOOGLE_CLIENT_ID="test", GOOGLE_CLIENT_SECRET="test",
                         HQ_ALLOWED_GOOGLE_EMAIL="owner@example.invalid")
        env.start()
        self.addCleanup(env.stop)
        self.product = dict(name="Fixture",variant="Pack",description="Synthetic item",retail=1500,
                            trade=900,cost=500,stock=3,delivery=300,published=1)

    def test_basket_uses_hq_prices_and_never_creates_order_or_reserves_stock(self):
        ref = save_product(self.product)
        total = basket_quote([{"id":ref,"quantity":2,"price":1,"tier":"trade"}],"delivery")
        self.assertEqual((total["subtotal_pence"], total["delivery_pence"], total["total_pence"]), (3000,300,3300))
        self.assertFalse(total["checkout_enabled"])
        self.assertNotIn("trade", str(total))
        for lines in ([{"id":ref,"quantity":4}], [{"id":ref,"quantity":True}],
                      [{"id":ref,"quantity":1},{"id":ref,"quantity":1}]):
            with self.assertRaises(ValueError): basket_quote(lines, "collection")
        with database() as db:
            self.assertEqual(db.execute("SELECT stock FROM products").fetchone()[0],3)

    def test_admin_requires_owner_and_csrf_and_public_never_exposes_trade_or_cost(self):
        save_product(self.product)
        with app.test_client() as client:
            for path in ("/hq/shop","/hq/maintenance/packages"):
                self.assertEqual(client.get(path).status_code,302)
            public = client.get("/book/shop")
            self.assertEqual(public.status_code,200)
            self.assertNotIn(b"Approved trade price",public.data)
            with client.session_transaction() as session, auth_db() as db:
                session.update(hq_sid=issue_session(db,"owner"),csrf="test")
            self.assertEqual(client.post("/hq/shop",data=self.product).status_code,403)
            self.assertEqual(client.get("/hq/shop").status_code,200)
            self.assertEqual(client.get("/hq/maintenance/packages").status_code,200)

    def test_stale_edit_and_unpublished_stock_are_rejected(self):
        ref = save_product(self.product)
        save_product({**self.product,"id":ref,"version":1,"published":0})
        with self.assertRaises(ValueError):save_product({**self.product,"id":ref,"version":1})
        with self.assertRaises(ValueError):basket_quote([{"id":ref,"quantity":1}], "collection")

    def test_concurrent_replayed_completion_deducts_once_and_corrects_auditably(self):
        ref=create_package(dict(customer_ref="synthetic",title="Fixture",terms="Fixture terms",price=5000,visits=2))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:visit_event(ref,"visit-1",-1,"Completed"),range(4)))
        self.assertEqual(sum(results),1)
        with self.assertRaises(ValueError):visit_event(ref,"visit-1",0,"Changed")
        visit_event(ref,"free-visit",0,"Complimentary")
        visit_event(ref,"visit-2",-1,"Completed")
        with self.assertRaises(ValueError):visit_event(ref,"visit-3",-1,"Completed")
        visit_event(ref,"correction-1",1,"Incorrect completion reversed")
        with database() as db:
            self.assertEqual(db.execute("SELECT SUM(delta) FROM visits WHERE package_id=?",(ref,)).fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM visits WHERE package_id=?",(ref,)).fetchone()[0],5)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM commerce_audit WHERE reference=?",(ref,)).fetchone()[0],5)
