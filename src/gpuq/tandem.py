# SPDX-License-Identifier: MIT
"""Coordinate ordinary host-local reservations with renewable start barriers."""
import hmac
import json
import math
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import runner
from .config import identifier
from .store import Store, TERMINAL, new_id, now
from .util import atomic_json, local_database


def number(value, name, minimum, maximum):
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def argv(value):
    if not isinstance(value, list) or not value or not all(
        isinstance(x, str) and x and "\0" not in x for x in value
    ) or len(json.dumps(value)) > 65536:
        raise ValueError("command must be a bounded nonempty argv list")
    return list(value)


def plan(value):
    """Validate private deployment inputs before creating any reservation."""
    if not isinstance(value, dict) or not {"experiment", "state_root", "members"} <= value.keys():
        raise ValueError("plan needs experiment, state_root and members")
    value = json.loads(json.dumps(value))
    identifier(value["experiment"])
    root = Path(value["state_root"]).expanduser()
    if not root.is_absolute():
        raise ValueError("state_root must be absolute")
    value["state_root"] = str(root)
    for key, default, low, high in (
        ("lease_seconds", 90, 15, 600),
        ("poll_seconds", 2, .1, 10),
        ("wait_seconds", 86400, 1, 604800),
        ("run_seconds", 3600, 1, 604800),
    ):
        value[key] = number(value.get(key, default), key, low, high)
    members = value.get("members")
    if not isinstance(members, list) or not 2 <= len(members) <= 8:
        raise ValueError("tandem needs two to eight participants")
    if value["lease_seconds"] < 2 * (12 * len(members) + value["poll_seconds"]):
        raise ValueError("lease_seconds must cover two rounds of bounded remote calls")
    names = set()
    for member in members:
        if not isinstance(member, dict) or not {"name", "cwd", "command"} <= member.keys():
            raise ValueError("participant needs name, cwd and command")
        identifier(member["name"])
        if member["name"] in names:
            raise ValueError("duplicate participant name")
        names.add(member["name"])
        target = member.get("ssh", "local")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,200}", target):
            raise ValueError("ssh must be a configured alias or local")
        member["program"] = argv(member.get("program", ["gpuq"]))
        member["command"] = argv(member["command"])
        member["resource"] = identifier(member.get("resource", "gpu"))
        if not Path(member["cwd"]).is_absolute():
            raise ValueError("participant cwd must be absolute")
        member["expected_minutes"] = number(member.get("expected_minutes", 30), "expected_minutes", .01, 10080)
    return value


def remote(member, request):
    """Send one bounded JSON request using the caller's existing SSH identity."""
    command = list(member["program"])
    if member.get("config"):
        command += ["--config", member["config"]]
    command += ["_tandem-rpc"]
    if member.get("ssh", "local") != "local":
        command = ["ssh", "-T", "-oBatchMode=yes", "-oConnectTimeout=5", "--",
                   member["ssh"], shlex.join(command)]
    result = subprocess.run(command, input=json.dumps(request), text=True,
                            capture_output=True, timeout=12)
    if result.returncode:
        raise RuntimeError("participant RPC failed: " + result.stderr[-2000:])
    return json.loads(result.stdout)


def metadata(job):
    return Path(job["run_dir"]) / "tandem.json"


def control(job, spec, decision):
    atomic_json(Path(job["run_dir"]) / "tandem-control.json", {
        "token": spec["token"], "decision": decision,
        "deadline": time.monotonic() + spec["lease_seconds"],
    })


def rpc(config, request):
    """Operate only on the authenticated account's matching participant."""
    from .cli import submit_job
    operation = request["operation"]
    if operation in {"identity", "availability"}:
        if request["resource"] not in config["resources"]:
            raise ValueError("unknown resource")
        if operation == "availability":
            busy = any(job["resource"] == request["resource"]
                       for job in Store(config["database"]).jobs())
            return {"idle": not busy}
        return {"host": config["host"], "resource": request["resource"], "protocol": 1}
    store = Store(config["database"])
    run_id = identifier(request["run_id"])
    if operation == "prepare":
        spec = request["spec"]
        identifier(spec["group_id"])
        identifier(spec["name"])
        if not re.fullmatch(r"[a-f0-9]{64}", spec["token"]):
            raise ValueError("invalid rendezvous token")
        number(spec["lease_seconds"], "lease_seconds", 15, 600)
        argv(spec["command"])
        existing = [job for job in store.jobs(include_history=True) if job["run_id"] == run_id]
        if existing:
            job = runner.own_job(config, store, run_id)
            if json.loads(metadata(job).read_text()) != spec:
                raise ValueError("participant ID already has different inputs")
            return {"run_id": run_id}

        def prepare(job):
            atomic_json(metadata(job), spec)
            control(job, spec, "wait")

        submit_job(
            config, experiment=spec["experiment"], resource=spec["resource"],
            description="tandem " + spec["group_id"] + ": " + spec["name"],
            command=runner.launcher() + ["--config", config["path"], "_tandem-member", run_id],
            cwd=spec["cwd"], expected_minutes=spec["expected_minutes"], run_id=run_id,
            parent_run=spec.get("parent_run"), prepare=prepare,
        )
        return {"run_id": run_id}
    job = runner.own_job(config, store, run_id)
    spec = json.loads(metadata(job).read_text())
    if not hmac.compare_digest(spec["token"], request.get("token", "")):
        raise PermissionError("participant token does not match")
    decision = request.get("decision", "wait")
    if operation not in {"poll", "cancel", "inspect"} or decision not in {"wait", "go"}:
        raise ValueError("unknown tandem operation")
    if operation == "cancel":
        control(job, spec, "abort")
        if store.cancel(run_id, os.getuid()):
            subprocess.run(["systemctl", "--user", "--no-block", "stop", job["unit"]],
                           check=True, capture_output=True, timeout=8)
    elif operation == "poll" and job["state"] not in TERMINAL and not job["cancel_requested"]:
        control(job, spec, decision)
    state_path = Path(job["run_dir"]) / "tandem-state.json"
    return {"job": store.get(run_id), "participant":
            json.loads(state_path.read_text()) if state_path.exists() else None}


def participant(config, run_id):
    """Hold the local reservation, then supervise payload while its lease lives."""
    store = Store(config["database"])
    job = runner.own_job(config, store, run_id)
    if os.environ.get("GPUQ_RUN_ID") != run_id or job["state"] != "running":
        raise RuntimeError("participant must run inside its active queue reservation")
    spec = json.loads(metadata(job).read_text())
    root = Path(job["run_dir"])
    stopping = False
    process = None

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    state = {"phase": "ready", "ready_at": now()}
    atomic_json(root / "tandem-state.json", state)
    try:
        while not stopping:
            lease = json.loads((root / "tandem-control.json").read_text())
            if not hmac.compare_digest(lease["token"], spec["token"]):
                raise PermissionError("control token does not match")
            if lease["decision"] == "abort" or time.monotonic() >= lease["deadline"]:
                raise RuntimeError("tandem aborted or coordinator lease expired")
            if process is None and lease["decision"] == "go":
                # The device may have acquired an out-of-queue owner while held.
                if not runner.guard_idle(config["resources"][job["resource"]]):
                    raise RuntimeError("resource became busy during rendezvous")
                env = {**os.environ, "GPUQ_TANDEM_ID": spec["group_id"],
                       "GPUQ_TANDEM_MEMBER": spec["name"]}
                process = subprocess.Popen(spec["command"], cwd=spec["cwd"], env=env)
                state.update({"phase": "executing", "started_at": now()})
                atomic_json(root / "tandem-state.json", state)
            if process is not None and process.poll() is not None:
                code = process.returncode
                state.update({"phase": "finished", "exit_code": code, "finished_at": now()})
                atomic_json(root / "tandem-state.json", state)
                return code if code >= 0 else 128 - code
            time.sleep(.2)
        return 130
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        # The normal worker/finalizer retains the slot through cgroup cleanup.


def owned_directory(path):
    path = Path(path).expanduser().resolve()
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise PermissionError("coordinator directory must be private and owned by you")
    return path


def submit(path):
    value = plan(json.loads(Path(path).read_text()))
    if os.getuid() == 0:
        raise PermissionError("submit as a non-root user")
    local_database(Path(value["state_root"]) / "placeholder")
    directory = Path(value["state_root"]) / ("tandem-" + new_id()[4:])
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    token = secrets.token_hex(32)
    for member in value["members"]:
        member["run_id"] = new_id()
    value.update({"group_id": directory.name, "token": token, "uid": os.getuid(),
                  "unit": "gpuq-" + directory.name + ".service", "phase": "submitted"})
    atomic_json(directory / "manifest.json", value)
    command = ["systemd-run", "--user", "--service-type=exec", "--unit=" + value["unit"],
               "--property=UMask=0077", "--property=KillMode=control-group",
               "--property=TimeoutStopSec=120s", "--expand-environment=no"]
    subprocess.run(command + runner.launcher() + ["_tandem-worker", str(directory)],
                   check=True, timeout=20)
    return directory


def coordinate(directory, call=remote):
    """Acquire in canonical order; release payloads only after all are ready."""
    directory = owned_directory(directory)
    value = json.loads((directory / "manifest.json").read_text())
    members = value["members"]
    stopping = False
    prepared = []

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def save(phase):
        value.update({"phase": phase, "updated_at": now()})
        atomic_json(directory / "manifest.json", value)

    def request(member, operation, **extra):
        return call(member, {"operation": operation, "run_id": member["run_id"],
                             "token": value["token"], **extra})

    try:
        identities = set()
        for member in members:
            identity = call(member, {"operation": "identity", "resource": member["resource"]})
            if identity.get("protocol") != 1:
                raise ValueError("unsupported tandem protocol")
            key = (identity["host"], identity["resource"])
            if key in identities:
                raise ValueError("two participants resolve to the same queue resource")
            identities.add(key)
            member["identity"] = list(key)
        members.sort(key=lambda m: m["identity"])
        deadline = time.monotonic() + value["wait_seconds"]
        save("waiting")
        while not stopping:
            if time.monotonic() >= deadline:
                raise RuntimeError("tandem deadline exceeded while waiting for queues")
            availability = [call(member, {"operation": "availability", "resource": member["resource"]})
                            for member in members]
            if all(reply["idle"] for reply in availability):
                break
            time.sleep(value["poll_seconds"])
        save("acquiring")
        released = False
        while not stopping:
            if time.monotonic() >= deadline:
                raise RuntimeError("tandem deadline exceeded")
            ready = True
            completed = True
            for member in prepared:
                reply = request(member, "poll", decision="go" if released else "wait")
                member["last_status"] = reply
                job = reply["job"]
                if job["state"] in {"failed", "cancelled", "needs_review"} or job["cancel_requested"]:
                    raise RuntimeError("participant failed or requires review: " + member["name"])
                completed = completed and job["state"] == "completed"
                ready = ready and job["state"] == "running" and reply["participant"] is not None and reply["participant"]["phase"] == "ready"
            if released:
                if completed:
                    save("completed")
                    return 0
            elif ready and len(prepared) < len(members):
                member = members[len(prepared)]
                spec = {"group_id": value["group_id"], "token": value["token"],
                        "name": member["name"], "experiment": value["experiment"],
                        "resource": member["resource"], "cwd": member["cwd"],
                        "command": member["command"], "expected_minutes": member.get("expected_minutes", 30),
                        "lease_seconds": value["lease_seconds"], "parent_run": member.get("parent_run")}
                # Persist identity before an RPC whose successful reply could be lost.
                prepared.append(member)
                member["prepared"] = True
                save("acquiring")
                request(member, "prepare", spec=spec)
            elif ready and len(prepared) == len(members):
                value["barrier"] = {"released_at": now(), "ready_members": [
                    {"name": m["name"], "run_id": m["run_id"], **m["last_status"]} for m in prepared
                ]}
                save("releasing")
                for member in prepared:
                    request(member, "poll", decision="go")
                released = True
                deadline = time.monotonic() + value["run_seconds"]
                save("executing")
            save(value["phase"])
            time.sleep(value["poll_seconds"])
        raise RuntimeError("tandem cancelled")
    except BaseException as error:
        value["error"] = str(error)
        failures = []
        for member in reversed(prepared):
            try:
                request(member, "cancel")
            except Exception as cleanup_error:
                failures.append({"name": member["name"], "error": str(cleanup_error)})
        # Stop is asynchronous: observe terminal local jobs before claiming drain.
        deadline = time.monotonic() + 80
        pending = list(prepared)
        while pending and not failures and time.monotonic() < deadline:
            remaining = []
            for member in pending:
                try:
                    reply = request(member, "inspect")
                    member["last_status"] = reply
                    if reply["job"]["state"] not in TERMINAL:
                        remaining.append(member)
                except Exception as cleanup_error:
                    failures.append({"name": member["name"], "error": str(cleanup_error)})
            pending = remaining
            if pending and not failures:
                time.sleep(1)
        if pending and not failures:
            failures.append({"error": "participant cleanup is not yet terminal",
                             "members": [m["name"] for m in pending]})
        value["cleanup_errors"] = failures
        save("needs_review" if failures else "cancelled" if stopping else "failed")
        return 1


def execute(args):
    if args.action == "_tandem-worker":
        return coordinate(args.directory)
    if args.tandem_action == "submit":
        print(submit(args.plan))
    elif args.tandem_action == "show":
        print((owned_directory(args.directory) / "manifest.json").read_text())
    elif args.tandem_action == "cancel":
        directory = owned_directory(args.directory)
        value = json.loads((directory / "manifest.json").read_text())
        subprocess.run(["systemctl", "--user", "stop", value["unit"]], check=True, timeout=130)
    return 0
