"""Opt-in encrypted export of an existing private snapshot; no external service.

Private key stays offline. No live restore function or automatic upload.
"""
from contextlib import closing
import os
import sqlite3
import stat
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

import hq_private_recovery as recovery

MAGIC = b"TT-HQ-RECOVERY-1\x00"
MAX_ENVELOPE = recovery.MAX_BYTES + 1024


def _oaep():
    return padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                        algorithm=hashes.SHA256(), label=MAGIC)


def _public_key(pem):
    if not isinstance(pem, str) or len(pem) > 2000 or not pem.strip().startswith("-----BEGIN PUBLIC KEY-----"):
        raise ValueError("RSA public key required")
    key = serialization.load_pem_public_key(pem.strip().encode("ascii"))
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size not in (3072, 4096) or key.public_numbers().e != 65537:
        raise ValueError("3072 or 4096 bit RSA public key required")
    return key


def _verify_sqlite(data):
    if not data.startswith(b"SQLite format 3\x00") or len(data) > recovery.MAX_BYTES:
        raise ValueError("Invalid recovery database")
    deadline = time.monotonic() + recovery.MAX_SECONDS
    with closing(sqlite3.connect(":memory:")) as db:
        db.deserialize(data)
        db.execute("PRAGMA query_only=ON")
        recovery._check(db, deadline)


def export_snapshot(snapshot_id, public_pem):
    key = _public_key(public_pem)
    if not isinstance(snapshot_id, str) or not recovery._ID.fullmatch(snapshot_id):
        raise ValueError("Invalid snapshot")
    _, directory = recovery._paths()
    if not any(item["id"] == snapshot_id for item in recovery.list_snapshots()):
        raise LookupError("Snapshot not found")
    # Open the existing, immutable file with no symlink following; never read live DB.
    fd = os.open(directory / (snapshot_id + ".sqlite3"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid() or info.st_size > recovery.MAX_BYTES):
            raise ValueError("Invalid private snapshot")
        data = handle.read(recovery.MAX_BYTES + 1)
        if len(data) != info.st_size:
            raise ValueError("Snapshot changed")
    _verify_sqlite(data)
    secret = AESGCM.generate_key(bit_length=256)
    wrapped = key.encrypt(secret, _oaep())
    nonce = os.urandom(12)
    header = MAGIC + len(wrapped).to_bytes(2, "big") + wrapped + nonce
    return header + AESGCM(secret).encrypt(nonce, data, header)


def decrypt_for_isolated_verification(envelope, private_key):
    """Offline helper: authenticate and validate bytes, never restore live data."""
    if not isinstance(envelope, bytes) or not envelope.startswith(MAGIC) or len(envelope) > MAX_ENVELOPE:
        raise ValueError("Invalid recovery envelope")
    if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size not in (3072, 4096):
        raise ValueError("Invalid recovery key")
    n = int.from_bytes(envelope[len(MAGIC):len(MAGIC)+2], "big")
    end = len(MAGIC) + 2 + n
    if n != private_key.key_size // 8 or len(envelope) < end + 12 + 16:
        raise ValueError("Invalid recovery envelope")
    try:
        secret = private_key.decrypt(envelope[len(MAGIC)+2:end], _oaep())
        data = AESGCM(secret).decrypt(envelope[end:end+12], envelope[end+12:], envelope[:end+12])
    except (InvalidTag, ValueError):
        raise ValueError("Recovery key or ciphertext invalid") from None
    _verify_sqlite(data)
    return data
