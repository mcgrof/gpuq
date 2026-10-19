# SPDX-License-Identifier: MIT
"""Versioned, read-only queue view for panels, bars and other frontends.

Frontends run ``gpuq snapshot`` (locally or over ssh) and parse its JSON.
Fields may be added within a schema version; renames or removals bump it.
"""
import datetime as dt
import json
import os
from pathlib import Path

from .util import atomic_json

SCHEMA = 1
ACTIVE = {"running", "needs_review"}


def live_dir(config):
    return Path(config["database"]).parent / "live"


def publish_live(config, resource, sample):
    """Share the latest device-wide sample with other queue members."""
    path = live_dir(config) / (resource + ".json")
    old_umask = os.umask(0o007)
    try:
        path.parent.mkdir(mode=0o2770, exist_ok=True)
        atomic_json(path, sample)
        path.chmod(0o660)
    finally:
        os.umask(old_umask)


def read_live(config, resource, instant):
    try:
        sample = json.loads((live_dir(config) / (resource + ".json")).read_text())
    except (OSError, ValueError):
        return None
    try:
        age = (instant - dt.datetime.fromisoformat(sample["timestamp_utc"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None
    # A finished or crashed collector leaves its last sample behind.
    sample["age_seconds"] = round(age, 1)
    sample["stale"] = age > 3 * config["telemetry_seconds"] + 5
    return sample


def schedule(jobs, instant):
    """Yield (job, expected_end) for unfinished jobs in FIFO order."""
    cursors = {}
    for job in sorted(jobs, key=lambda row: row["seq"]):
        if job["state"] in {"completed", "failed", "cancelled"}:
            continue
        resource = job["resource"]
        if job["expected_end"]:
            end = dt.datetime.fromisoformat(job["expected_end"])
        else:
            end = max(cursors.get(resource, instant), instant) + dt.timedelta(seconds=job["expected_seconds"])
        cursors[resource] = max(end, instant)
        yield job, end


def build(config, jobs, instant=None):
    instant = instant or dt.datetime.now(dt.timezone.utc)
    resources = {name: {"name": name, "active": None, "queue": [], "telemetry": None}
                 for name in config["resources"]}
    for job, end in schedule(jobs, instant):
        slot = resources.setdefault(job["resource"], {
            "name": job["resource"], "active": None, "queue": [], "telemetry": None,
        })
        entry = {key: job[key] for key in (
            "run_id", "experiment_id", "username", "description", "state",
            "submitted_at", "started_at", "expected_seconds", "cancel_requested",
        )}
        entry["cancel_requested"] = bool(entry["cancel_requested"])
        entry["expected_end"] = end.isoformat(timespec="seconds")
        entry["remaining_seconds"] = max(0, round((end - instant).total_seconds()))
        if job["state"] in ACTIVE:
            slot["active"] = entry
        else:
            entry["position"] = len(slot["queue"]) + 1
            slot["queue"].append(entry)
    for name, slot in resources.items():
        if name in config["resources"]:
            slot["telemetry"] = read_live(config, name, instant)
    return {
        "schema": SCHEMA,
        "host": config["host"],
        "generated_at": instant.isoformat(timespec="seconds"),
        "resources": list(resources.values()),
    }


def waybar(view):
    """Render the custom-module JSON accepted by waybar and similar bars."""
    busy = [slot for slot in view["resources"] if slot["active"]]
    waiting = sum(len(slot["queue"]) for slot in view["resources"])
    parts, lines = [], [view["host"]]
    for slot in view["resources"]:
        active, stats = slot["active"], (slot["telemetry"] or {}).get("stats", {})
        live = slot["telemetry"] and not slot["telemetry"]["stale"]
        util = stats.get("utilization") if live else None
        if active:
            parts.append(slot["name"] + (f" {util:.0f}%" if isinstance(util, (int, float)) else ""))
        lines.append(f"{slot['name']}: " + (
            f"{active['username']} {active['experiment_id']} "
            f"({active['remaining_seconds'] // 60} min left, {active['state']})" if active else "idle"
        ))
        for job in slot["queue"]:
            lines.append(f"  {job['position']}. {job['username']} {job['experiment_id']}")
    text = (" ".join(parts) or "idle") + (f" +{waiting}" if waiting else "")
    return {
        "text": text,
        "tooltip": "\n".join(lines),
        "class": "busy" if busy else "idle",
        "alt": "busy" if busy else "idle",
    }
