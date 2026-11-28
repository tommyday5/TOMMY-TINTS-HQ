"""Synthetic owner-only recovery workflow tests; no live records or provider calls."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import app
from customer_request_store import connect
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_private_recovery import list_snapshots
from hq_recovery_export import decrypt_for_isolated_verification


class RecoveryWorkspaceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"HQ_DATA_DIR": tmp.name,
            "HQ_SESSION_SECRET": "synthetic-recovery-workspace-secret",
            "GOOGLE_CLIENT_ID": "synthetic", "GOOGLE_CLIENT_SECRET": "synthetic",
            "HQ_ALLOWED_GOOGLE_EMAIL": "owner@example.invalid"})
        env.start()
        self.addCleanup(env.stop)
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        with connect() as db:
            db.execute("CREATE TABLE synthetic_recovery_fixture (id INTEGER PRIMARY KEY)")
            db.execute("INSERT INTO synthetic_recovery_fixture VALUES (1)")
        self.client = app.test_client()
        self.url = "/hq/migration/recovery"

    def login(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "synthetic-owner"),
                        google_sub="synthetic-owner", csrf="recovery-csrf")

    def test_owner_can_save_reload_and_export_without_any_staged_job(self):
        form = dict(csrf="recovery-csrf", action="private_backup",
                    operation_id="a"*32, confirm_backup="yes")
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.post(self.url, data=form).status_code, 302)
        self.login()
        with patch.dict(app.extensions, {"hq_sheets_connection":
                lambda: self.fail("Recovery must not call Sheets")}):
            page = self.client.get(self.url)
            self.assertEqual(page.status_code, 200)
            self.assertIn("no-store", page.headers["Cache-Control"])
            self.assertIn(b"No saved private snapshots", page.data)
            self.assertEqual(list_snapshots(), [])
            self.assertEqual(self.client.post(self.url, data=dict(form, csrf="bad")).status_code, 403)
            self.assertEqual(self.client.post(self.url, data=dict(form, confirm_backup="")).status_code, 400)
            self.assertEqual(list_snapshots(), [])
            with patch("hq_private_recovery.MAX_BYTES", 1):
                self.assertEqual(self.client.post(self.url, data=form).status_code, 409)
            self.assertEqual(list_snapshots(), [])
            saved = self.client.post(self.url, data=form)
            self.assertEqual(saved.status_code, 200)
            self.assertIn(b"Private recovery snapshot saved and isolated restore checked", saved.data)
            self.assertIn(b"Download encrypted recovery copy", saved.data)
            self.assertEqual(self.client.post(self.url, data=form).status_code, 200)
            self.assertEqual(len(list_snapshots()), 1)
            reload = self.client.get(self.url)
            self.assertIn(("a"*32).encode(), reload.data)
            self.assertNotIn(b"Private recovery snapshot saved and isolated restore checked", reload.data)
            self.assertIn(b"Private recovery and encrypted backup",
                          self.client.get("/hq/migration/customers").data)
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                      serialization.PublicFormat.SubjectPublicKeyInfo).decode()
            export = dict(csrf="recovery-csrf", action="encrypted_export",
                          snapshot_id="a"*32, public_key=pem, confirm_export="yes")
            self.assertEqual(self.client.post(self.url, data=dict(export, csrf="bad")).status_code, 403)
            self.assertEqual(self.client.post(self.url, data=dict(export, confirm_export="")).status_code, 400)
            self.assertEqual(self.client.post(self.url, data=dict(export, snapshot_id="../bad")).status_code, 400)
            encrypted = self.client.post(self.url, data=export)
            self.assertEqual(encrypted.status_code, 200)
            self.assertIn("attachment", encrypted.headers["Content-Disposition"])
            self.assertIn("no-store", encrypted.headers["Cache-Control"])
            data = decrypt_for_isolated_verification(encrypted.data, key)
            self.assertTrue(data.startswith(b"SQLite format 3\x00"))
            self.assertEqual(len(list_snapshots()), 1)
            with connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM synthetic_recovery_fixture").fetchone()[0], 1)
