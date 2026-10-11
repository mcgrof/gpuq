# SPDX-License-Identifier: MIT
import json
import math
import os
import pwd
import re
from pathlib import Path

IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}\Z")


def identifier(value):
    if not IDENTIFIER.fullmatch(value) or value in {".", ".."}:
        raise ValueError("IDs must be 1-96 letters, digits, dots, dashes or underscores")
    return value


def account(uid=None):
    uid = os.getuid() if uid is None else uid
    user = pwd.getpwuid(uid)
    return {"uid": uid, "user": user.pw_name, "home": user.pw_dir}


def load(path):
    path = Path(path).expanduser().resolve()
    data = json.loads(path.read_text())
    resources = data.get("resources", {})
    if not resources:
        raise ValueError("config must define at least one resource")
    for name, resource in resources.items():
        identifier(name)
        if resource.get("guard_mode") not in {"nvidia", "fuser", "exit-zero-idle"}:
            raise ValueError(f"{name}: unsupported or missing guard_mode")
        guard = resource.get("guard")
        if not isinstance(guard, list) or not guard or not all(
            isinstance(arg, str) and arg for arg in guard
        ):
            raise ValueError(f"{name}: guard must be a nonempty argv list")
        environment = resource.get("environment", {})
        allowed = {"CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES"}
        if set(environment) - allowed or not all(
            isinstance(value, str) for value in environment.values()
        ):
            raise ValueError(f"{name}: environment may only select GPU devices")
    for key in ("host", "database", "artifact_root"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"config must explicitly set {key}")
    identifier(data["host"])
    db = Path(data["database"])
    if not db.is_absolute():
        raise ValueError("database must be an absolute host-local path")
    artifacts = data["artifact_root"]
    if not Path(artifacts).is_absolute() and artifacts != "~" and not artifacts.startswith("~/"):
        raise ValueError("artifact_root must be absolute or start with ~/")
    archive_root = data.get("archive_root")
    archive_mount = data.get("archive_mount")
    if archive_root is not None:
        if not isinstance(archive_root, str) or not Path(archive_root).is_absolute():
            raise ValueError("archive_root must be an absolute path")
        if "archive_mount" not in data:
            raise ValueError("set archive_mount explicitly (null allows an ordinary directory)")
        if archive_mount is not None:
            if not isinstance(archive_mount, str) or not Path(archive_mount).is_absolute():
                raise ValueError("archive_mount must be an absolute path or null")
            Path(archive_root).relative_to(Path(archive_mount))
    result = {
        **data,
        "path": str(path),
        "database": str(db),
        "resources": resources,
        "artifact_root": data["artifact_root"],
        "archive_root": archive_root,
        "archive_mount": archive_mount,
        "poll_seconds": float(data.get("poll_seconds", 5)),
        "telemetry_seconds": float(data.get("telemetry_seconds", 5)),
        "host": data["host"],
    }
    if any(not math.isfinite(result[key]) or result[key] < 0.1
           for key in ("poll_seconds", "telemetry_seconds")):
        raise ValueError("poll and telemetry intervals must be at least 0.1 seconds")
    return result


def artifact_root(config, user):
    text = str(config["artifact_root"]).replace("{user}", user["user"])
    if text == "~" or text.startswith("~/"):
        text = user["home"] + text[1:]
    path = Path(text)
    if not path.is_absolute():
        raise ValueError("artifact_root must be absolute or start with ~/")
    return path.resolve()


def run_path(config, user, experiment_id, run_id):
    return artifact_root(config, user) / identifier(experiment_id) / identifier(run_id)
