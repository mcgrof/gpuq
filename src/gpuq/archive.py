# SPDX-License-Identifier: MIT
import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import account, identifier, run_path
from .store import TERMINAL, now
from .util import atomic_json, sha256


def mounted_archive(config):
    if not config.get("archive_root"):
        raise ValueError("archival is disabled; configure archive_root and archive_mount")
    root = Path(config["archive_root"]).expanduser()
    if not root.is_absolute():
        raise ValueError("archive_root must be absolute")
    mount = config.get("archive_mount")
    if mount is not None:
        mount = Path(mount).resolve()
        root.resolve().relative_to(mount)
        # Access triggers an automount, but the subsequent exact mount check
        # prevents silently filling an ordinary directory on the root disk.
        next(mount.iterdir(), None)
        result = subprocess.run(
            ["findmnt", "-rn", "-M", str(mount), "-t", "nfs,nfs4,ext4,xfs,btrfs", "-o", "SOURCE"],
            text=True, capture_output=True, timeout=10,
        )
        sources = result.stdout.strip().splitlines()
        if result.returncode or not sources:
            raise ValueError("archive_mount is not mounted; local source files are retained")
        expected = config.get("archive_source")
        if expected and sources != [expected]:
            raise ValueError("archive mount source differs from configuration")
    return root


def signature(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def files(directory, include_checkpoints):
    selected = ["logs", "telemetry"]
    if include_checkpoints:
        selected.append("checkpoints")
    result = []
    for category in selected:
        base = directory / category
        if not base.exists():
            continue
        if base.is_symlink():
            raise ValueError("artifact directories must not be symlinks")
        for path in sorted(base.rglob("*")):
            if path.is_symlink():
                raise ValueError("artifact archives do not follow symlinks")
            if path.is_file():
                result.append(path)
    return result


def copy_verified(source, destination, checksum):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or sha256(destination) != checksum:
            raise ValueError("archive destination already contains different data: " + str(destination))
        return
    fd, temp = tempfile.mkstemp(prefix="." + destination.name, dir=destination.parent)
    os.close(fd)
    try:
        shutil.copy2(source, temp)
        with open(temp, "rb") as file:
            os.fsync(file.fileno())
        if sha256(temp) != checksum:
            raise ValueError("archive checksum verification failed")
        os.replace(temp, destination)
        if sha256(destination) != checksum:
            raise ValueError("archive destination verification failed")
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def archive_run(config, store, run_id, *, older_than_days=30, include_checkpoints=False,
                prune_local=False, dry_run=False):
    if not config.get("archive_root"):
        raise ValueError("archival is disabled; configure archive_root and archive_mount")
    user = account()
    job = store.get(run_id)
    if job["uid"] != user["uid"]:
        raise PermissionError("only the submitting user may archive a run")
    if job["state"] not in TERMINAL or not job["ended_at"] or job["pinned"]:
        raise ValueError("only unpinned completed/failed/cancelled runs may be archived")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=older_than_days)
    if dt.datetime.fromisoformat(job["ended_at"]) > cutoff:
        raise ValueError("run is newer than the completion-time retention cutoff")
    directory = run_path(config, user, job["experiment_id"], job["run_id"])
    if Path(job["run_dir"]).resolve() != directory or directory.is_symlink():
        raise ValueError("run directory is outside the configured artifact root")
    selected = files(directory, include_checkpoints)
    destination = (Path(config["archive_root"]) / identifier(config["host"]) /
                   identifier(user["user"]) / job["experiment_id"] / job["run_id"])
    result = {"run_id": run_id, "destination": str(destination),
              "file_count": len(selected), "prune_local": prune_local, "dry_run": dry_run}
    if dry_run:
        return result
    root = mounted_archive(config)
    destination.resolve().relative_to(root.resolve())
    destination.mkdir(parents=True, exist_ok=True)
    checksums = {}
    signatures = {}
    for source in selected:
        relative = str(source.relative_to(directory))
        signatures[relative] = signature(source)
        checksum = sha256(source)
        copy_verified(source, destination / relative, checksum)
        if signature(source) != signatures[relative] or sha256(source) != checksum:
            raise ValueError("source changed during archival; local files are retained")
        checksums[relative] = {"sha256": checksum, "bytes": source.stat().st_size}
    previous_manifest = destination / "manifest.json"
    previous = json.loads(previous_manifest.read_text()) if previous_manifest.exists() else {}
    if previous and previous.get("run", {}).get("run_id") != run_id:
        raise ValueError("archive manifest belongs to another run")
    manifest = directory / "manifest.json"
    local = json.loads(manifest.read_text()) if manifest.exists() else {"run": job}
    archive_manifest = {
        "run": job, "archived_at": now(), "destination": str(destination),
        "provenance": local.get("provenance", previous.get("provenance", {})),
        "events": store.events(run_id),
        "files": {**previous.get("files", {}), **checksums},
        "includes_checkpoints": bool(include_checkpoints or previous.get("includes_checkpoints")),
    }
    atomic_json(destination / "manifest.json", archive_manifest)
    # Verify all sources again before removing ANY local file. This avoids
    # pruning the first files while later copies or checks are still failing.
    for source in selected:
        relative = str(source.relative_to(directory))
        if (signature(source) != signatures[relative] or
                sha256(source) != checksums[relative]["sha256"] or
                sha256(destination / relative) != checksums[relative]["sha256"]):
            raise ValueError("archive changed during verification; local files are retained")
    store.archived(run_id, user["uid"], destination, checksums, prune_local)
    local["archive_path"] = str(destination)
    local.setdefault("archives", []).append(archive_manifest)
    atomic_json(manifest, local)
    if prune_local:
        for source in selected:
            source.unlink()
    return result
