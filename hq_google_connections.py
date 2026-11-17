"""Encrypted, owner-scoped offline Google grants, independent of HQ sign-in."""
import base64
import hashlib
import json
import os
import sqlite3
import time

from cryptography.fernet import Fernet, InvalidToken
import requests

from customer_request_store import storage_ready
from hq_auth_store import auth_db


def _cipher():
    secret = os.environ["HQ_SESSION_SECRET"].encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"hq-google-grants-v1\0" + secret).digest()))


def _table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS google_connections (
        provider TEXT NOT NULL, owner TEXT NOT NULL, token BLOB NOT NULL,
        PRIMARY KEY(provider, owner))""")


def save_connection(provider, owner, token):
    if not storage_ready() or not owner or not token.get("refresh_token"):
        return False
    grant = {"access": token["access_token"], "refresh": token["refresh_token"],
             "expires": time.time() + min(int(token.get("expires_in", 3600)), 3600)}
    encrypted = _cipher().encrypt(json.dumps(grant).encode())
    with auth_db() as db:
        _table(db)
        db.execute("""INSERT INTO google_connections VALUES (?,?,?)
            ON CONFLICT(provider,owner) DO UPDATE SET token=excluded.token""", (provider, owner, encrypted))
    return True


def connection(provider, owner):
    if not storage_ready() or not owner:
        return None
    try:
        cipher = _cipher()
        with auth_db() as db:
            _table(db)
            row = db.execute("SELECT token FROM google_connections WHERE provider=? AND owner=?",
                             (provider, owner)).fetchone()
        if row is None:
            return None
        grant = json.loads(cipher.decrypt(row["token"]))
        if grant["expires"] > time.time() + 30:
            return grant["access"], grant["expires"]
        response = requests.post("https://oauth2.googleapis.com/token", timeout=(3, 5), data={
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "refresh_token": grant["refresh"], "grant_type": "refresh_token",
        })
        response.raise_for_status()
        token = response.json()
        expires = time.time() + min(int(token["expires_in"]), 3600)
        updated = cipher.encrypt(json.dumps({"access": token["access_token"], "expires": expires,
                                            "refresh": token.get("refresh_token") or grant["refresh"]}).encode())
        with auth_db() as db:
            change = db.execute("""UPDATE google_connections SET token=?
                WHERE provider=? AND owner=? AND token=?""", (updated, provider, owner, row["token"]))
        return (token["access_token"], expires) if change.rowcount else None
    except (InvalidToken, OSError, requests.RequestException, sqlite3.Error, TypeError, ValueError, KeyError):
        return None
