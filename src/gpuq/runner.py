# SPDX-License-Identifier: MIT
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import account, run_path
from .store import PREEMPT_STOP, Store, now
from .telemetry import Telemetry
from .util import atomic_json


def own_job(config, store, run_id):
    job = store.get(run_id)
    user = account()
    if user["uid"] == 0:
        raise PermissionError("experiments must run under a non-root user")
    if job["uid"] != user["uid"]:
        raise PermissionError("run belongs to a different user")
    expected = run_path(config, user, job["experiment_id"], run_id)
    if Path(job["run_dir"]).resolve() != expected:
        raise ValueError("run directory does not match the configured artifact root")
    return job


def guard_idle(resource):
    result = subprocess.run(resource["guard"], capture_output=True, text=True, timeout=10)
    mode = resource["guard_mode"]
    if mode == "nvidia":
        if result.returncode:
            raise RuntimeError("GPU guard failed: " + result.stderr.strip())
        return not result.stdout.strip()
    if mode == "fuser":
        if result.returncode == 0:
            return False
        if result.returncode == 1 and not result.stderr.strip() and not result.stdout.strip():
            return True
        raise RuntimeError("GPU process visibility failed: " + result.stderr.strip())
    if mode == "exit-zero-idle":
        if result.returncode not in (0, 1):
            raise RuntimeError("custom guard failed")
        return result.returncode == 0
    raise ValueError("unsupported guard")


def launcher():
    executable = Path(sys.argv[0]).resolve()
    if executable.name == "gpuq" and executable.is_file():
        return [sys.executable, str(executable)]
    return [sys.executable, "-m", "gpuq"]


def unit_argument(value):
    # systemd Exec* values have their own quoting/specifier rules, not shell parsing.
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return '"' + text.replace("$", "$$").replace("%", "%%") + '"'


def launch(config, job):
    finalizer = launcher() + ["--config", config["path"], "_finalize", job["run_id"]]
    worker = launcher() + ["--config", config["path"], "_worker", job["run_id"]]
    command = [
        "systemd-run", "--user", "--service-type=exec",
        "--unit=" + job["unit"], "--description=gpuq: " + job["description"],
        "--property=UMask=0007", "--property=KillMode=control-group",
        "--property=TimeoutStopSec=30s",
        "--property=ExecStopPost=" + " ".join(unit_argument(value) for value in finalizer),
        "--expand-environment=no",
    ] + worker
    # No shell and no uid impersonation: the user's own manager starts the service.
    subprocess.run(command, check=True, timeout=20)


def preempt_file(job):
    return Path(job["run_dir"]) / "preempt.json"


def limit_reached(job, elapsed, idle):
    """Which of the run's own limits, if any, ends this attempt: "timeout" once the payload has run
    timeout_seconds, "idle" once the device has stayed at or below idle_percent for idle_seconds.
    An unknown idle time (no telemetry) never triggers the idle limit."""
    if job.get("timeout_seconds") and elapsed >= job["timeout_seconds"]:
        return "timeout"
    if job.get("idle_seconds") and idle is not None and idle >= job["idle_seconds"]:
        return "idle"
    return None


def manifest(config, store, run_id):
    job = store.get(run_id)
    directory = Path(job["run_dir"])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "manifest.json"
    previous = json.loads(path.read_text()) if path.exists() else {}
    provenance = previous.get("provenance", {})
    if job["started_at"] and not provenance:
        revision_text = None
        try:
            revision = subprocess.run(["git", "-C", job["cwd"], "rev-parse", "HEAD"],
                                      capture_output=True, text=True, timeout=5)
            if revision.returncode == 0:
                revision_text = revision.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        provenance = {
            "git_revision_at_start": revision_text,
            "worker_python": sys.version,
            "kernel": os.uname().release,
            "resource_environment": config["resources"][job["resource"]].get("environment", {}),
        }
    atomic_json(path, {
        **previous,
        **job,
        "host": config["host"],
        "provenance": provenance,
        "events": store.events(run_id),
    })


def worker(config, run_id):
    store = Store(config["database"])
    job = own_job(config, store, run_id)
    resource = config["resources"][job["resource"]]
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    directory = Path(job["run_dir"])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs = directory / "logs"
    logs.mkdir(exist_ok=True)
    monitor = None
    process = None
    exit_code = 1
    preempting = 0
    limit = None
    # Preemption notice: the job's signal (if any) goes to the payload's main process only, and
    # $GPUQ_PREEMPT_FILE appears for payloads that poll. A shell wrapper must trap and forward
    # the signal (or exec its program): an untrapped SIGUSR1 terminates bash.
    preempt_signal = job["preempt_signal"]
    try:
        print(f"gpuq {run_id}: waiting for {job['resource']}", flush=True)
        while not stopping:
            job = store.get(run_id)
            if job["cancel_requested"]:
                exit_code = 130
                return 130
            store.heartbeat(run_id, os.getuid())
            if guard_idle(resource) and store.claim(run_id, os.getuid()):
                break
            preempted = store.preempt_for(run_id, os.getuid())
            if preempted:
                print(f"gpuq {run_id}: asked low-priority {preempted} to yield", flush=True)
            time.sleep(config["poll_seconds"])
        if stopping:
            exit_code = 130
            return 130
        job = store.get(run_id)
        # A second process check catches an unregistered job racing the queue.
        if not guard_idle(resource):
            raise RuntimeError("GPU became occupied before launch")
        manifest(config, store, run_id)
        monitor = Telemetry(config, job)
        monitor.start()
        if job["idle_seconds"] and monitor.thread is None:
            print(f"gpuq {run_id}: no telemetry, so the idle limit cannot be enforced", flush=True)
        environment = dict(os.environ)
        environment.update(resource.get("environment", {}))
        environment.update({
            "GPUQ_RUN_ID": run_id, "GPUQ_EXPERIMENT_ID": job["experiment_id"],
            "GPUQ_RUN_DIR": job["run_dir"], "GPUQ_PRIORITY": job["priority"],
            "GPUQ_PREEMPT_FILE": str(preempt_file(job)), "GPUQ_PREEMPT_SIGNAL": preempt_signal,
            "GPUQ_ATTEMPT": str(job["requeues"] + 1),
        })
        # A notice from an earlier attempt must not stop this one.
        preempt_file(job).unlink(missing_ok=True)
        print(f"gpuq {run_id}: running {job['command'][0]}", flush=True)
        with (logs / "stdout.log").open("ab") as stdout, (logs / "stderr.log").open("ab") as stderr:
            process = subprocess.Popen(
                job["command"], cwd=job["cwd"], env=environment,
                stdout=stdout, stderr=stderr,
            )
            last_heartbeat = 0
            deadline = None
            launched = time.monotonic()
            while process.poll() is None:
                if time.monotonic() - last_heartbeat >= config["poll_seconds"]:
                    store.heartbeat(run_id, os.getuid())
                    last_heartbeat = time.monotonic()
                    current = store.get(run_id)
                    stopping = stopping or bool(current["cancel_requested"])
                    if current["preempt_requested"] > preempting:
                        preempting = current["preempt_requested"]
                        grace = current["preempt_grace_seconds"]
                        deadline = deadline or time.monotonic() + grace
                        atomic_json(preempt_file(job), {
                            "requested_at": now(), "grace_seconds": grace,
                            "mode": "stop" if preempting == PREEMPT_STOP else "requeue",
                        })
                        if preempt_signal != "none":
                            with contextlib.suppress(ProcessLookupError):
                                process.send_signal(signal.Signals[preempt_signal])
                        print(f"gpuq {run_id}: preemption requested; {grace:.0f} s to checkpoint", flush=True)
                if deadline is not None and time.monotonic() > deadline:
                    print(f"gpuq {run_id}: preemption grace expired; terminating", flush=True)
                    stopping = stopping or "grace"
                # Once a preemption notice arrives its grace period decides, so the run is requeued.
                if not stopping and not preempting:
                    limit = limit_reached(job, time.monotonic() - launched, monitor.idle_seconds())
                    if limit:
                        if limit == "timeout":
                            detail = {"timeout_seconds": job["timeout_seconds"]}
                        else:
                            detail = {"idle_seconds": job["idle_seconds"], "idle_percent": job["idle_percent"]}
                        store.record(run_id, os.getuid(), "limit_reached", {"limit": limit, **detail})
                        print(f"gpuq {run_id}: {limit} limit reached; terminating", flush=True)
                        stopping = limit
                if stopping:
                    process.terminate()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    break
                time.sleep(min(config["poll_seconds"], 0.5))
            exit_code = process.returncode
        if stopping is True:
            exit_code = 130
        return max(0, exit_code) if exit_code >= 0 else 128 - exit_code
    finally:
        if monitor:
            monitor.stop()
        atomic_json(directory / "outcome.json", {
            "command_exit_code": exit_code, "worker_finished_at": now(),
            "cancelled": stopping is True, "pid": process.pid if process else None,
            "preempted": bool(preempting), "attempt": job["requeues"] + 1, "limit": limit,
        })
        # The slot is deliberately NOT released here: systemd kills remaining
        # service children before running the stop-post finalizer.


def finalize(config, run_id):
    store = Store(config["database"])
    job = own_job(config, store, run_id)
    if "SERVICE_RESULT" not in os.environ or not os.environ.get("INVOCATION_ID"):
        raise RuntimeError("finalizer must run as systemd ExecStopPost")
    show = subprocess.run(
        ["systemctl", "--user", "show", job["unit"], "-p", "SubState", "--value"],
        text=True, capture_output=True, check=True, timeout=10,
    ).stdout.strip()
    if show != "stop-post":
        raise RuntimeError("service is not in stop-post; refusing to release the slot")
    # KillMode=control-group has drained the experiment's process tree at this point.
    outcome_path = Path(job["run_dir"]) / "outcome.json"
    outcome = json.loads(outcome_path.read_text()) if outcome_path.exists() else {}
    result = os.environ["SERVICE_RESULT"]
    exit_code = outcome.get("command_exit_code")
    if job["cancel_requested"] or outcome.get("cancelled"):
        state = "cancelled"
    elif outcome.get("limit"):
        # A run its own limit ended has failed, whatever exit code the terminated payload chose.
        state, result = "failed", outcome["limit"]
    elif result == "success" and exit_code == 0:
        state = "completed"
    elif outcome.get("preempted") and job["preempt_requested"] == PREEMPT_STOP:
        state = "cancelled"
    elif outcome.get("preempted"):
        return requeue(config, store, job)
    else:
        state = "failed"
    store.finish(run_id, os.getuid(), state=state, exit_code=exit_code, service_result=result)
    manifest(config, store, run_id)
    print(f"gpuq {run_id}: {state}", flush=True)


def requeue(config, store, job):
    """A preempted run goes back to the queue under a fresh unit (this one is still in stop-post),
    in its own run directory, so the payload can resume from $GPUQ_RUN_DIR/checkpoints."""
    run_id = job["run_id"]
    store.requeue(run_id, os.getuid(), f"gpuq-{run_id}-r{job['requeues'] + 1}.service")
    manifest(config, store, run_id)
    job = store.get(run_id)
    try:
        launch(config, job)
    except Exception:
        check = subprocess.run(
            ["systemctl", "--user", "show", job["unit"], "-p", "LoadState", "--value"],
            text=True, capture_output=True, timeout=10,
        )
        if check.returncode == 0 and check.stdout.strip() == "not-found":
            store.finish(run_id, os.getuid(), state="failed", service_result="requeue-launch-failed")
        else:
            store.review(run_id, os.getuid(), "requeued service launch result was ambiguous")
        manifest(config, store, run_id)
        raise
    print(f"gpuq {run_id}: preempted and requeued as {job['unit']}", flush=True)
