# SPDX-License-Identifier: MIT
import contextlib
import datetime as dt
import json
import math
import os
import sqlite3
import uuid
from pathlib import Path

from .config import identifier

ACTIVE = ("running", "needs_review")
TERMINAL = ("completed", "failed", "cancelled")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def future(seconds):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


def new_id():
    return "run-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:10]


class Store:
    def __init__(self, path):
        self.path = Path(path)

    def initialize(self, gid=None):
        if self.path.parent.exists():
            allowed = {self.path.name, self.path.name + "-wal", self.path.name + "-shm", "motd.lock"}
            if any(entry.name not in allowed for entry in self.path.parent.iterdir()):
                raise ValueError("initialize requires a dedicated database directory")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A setgid parent and restrictive umask keep SQLite sidecars accessible
        # to the cooperating group, without making metadata world-readable.
        if gid is not None:
            os.chown(self.path.parent, -1, gid)
        self.path.parent.chmod(0o2770)
        old_umask = os.umask(0o007)
        try:
            with contextlib.closing(sqlite3.connect(self.path)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS jobs (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT UNIQUE NOT NULL,
                        experiment_id TEXT NOT NULL,
                        parent_run_id TEXT,
                        uid INTEGER NOT NULL,
                        username TEXT NOT NULL,
                        resource TEXT NOT NULL,
                        description TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'queued',
                        submitted_at TEXT NOT NULL,
                        started_at TEXT,
                        expected_end TEXT,
                        ended_at TEXT,
                        expected_seconds REAL NOT NULL,
                        heartbeat_at TEXT,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        cwd TEXT NOT NULL,
                        command_json TEXT NOT NULL,
                        run_dir TEXT NOT NULL,
                        unit TEXT NOT NULL,
                        exit_code INTEGER,
                        service_result TEXT,
                        archive_path TEXT,
                        pinned INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_resource
                    ON jobs(resource) WHERE state IN ('running', 'needs_review');
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        event TEXT NOT NULL,
                        details_json TEXT NOT NULL
                    );
                """)
            if gid is not None:
                os.chown(self.path, -1, gid)
            self.path.chmod(0o660)
        finally:
            os.umask(old_umask)

    @contextlib.contextmanager
    def connection(self, write=False):
        if not self.path.is_file():
            raise ValueError("queue database is missing; an administrator must run init")
        # rw avoids creating a replacement database after a path/config mistake.
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            raise ValueError("unknown run ID")
        result = dict(row)
        result["command"] = json.loads(result.pop("command_json"))
        return result

    @staticmethod
    def owned(row, uid):
        if row is None:
            raise ValueError("unknown run ID")
        if row["uid"] != uid:
            raise PermissionError("only the submitting user may change this run")

    @staticmethod
    def event(db, run_id, event, details=None):
        db.execute(
            "INSERT INTO events(run_id,timestamp,event,details_json) VALUES(?,?,?,?)",
            (run_id, now(), event, json.dumps(details or {}, sort_keys=True)),
        )

    def submit(self, *, user, resource, description, experiment_id, command,
               cwd, run_dir, expected_seconds, run_id=None, parent_run_id=None):
        run_id = identifier(run_id or new_id())
        identifier(experiment_id)
        identifier(resource)
        if parent_run_id is not None:
            identifier(parent_run_id)
        if not math.isfinite(expected_seconds) or expected_seconds <= 0:
            raise ValueError("expected runtime must be positive")
        if not command or not all(isinstance(item, str) and "\0" not in item for item in command):
            raise ValueError("command must be a nonempty argv list without NUL characters")
        if len(json.dumps(command)) > 65536 or len(description) > 512:
            raise ValueError("command/description too long")
        with self.connection(write=True) as db:
            db.execute(
                """INSERT INTO jobs(run_id,experiment_id,parent_run_id,uid,username,resource,
                   description,submitted_at,expected_seconds,cwd,command_json,run_dir,unit)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, experiment_id, parent_run_id, user["uid"], user["user"], resource,
                 description, now(), expected_seconds, str(cwd), json.dumps(command),
                 str(run_dir), f"gpuq-{run_id}.service"),
            )
            self.event(db, run_id, "submitted")
        return self.get(run_id)

    def get(self, run_id):
        with self.connection() as db:
            return self.decode(db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone())

    def jobs(self, include_history=False):
        with self.connection() as db:
            query = "SELECT * FROM jobs"
            if not include_history:
                query += " WHERE state NOT IN ('completed','failed','cancelled')"
            return [self.decode(row) for row in db.execute(query + " ORDER BY seq")]

    def events(self, run_id):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)
            )]

    def claim(self, run_id, uid):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            self.owned(row, uid)
            if row["state"] != "queued" or row["cancel_requested"]:
                return False
            active = db.execute(
                "SELECT 1 FROM jobs WHERE resource=? AND state IN ('running','needs_review')",
                (row["resource"],),
            ).fetchone()
            head = db.execute(
                "SELECT run_id FROM jobs WHERE resource=? AND state='queued' ORDER BY seq LIMIT 1",
                (row["resource"],),
            ).fetchone()
            if active or head["run_id"] != run_id:
                return False
            db.execute(
                "UPDATE jobs SET state='running',started_at=?,expected_end=?,heartbeat_at=? WHERE run_id=?",
                (now(), future(row["expected_seconds"]), now(), run_id),
            )
            self.event(db, run_id, "started")
            return True

    def heartbeat(self, run_id, uid):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            self.owned(row, uid)
            db.execute("UPDATE jobs SET heartbeat_at=? WHERE run_id=?", (now(), run_id))

    def cancel(self, run_id, uid):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            self.owned(row, uid)
            if row["state"] in TERMINAL:
                return False
            db.execute("UPDATE jobs SET cancel_requested=1 WHERE run_id=?", (run_id,))
            self.event(db, run_id, "cancellation_requested")
            # A running slot remains held until systemd's stop-post finalizer.
            return True

    def finish(self, run_id, uid, *, state, exit_code=None, service_result=None):
        if state not in TERMINAL:
            raise ValueError("invalid terminal state")
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            self.owned(row, uid)
            if row["state"] in TERMINAL:
                return False
            db.execute(
                "UPDATE jobs SET state=?,ended_at=?,exit_code=?,service_result=? WHERE run_id=?",
                (state, now(), exit_code, service_result, run_id),
            )
            self.event(db, run_id, state, {"exit_code": exit_code, "service_result": service_result})
            return True

    def review(self, run_id, uid, reason):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            self.owned(row, uid)
            if row["state"] in ACTIVE:
                db.execute("UPDATE jobs SET state='needs_review' WHERE run_id=?", (run_id,))
            self.event(db, run_id, "needs_review", {"reason": reason})

    def pin(self, run_id, uid, value):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            self.owned(row, uid)
            db.execute("UPDATE jobs SET pinned=? WHERE run_id=?", (int(value), run_id))
            self.event(db, run_id, "pinned" if value else "unpinned")

    def estimate(self, run_id, uid, seconds):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("expected runtime must be positive and finite")
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            self.owned(row, uid)
            if row["state"] in TERMINAL:
                raise ValueError("cannot extend a finished run")
            db.execute("UPDATE jobs SET expected_seconds=?,expected_end=? WHERE run_id=?",
                       (seconds, future(seconds) if row["started_at"] else None, run_id))
            self.event(db, run_id, "estimate_updated", {"remaining_seconds": seconds})

    def archived(self, run_id, uid, destination, checksums, pruned):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            self.owned(row, uid)
            db.execute("UPDATE jobs SET archive_path=? WHERE run_id=?", (str(destination), run_id))
            self.event(db, run_id, "archived", {"path": str(destination), "files": checksums,
                                             "pruned_local": pruned})
