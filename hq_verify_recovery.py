"""Offline recovery verification. Never opens the live database or restores data."""
import argparse
from getpass import getpass
import hashlib
import json
from pathlib import Path
import stat
import os
import sqlite3
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import UnsupportedAlgorithm

from hq_recovery_export import MAX_ENVELOPE, decrypt_for_isolated_verification


def _read_bounded(path, maximum):
    fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Regular file required")
        data = handle.read(maximum + 1)
    if not data or len(data) > maximum:
        raise ValueError("Invalid file size")
    return data


def verify_files(envelope_path, key_path, password):
    envelope = _read_bounded(envelope_path, MAX_ENVELOPE)
    key_data = _read_bounded(key_path, 16384)
    key = serialization.load_pem_private_key(key_data, password=password)
    data = decrypt_for_isolated_verification(envelope, key)
    public = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return {
        "format": "tt-hq-offline-verification-v1",
        "result": "authenticated_sqlite_integrity_passed",
        "encrypted_sha256": hashlib.sha256(envelope).hexdigest(),
        "sqlite_sha256": hashlib.sha256(data).hexdigest(),
        "public_key_sha256": hashlib.sha256(public).hexdigest(),
        "sqlite_bytes": len(data),
        "live_restore_performed": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify a downloaded HQ recovery envelope offline. No restore or network access.")
    parser.add_argument("envelope", help="Downloaded encrypted .tthq file")
    parser.add_argument("private_key", help="Local encrypted PEM private key")
    args = parser.parse_args(argv)
    try:
        password = getpass("Private key passphrase: ").encode()
        if not password:
            raise ValueError("Encrypted private key passphrase required")
        result = verify_files(args.envelope, args.private_key, password)
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm, EOFError, KeyboardInterrupt, sqlite3.Error):
        print("Recovery verification failed. Check the encrypted copy, key and passphrase. No restore performed.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
