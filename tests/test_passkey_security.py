import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url

from app import app
from hq_auth import issue_session
from hq_auth_store import auth_db
from hq_google_connections import connection, save_connection
from hq_passkeys import ORIGIN, _user_handle


class PasskeySecurityTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(root.cleanup)
        env = patch.dict(os.environ, HQ_DATA_DIR=root.name, HQ_SESSION_SECRET="test-key",
                         GOOGLE_CLIENT_ID="test", GOOGLE_CLIENT_SECRET="test",
                         HQ_ALLOWED_GOOGLE_EMAIL="owner@example.invalid")
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()
        self.headers = {"Origin": ORIGIN, "X-HQ-CSRF": "test", "X-HQ-Passkey": "1"}
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.ref = b"synthetic-credential-reference"
        self.handle = _user_handle("test-owner")
        self.rp_hash = hashlib.sha256(ORIGIN.split("://")[1].encode()).digest()

    def post(self, path, data=None):
        return self.client.post("/api/hq/passkeys/" + path, json={} if data is None else data,
                                headers=self.headers)

    def owner(self):
        with self.client.session_transaction() as sess, auth_db() as db:
            sess.update(hq_sid=issue_session(db, "test-owner"), csrf="test", google_sub="test-owner")

    def client_data(self, challenge, kind, origin=ORIGIN):
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin,
                           "crossOrigin": False}).encode()

    def enroll(self):
        self.owner()
        options = self.post("register/options").json
        self.assertEqual(options["authenticatorSelection"]["userVerification"], "required")
        point = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: point.x.to_bytes(32, "big"), -3: point.y.to_bytes(32, "big")})
        auth_data = self.rp_hash + b"\x45" + bytes(4) + bytes(16) + len(self.ref).to_bytes(2, "big") + self.ref + cose
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        result = self.post("register/verify", self.credential({
            "attestationObject": bytes_to_base64url(attestation),
            "clientDataJSON": bytes_to_base64url(self.client_data(options["challenge"], "webauthn.create")),
        }))
        self.assertEqual(result.status_code, 200, result.json)

    def credential(self, response):
        return {"type": "public-key", "id": bytes_to_base64url(self.ref),
                "rawId": bytes_to_base64url(self.ref), "response": response}

    def assertion(self, challenge, origin=ORIGIN, flags=5, rp_hash=None, handle=None, counter=1):
        client = self.client_data(challenge, "webauthn.get", origin)
        data = (rp_hash or self.rp_hash) + bytes([flags]) + counter.to_bytes(4, "big")
        signature = self.key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return self.credential({"clientDataJSON": bytes_to_base64url(client),
                                "authenticatorData": bytes_to_base64url(data),
                                "signature": bytes_to_base64url(signature),
                                "userHandle": bytes_to_base64url(handle or self.handle)})

    def test_logout_revokes_copied_cookie_and_fresh_signature_creates_session(self):
        self.enroll()
        cookie = self.client.get_cookie("session").value
        self.assertEqual(self.client.post("/logout", data={"csrf": "test"}).location, "/")
        with app.test_client() as copied:
            copied.set_cookie("session", cookie)
            self.assertEqual(copied.get("/api/hq/work-records").status_code, 302)
        challenge = self.post("login/options").json["challenge"]
        assertion = self.assertion(challenge, counter=0)
        cookie = self.client.get_cookie("session").value
        self.assertEqual(self.post("login/verify", assertion).status_code, 200)
        self.assertEqual(self.client.get("/api/hq/work-records").status_code, 200)
        self.client.set_cookie("session", cookie)
        self.assertEqual(self.post("login/verify", assertion).status_code, 403)

    def test_invalid_origin_rp_uv_owner_and_challenge_are_rejected(self):
        self.enroll()
        self.client.post("/logout", data={"csrf": "test"})
        for change in ({"origin": "https://example.invalid"}, {"flags": 1},
                       {"rp_hash": bytes(32)}, {"handle": b"wrong owner"}):
            challenge = self.post("login/options").json["challenge"]
            self.assertEqual(self.post("login/verify", self.assertion(challenge, **change)).status_code, 403)
        self.post("login/options")
        self.assertEqual(self.post("login/verify", self.assertion(bytes_to_base64url(bytes(32)))).status_code, 403)

    def test_enrolment_and_removal_require_recent_session_csrf_and_origin(self):
        self.assertEqual(self.post("register/options").status_code, 403)
        self.enroll()
        self.headers["X-HQ-CSRF"] = "wrong"
        self.assertEqual(self.post("remove", {"id": bytes_to_base64url(self.ref)}).status_code, 403)
        self.headers["X-HQ-CSRF"] = "test"
        self.headers["Origin"] = "https://example.invalid"
        self.assertEqual(self.post("register/options").status_code, 403)
        self.headers["Origin"] = ORIGIN
        with auth_db() as db:
            db.execute("UPDATE sessions SET issued=issued-400")
        self.assertEqual(self.post("register/options").status_code, 403)
        self.assertEqual(self.post("remove", {"id": bytes_to_base64url(self.ref)}).status_code, 403)
        self.owner()
        self.assertEqual(self.post("remove", {"id": bytes_to_base64url(self.ref)}).status_code, 200)
        self.client.post("/logout", data={"csrf": "test"})
        challenge = self.post("login/options").json["challenge"]
        self.assertEqual(self.post("login/verify", self.assertion(challenge)).status_code, 403)

    def test_encrypted_grant_survives_logout_and_refreshes_for_same_owner(self):
        self.owner()
        self.assertTrue(save_connection("calendar", "test-owner", {
            "access_token": "expired-private-access", "refresh_token": "private-refresh", "expires_in": 0,
        }))
        self.client.post("/logout", data={"csrf": "test"})
        with auth_db() as db:
            ciphertext = db.execute("SELECT token FROM google_connections").fetchone()[0]
            self.assertNotIn(b"private-refresh", ciphertext)
        with patch("hq_google_connections.requests.post", return_value=Mock(
                json=Mock(return_value={"access_token": "new-token", "expires_in": 3600}))) as refresh:
            self.assertIsNone(connection("calendar", "another-owner"))
            self.assertEqual(connection("calendar", "test-owner")[0], "new-token")
            self.assertEqual(connection("calendar", "test-owner")[0], "new-token")
            self.assertEqual(refresh.call_count, 1)
