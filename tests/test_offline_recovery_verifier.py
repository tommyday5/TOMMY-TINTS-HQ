import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import hq_private_recovery as recovery
from hq_recovery_export import export_snapshot
from hq_verify_recovery import main, verify_files


class OfflineRecoveryVerifierTests(unittest.TestCase):
    def test_authentication_integrity_and_no_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "customer_requests.sqlite3"
            with contextlib.closing(sqlite3.connect(source)) as db, db:
                db.execute("CREATE TABLE sample (value TEXT)")
                db.execute("INSERT INTO sample VALUES ('private synthetic fixture')")
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            password = b"synthetic-only-passphrase"
            pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
            key_path = root / "private.pem"
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(password)))
            envelope_path = root / "recovery.tthq"
            with patch.dict(os.environ, {"HQ_DATA_DIR": tmp}):
                snapshot = recovery.create_snapshot("a"*32)
                envelope = export_snapshot(snapshot, pem)
            envelope_path.write_bytes(envelope)
            snapshot_path = root / "private_recovery_snapshots" / ("a"*32 + ".sqlite3")
            snapshot_hash = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
            # Offline verifier works even without access to the live source.
            source.unlink()
            result = verify_files(envelope_path, key_path, password)
            self.assertEqual(result["sqlite_sha256"], snapshot_hash)
            self.assertEqual(result["encrypted_sha256"], hashlib.sha256(envelope).hexdigest())
            self.assertFalse(result["live_restore_performed"])
            self.assertNotIn("private synthetic fixture", json.dumps(result))
            self.assertFalse(source.exists())
            with self.assertRaises(ValueError):
                verify_files(envelope_path, key_path, b"wrong")
            out = io.StringIO()
            with patch("hq_verify_recovery.getpass", return_value=password.decode()), contextlib.redirect_stdout(out):
                self.assertEqual(main([str(envelope_path), str(key_path)]), 0)
            self.assertEqual(json.loads(out.getvalue()), result)
            envelope_path.write_bytes(envelope[:-1] + bytes([envelope[-1]^1]))
            with patch("hq_verify_recovery.getpass", return_value=password.decode()), contextlib.redirect_stdout(io.StringIO()) as bad_out, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([str(envelope_path), str(key_path)]), 1)
            self.assertEqual(bad_out.getvalue(), "")
            self.assertFalse(source.exists())
            self.assertEqual(hashlib.sha256(snapshot_path.read_bytes()).hexdigest(), snapshot_hash)


if __name__ == "__main__":
    unittest.main()
