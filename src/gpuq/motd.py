# SPDX-License-Identifier: MIT
import datetime as dt
import os
import tempfile
from pathlib import Path

BEGIN = "# BEGIN GPUQ RESERVATIONS"
END = "# END GPUQ RESERVATIONS"


def field(text):
    # Descriptions and command metadata must never inject terminal escapes,
    # extra rows, or managed-block delimiters into the login banner.
    return "".join(char if char.isprintable() and char not in "|" else " " for char in str(text))


def render(jobs):
    lines = [
        BEGIN,
        "USER | EXPERIMENT | START_UTC | EXPECTED_END_UTC | RESOURCE | RUN_ID | STATE",
    ]
    cursors = {}
    instant = dt.datetime.now(dt.timezone.utc)
    for job in sorted(jobs, key=lambda row: row["seq"]):
        if job["state"] in {"completed", "failed", "cancelled"}:
            continue
        resource = job["resource"]
        start = job["started_at"] or "pending"
        if job["expected_end"]:
            end = dt.datetime.fromisoformat(job["expected_end"])
        else:
            end = max(cursors.get(resource, instant), instant) + dt.timedelta(seconds=job["expected_seconds"])
        cursors[resource] = max(end, instant)
        lines.append(" | ".join(field(value) for value in (
            job["username"], job["description"], start, end.isoformat(timespec="seconds"),
            resource, job["run_id"], job["state"],
        )))
    if len(lines) == 2:
        lines.append("(no reservations)")
    lines.append(END)
    return "\n".join(lines) + "\n"


def update(path, block):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("refusing to replace a symlink MOTD")
    original = path.read_text() if path.exists() else ""
    if BEGIN in original or END in original:
        if original.count(BEGIN) != 1 or original.count(END) != 1:
            raise ValueError("ambiguous managed MOTD block")
        start = original.index(BEGIN)
        stop = original.index(END, start) + len(END)
        if stop < start:
            raise ValueError("invalid managed MOTD block")
        if original[stop:stop + 1] == "\n":
            stop += 1
        value = original[:start] + block + original[stop:]
    else:
        value = original.rstrip("\n") + "\n\n" + block
    info = path.stat() if path.exists() else None
    fd, temp = tempfile.mkstemp(prefix=".gpuq-motd-", dir=path.parent)
    try:
        if info:
            os.fchmod(fd, info.st_mode & 0o777)
            os.fchown(fd, info.st_uid, info.st_gid)
        else:
            os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
