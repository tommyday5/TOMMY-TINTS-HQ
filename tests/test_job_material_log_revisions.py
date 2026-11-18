"""Owner-only material costing: additive revisions, safe retries and printable fields."""
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from job_material_store import StaleMaterialLogError, connect, read_log, write_log


class JobMaterialRevisionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = tmp.name
        env = patch.dict(os.environ, {
            "HQ_DATA_DIR": self.folder,
            "HQ_SESSION_SECRET": "material-revisions-tests",
            "GOOGLE_CLIENT_ID": "test", "GOOGLE_CLIENT_SECRET": "test",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()

    def sign_in(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(
                hq_sid=issue_session(db, "material-owner"),
                google_sub="material-owner",
                csrf="material-csrf",
            )

    @staticmethod
    def payload(cost="4.00", revision=0):
        return {
            "lines": [{"name": "Clearcoat", "cost": cost}],
            "sale": "350", "labour": "2", "actual_labour_rate": "25",
            "overhead": "15", "other_costs": "5", "minimum_margin_percent": "35",
            "description": "Hyundai bonnet repair and repaint",
            "stages": "Preparation and masking\nPrimer and paint\nFinishing",
            "expected_revision": revision,
        }

    def put(self, payload, headers=None):
        return self.client.put("/api/job-materials/J0057", json=payload,
                               headers=headers or {"X-HQ-CSRF": "material-csrf"})

    def test_owner_csrf_revision_and_printable_fields(self):
        self.assertEqual(self.client.get("/api/job-materials/J0057").status_code, 302)
        self.sign_in()
        initial = self.client.get("/api/job-materials/J0057")
        self.assertEqual(initial.json["revision"], 0)
        self.assertIsNone(initial.json["data"])
        self.assertEqual(initial.headers["Cache-Control"], "no-store")
        self.assertEqual(self.put(self.payload(), {"X-HQ-CSRF": "wrong"}).status_code, 403)
        self.assertEqual(self.put({k: v for k, v in self.payload().items() if k != "expected_revision"}).status_code, 400)
        self.assertEqual(self.put(self.payload(revision=True)).status_code, 400)
        self.assertEqual(self.put({**self.payload(), "stages": "a" * 4001}).status_code, 400)

        created = self.put(self.payload())
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json["revision"], 1)
        self.assertNotIn("expected_revision", created.json["data"])
        self.assertEqual(created.json["data"]["stages"], self.payload()["stages"])
        self.assertEqual(self.put(self.payload()).json["revision"], 1)

        stale = self.put(self.payload("8.00"))
        self.assertEqual(stale.status_code, 409)
        self.assertIn("another tab", stale.json["error"])
        self.assertEqual(read_log("material-owner", "J0057")["data"]["lines"][0]["cost"], "4.00")

        changed = self.put(self.payload("8.00", revision=1))
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json["revision"], 2)
        with connect() as db:
            rows = db.execute("SELECT revision,previous_sha256,current_sha256 FROM job_material_log_audit ORDER BY revision").fetchall()
        self.assertEqual([row[0] for row in rows], [1, 2])
        self.assertIsNone(rows[0][1])
        self.assertEqual(len(rows[1][1]), 64)
        self.assertNotEqual(rows[1][1], rows[1][2])
        printable = self.client.get("/customer-breakdown/J0057")
        self.assertEqual(printable.status_code, 200)
        html = printable.get_data(as_text=True)
        self.assertIn("Hyundai bonnet repair and repaint", html)
        self.assertIn("Preparation and masking", html)
        self.assertIn("£350.00", html)
        self.assertNotIn("Actual labour", html)

        # The page must actually send all customer-facing fields and revision.
        main_html = self.client.get("/").get_data(as_text=True)
        self.assertIn("expected_revision:currentMaterialRevision", main_html)
        self.assertIn('description:$("matDescription").value', main_html)
        self.assertIn('stages:$("matStages").value', main_html)

    def test_legacy_logs_migrate_in_place_and_are_not_overwritten_by_stale_tabs(self):
        path = Path(self.folder) / "hq_jobs.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE job_material_logs(
                owner TEXT NOT NULL,job_key TEXT NOT NULL,payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(owner,job_key))""")
            db.execute("INSERT INTO job_material_logs(owner,job_key,payload) VALUES(?,?,?)",
                       ("material-owner", "J0001", '{"lines":[],"sale":"270"}'))
        original = read_log("material-owner", "J0001")
        self.assertEqual((original["revision"], original["data"]["sale"]), (0, "270"))
        with self.assertRaises(StaleMaterialLogError):
            write_log("material-owner", "J0001", {"lines": [], "sale": "300"}, expected_revision=1)
        saved = write_log("material-owner", "J0001", {"lines": [], "sale": "280"}, expected_revision=0)
        self.assertEqual((saved["revision"], saved["data"]["sale"]), (1, "280"))
        self.assertEqual(write_log("material-owner", "J0001", saved["data"], expected_revision=0)["revision"], 1)

    def test_concurrent_edits_only_one_wins_and_owner_is_isolated(self):
        def save(amount):
            try:
                return write_log("material-owner", "J0099",
                                 {"lines": [{"name": "Basecoat", "cost": amount}]},
                                 expected_revision=0)["revision"]
            except StaleMaterialLogError:
                return "stale"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ("3.00", "4.00")))
        self.assertCountEqual(results, [1, "stale"])
        self.assertIsNone(read_log("different-owner", "J0099")["data"])
        self.assertEqual(read_log("material-owner", "J0099")["revision"], 1)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM job_material_log_audit").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
