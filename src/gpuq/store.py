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


def signal_names():
    import signal
    return {item.name for item in signal.Signals}

ACTIVE = ("running", "needs_review")
TERMINAL = ("completed", "failed", "cancelled")
PRIORITIES = ("normal", "low")
# Queued jobs claim in this order: every normal job before any low one, FIFO within each class.
QUEUE_ORDER = "ORDER BY priority='low', seq"
# Columns added after the first release. Older databases gain them on first use; older gpuq
# versions keep working against the wider table because they select by name.
LATER_COLUMNS = {
    "priority": "TEXT NOT NULL DEFAULT 'normal'",
    # 0 none; 1 checkpoint and requeue; 2 checkpoint and stop (cancelled, no requeue)
    "preempt_requested": "INTEGER NOT NULL DEFAULT 0",
    "preempt_signal": "TEXT NOT NULL DEFAULT 'SIGUSR1'",
    "preempt_grace_seconds": "REAL NOT NULL DEFAULT 900",
    "requeues": "INTEGER NOT NULL DEFAULT 0",
    # Optional per-attempt limits enforced by the worker; NULL means none.
    "timeout_seconds": "REAL",
    "idle_seconds": "REAL",
    "idle_percent": "REAL NOT NULL DEFAULT 5",
}
PREEMPT_REQUEUE, PREEMPT_STOP = 1, 2


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
        self.migrated = False

    @staticmethod
    def migrate(db):
        present = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        for name, declaration in LATER_COLUMNS.items():
            if name not in present:
                try:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
                except sqlite3.OperationalError as error:
                    # Another process added it between the check and the ALTER.
                    if "duplicate column" not in str(error):
                        raise
        db.commit()

    def initialize(self, gid=None):
        if self.path.parent.exists():
            allowed = {self.path.name, self.path.name + "-wal", self.path.name + "-shm", "motd.lock", "live"}
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
                self.migrate(db)
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
        if not self.migrated:
            self.migrate(db)
            self.migrated = True
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
               cwd, run_dir, expected_seconds, run_id=None, parent_run_id=None,
               priority="normal", preempt_signal="SIGUSR1", preempt_grace_seconds=900,
               timeout_seconds=None, idle_seconds=None, idle_percent=5):
        run_id = identifier(run_id or new_id())
        identifier(experiment_id)
        identifier(resource)
        if parent_run_id is not None:
            identifier(parent_run_id)
        if not math.isfinite(expected_seconds) or expected_seconds <= 0:
            raise ValueError("expected runtime must be positive")
        if priority not in PRIORITIES:
            raise ValueError("priority must be one of " + ", ".join(PRIORITIES))
        if preempt_signal != "none" and preempt_signal not in signal_names():
            raise ValueError("preempt signal must be a signal name such as SIGUSR1, or none")
        if not math.isfinite(preempt_grace_seconds) or preempt_grace_seconds < 0:
            raise ValueError("preemption grace must be finite and nonnegative")
        for limit in (timeout_seconds, idle_seconds):
            if limit is not None and (not math.isfinite(limit) or limit <= 0):
                raise ValueError("timeout and idle limits must be positive and finite")
        if not math.isfinite(idle_percent) or not 0 <= idle_percent < 100:
            raise ValueError("idle percent must be in [0, 100)")
        if not command or not all(isinstance(item, str) and "\0" not in item for item in command):
            raise ValueError("command must be a nonempty argv list without NUL characters")
        if len(json.dumps(command)) > 65536 or len(description) > 512:
            raise ValueError("command/description too long")
        with self.connection(write=True) as db:
            db.execute(
                """INSERT INTO jobs(run_id,experiment_id,parent_run_id,uid,username,resource,
                   description,submitted_at,expected_seconds,cwd,command_json,run_dir,unit,
                   priority,preempt_signal,preempt_grace_seconds,timeout_seconds,idle_seconds,idle_percent)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, experiment_id, parent_run_id, user["uid"], user["user"], resource,
                 description, now(), expected_seconds, str(cwd), json.dumps(command),
                 str(run_dir), f"gpuq-{run_id}.service", priority, preempt_signal,
                 preempt_grace_seconds, timeout_seconds, idle_seconds, idle_percent),
            )
            details = {"priority": priority} if priority != "normal" else {}
            details.update({key: value for key, value in (("timeout_seconds", timeout_seconds),
                                                           ("idle_seconds", idle_seconds)) if value is not None})
            self.event(db, run_id, "submitted", details or None)
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
                f"SELECT run_id FROM jobs WHERE resource=? AND state='queued' {QUEUE_ORDER} LIMIT 1",
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

    def request_preemption(self, run_id, uid, mode=PREEMPT_REQUEUE, reason=None):
        """Ask a running job to checkpoint and exit at its next stopping point. Owners may preempt
        their own runs; any queue member may preempt a low-priority run."""
        if mode not in (PREEMPT_REQUEUE, PREEMPT_STOP):
            raise ValueError("invalid preemption mode")
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("unknown run ID")
            if row["uid"] != uid and (row["priority"] != "low" or mode != PREEMPT_REQUEUE):
                raise PermissionError("only the submitting user may preempt a normal-priority run")
            if row["state"] != "running" or row["cancel_requested"] or row["preempt_requested"] >= mode:
                return False
            db.execute("UPDATE jobs SET preempt_requested=? WHERE run_id=?", (mode, run_id))
            self.event(db, run_id, "preemption_requested",
                       {"mode": "stop" if mode == PREEMPT_STOP else "requeue", "uid": uid, "reason": reason})
            return True

    def preempt_for(self, run_id, uid):
        """Called by a waiting normal-priority job: if it heads the queue and a low-priority run holds
        the resource, ask that run to yield. Returns the preempted run ID, or None."""
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            self.owned(row, uid)
            if row["state"] != "queued" or row["cancel_requested"] or row["priority"] != "normal":
                return None
            head = db.execute(
                f"SELECT run_id FROM jobs WHERE resource=? AND state='queued' {QUEUE_ORDER} LIMIT 1",
                (row["resource"],),
            ).fetchone()
            active = db.execute(
                "SELECT * FROM jobs WHERE resource=? AND state IN ('running','needs_review')",
                (row["resource"],),
            ).fetchone()
            if (head["run_id"] != run_id or active is None or active["state"] != "running"
                    or active["priority"] != "low" or active["preempt_requested"] or active["cancel_requested"]):
                return None
            db.execute("UPDATE jobs SET preempt_requested=? WHERE run_id=?", (PREEMPT_REQUEUE, active["run_id"]))
            self.event(db, active["run_id"], "preemption_requested",
                       {"mode": "requeue", "uid": uid, "reason": "queued " + run_id})
            return active["run_id"]

    def requeue(self, run_id, uid, unit):
        """Return a preempted run to the queue under a new service unit. It keeps its sequence number,
        so it resumes ahead of later jobs of its priority."""
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
            self.owned(row, uid)
            if row["state"] not in ACTIVE:
                raise ValueError("only an active run can be requeued")
            db.execute(
                """UPDATE jobs SET state='queued',started_at=NULL,expected_end=NULL,preempt_requested=0,
                   unit=?,requeues=requeues+1 WHERE run_id=?""",
                (unit, run_id),
            )
            self.event(db, run_id, "requeued", {"unit": unit, "requeues": row["requeues"] + 1})

    def record(self, run_id, uid, event, details=None):
        """Append an event to the owner's run, e.g. a limit the worker enforced."""
        with self.connection(write=True) as db:
            self.owned(db.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone(), uid)
            self.event(db, run_id, event, details)

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
