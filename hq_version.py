"""Release identifiers and content fingerprints for the HQ web interface."""
import hashlib
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
UPDATE_SCRIPT_VERSION = hashlib.sha256((ROOT / "static/hq-updates.js").read_bytes()).hexdigest()[:16]
LOCAL_VERSION = "local-" + hashlib.sha256((ROOT / "app.py").read_bytes() +
                                          UPDATE_SCRIPT_VERSION.encode()).hexdigest()[:12]
ICON_VERSION = "20261117c"


def build_version():
    commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else LOCAL_VERSION
