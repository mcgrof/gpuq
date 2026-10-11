# SPDX-License-Identifier: MIT
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(value, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def filesystem(path):
    path = Path(path)
    while not path.exists():
        path = path.parent
    result = subprocess.run(
        ["findmnt", "-rn", "-T", str(path), "-o", "FSTYPE"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode:
        raise ValueError("cannot determine the storage filesystem with findmnt")
    return result.stdout.strip().splitlines()[-1]


def local_database(path):
    kind = filesystem(Path(path).parent)
    if kind.startswith(("nfs", "fuse.sshfs", "cifs", "smb", "9p")):
        raise ValueError("SQLite queue state must be on a host-local filesystem")
