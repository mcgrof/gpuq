# SPDX-License-Identifier: MIT
import argparse
import datetime as dt
import fcntl
import grp
import json
import sqlite3
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from . import archive, motd, runner, snapshot
from .config import account, identifier, load, run_path
from .store import Store, TERMINAL, new_id
from .util import local_database


def parser():
    root = argparse.ArgumentParser(description="Cooperative FIFO GPU reservations and experiment history")
    root.add_argument("--config", default=os.environ.get("GPUQ_CONFIG"),
                      help="private configuration file (or set GPUQ_CONFIG)")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init", help="initialize a host-local shared database")
    init.add_argument("--group", help="Unix group allowed to use the queue")
    submit = commands.add_parser("submit", help="enqueue and launch a managed user service")
    submit.add_argument("--experiment", required=True, type=identifier)
    submit.add_argument("--resource", default="gpu", type=identifier)
    submit.add_argument("--description", default="")
    submit.add_argument("--expected-minutes", type=float, required=True)
    submit.add_argument("--parent-run", type=identifier)
    submit.add_argument("--cwd", default=str(Path.cwd()))
    submit.add_argument("command", nargs=argparse.REMAINDER)
    status = commands.add_parser("status", help="show current reservations")
    status.add_argument("--json", action="store_true")
    history = commands.add_parser("history", help="show completed and current runs")
    history.add_argument("--experiment")
    history.add_argument("--json", action="store_true")
    show = commands.add_parser("show", help="show a run's metadata and events")
    show.add_argument("run_id")
    cancel = commands.add_parser("cancel", help="cancel your own job and drain its systemd unit")
    cancel.add_argument("run_id")
    recover = commands.add_parser("reconcile", help="recover your inactive run after an interrupted finalizer")
    recover.add_argument("run_id")
    for name in ("pin", "unpin"):
        commands.add_parser(name, help="control automatic archive eligibility").add_argument("run_id")
    estimate = commands.add_parser("estimate", help="update your run's remaining time estimate")
    estimate.add_argument("run_id")
    estimate.add_argument("--remaining-minutes", type=float, required=True)
    cold = commands.add_parser("archive", help="verify and archive completed run artifacts")
    cold.add_argument("--run-id")
    cold.add_argument("--older-than-days", type=float, default=30)
    cold.add_argument("--include-checkpoints", action="store_true")
    cold.add_argument("--prune-local", action="store_true")
    cold.add_argument("--dry-run", action="store_true")
    view = commands.add_parser("snapshot", help="print a versioned queue and telemetry view for frontends")
    view.add_argument("--format", choices=("json", "waybar"), default="json")
    banner = commands.add_parser("motd", help="render the queue as a login banner")
    banner.add_argument("--write", metavar="FILE")
    for name in ("_worker", "_finalize"):
        commands.add_parser(name, help=argparse.SUPPRESS).add_argument("run_id")
    return root


def print_jobs(jobs, as_json):
    if as_json:
        print(json.dumps(jobs, indent=2))
        return
    print("USER\tSTATE\tRESOURCE\tRUN_ID\tEXPERIMENT")
    for job in jobs:
        print("\t".join(motd.field(value) for value in (
            job["username"], job["state"], job["resource"], job["run_id"], job["experiment_id"]
        )))
    if not jobs:
        print("(no runs)")


def submit_job(config, *, experiment, resource, description, expected_minutes,
               command, cwd, parent_run=None, run_id=None, prepare=None):
    store = Store(config["database"])
    user = account()
    if user["uid"] == 0:
        raise PermissionError("submit as the research user, not through sudo")
    if resource not in config["resources"]:
        raise ValueError("unknown resource")
    command = list(command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise ValueError("supply a command after --")
    cwd = Path(cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError("working directory does not exist")
    if "/" in command[0]:
        executable = Path(command[0]).expanduser()
        # Preserve interpreter symlinks: resolving a venv's bin/python to
        # /usr/bin/python loses that virtual environment at execution time.
        command[0] = os.path.abspath(cwd / executable)
    else:
        executable = shutil.which(command[0])
        if executable is None:
            raise ValueError("command is not in your PATH")
        command[0] = executable
    run_id = run_id or new_id()
    directory = run_path(config, user, experiment, run_id)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    job = store.submit(
        user=user, resource=resource, description=description or experiment,
        experiment_id=experiment, command=command, cwd=cwd, run_dir=directory,
        expected_seconds=expected_minutes * 60, run_id=run_id, parent_run_id=parent_run,
    )
    try:
        if prepare is not None:
            prepare(job)
        runner.manifest(config, store, run_id)
        runner.launch(config, job)
    except Exception:
        # An ambiguous timeout may mean that the service actually started.
        # Never free such a slot automatically.
        check = subprocess.run(
            ["systemctl", "--user", "show", job["unit"], "-p", "LoadState", "--value"],
            text=True, capture_output=True, timeout=10,
        )
        if check.returncode == 0 and check.stdout.strip() == "not-found":
            store.finish(run_id, user["uid"], state="failed", service_result="launch-failed")
            runner.manifest(config, store, run_id)
        else:
            store.review(run_id, user["uid"], "service launch result was ambiguous")
        raise
    return job


def execute(args):
    os.umask(0o007)
    if not args.config:
        raise ValueError("provide --config or set GPUQ_CONFIG")
    config = load(args.config)
    store = Store(config["database"])
    if args.action == "init":
        local_database(config["database"])
        group = grp.getgrnam(args.group).gr_gid if args.group else os.getgid()
        store.initialize(group)
        print("Initialized " + config["database"])
        return
    if args.action == "submit":
        job = submit_job(
            config, experiment=args.experiment, resource=args.resource,
            description=args.description, expected_minutes=args.expected_minutes,
            command=args.command, cwd=args.cwd, parent_run=args.parent_run,
        )
        print(job["run_id"])
    elif args.action in ("status", "history"):
        jobs = store.jobs(include_history=args.action == "history")
        if args.action == "history" and args.experiment:
            jobs = [job for job in jobs if job["experiment_id"] == args.experiment]
        print_jobs(jobs, args.json)
    elif args.action == "show":
        print(json.dumps({"run": store.get(args.run_id), "events": store.events(args.run_id)}, indent=2))
    elif args.action == "cancel":
        job = store.get(args.run_id)
        if store.cancel(args.run_id, os.getuid()):
            subprocess.run(["systemctl", "--user", "stop", job["unit"]], check=True, timeout=45)
        print(args.run_id)
    elif args.action == "reconcile":
        job = store.get(args.run_id)
        if job["uid"] != os.getuid():
            raise PermissionError("only the submitting user may reconcile a run")
        result = subprocess.run(
            ["systemctl", "--user", "show", job["unit"], "-p", "ActiveState", "--value"],
            text=True, capture_output=True, check=True, timeout=10,
        ).stdout.strip()
        if result not in {"inactive", "failed"}:
            raise ValueError("service is still active or its state is unknown")
        if not runner.guard_idle(config["resources"][job["resource"]]):
            raise ValueError("GPU is still occupied; reservation is retained")
        state = "cancelled" if job["cancel_requested"] else "failed"
        store.finish(args.run_id, os.getuid(), state=state, service_result="reconciled")
        runner.manifest(config, store, args.run_id)
    elif args.action in ("pin", "unpin"):
        store.pin(args.run_id, os.getuid(), args.action == "pin")
    elif args.action == "estimate":
        store.estimate(args.run_id, os.getuid(), args.remaining_minutes * 60)
        runner.manifest(config, store, args.run_id)
    elif args.action == "archive":
        import math
        if not config.get("archive_root"):
            raise ValueError("archival is disabled; configure archive_root and archive_mount")
        if not math.isfinite(args.older_than_days) or args.older_than_days < 0:
            raise ValueError("older-than-days must be nonnegative")
        user = account()
        jobs = [store.get(args.run_id)] if args.run_id else store.jobs(include_history=True)
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.older_than_days)
        results = []
        for job in jobs:
            if not args.run_id and (
                job["uid"] != user["uid"] or job["state"] not in TERMINAL or job["pinned"] or
                not job["ended_at"] or dt.datetime.fromisoformat(job["ended_at"]) > cutoff
            ):
                continue
            results.append(archive.archive_run(
                config, store, job["run_id"], older_than_days=args.older_than_days,
                include_checkpoints=args.include_checkpoints, prune_local=args.prune_local,
                dry_run=args.dry_run,
            ))
        print(json.dumps(results, indent=2))
    elif args.action == "snapshot":
        view = snapshot.build(config, store.jobs())
        if args.format == "waybar":
            print(json.dumps(snapshot.waybar(view)))
        else:
            print(json.dumps(view, indent=2))
    elif args.action == "motd":
        if args.write:
            # Serialize snapshot generation too, so concurrent writers cannot
            # overwrite a fresh snapshot with an older one.
            lock = Path(config["database"]).parent / "motd.lock"
            with lock.open("a") as file:
                fcntl.flock(file, fcntl.LOCK_EX)
                motd.update(args.write, motd.render(store.jobs()))
        else:
            print(motd.render(store.jobs()), end="")
    elif args.action == "_worker":
        return runner.worker(config, args.run_id)
    elif args.action == "_finalize":
        runner.finalize(config, args.run_id)
    return 0


def main():
    try:
        code = execute(parser().parse_args())
    except (ValueError, PermissionError, OSError, RuntimeError, subprocess.SubprocessError, sqlite3.Error) as error:
        print("gpuq: " + str(error), file=sys.stderr)
        code = 2
    raise SystemExit(code or 0)
