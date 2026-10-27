# SPDX-License-Identifier: MIT
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpuq import tandem
from gpuq.config import account
from gpuq.store import Store
from gpuq.util import atomic_json


class TandemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "db/queue.sqlite3")
        self.store.initialize()
        self.config = {"database": str(self.store.path), "artifact_root": str(self.root / "runs"),
                       "resources": {"gpu": {"guard": ["/bin/true"], "guard_mode": "exit-zero-idle"}},
                       "host": "node-a", "path": str(self.root / "config.json")}
        self.spec = {"group_id": "tandem-test", "name": "encoder", "experiment": "example",
                     "resource": "gpu", "command": ["/bin/true"], "cwd": str(self.root),
                     "expected_minutes": 1, "lease_seconds": 90, "token": "a" * 64}

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        with patch("gpuq.cli.runner.launch"), contextlib.redirect_stdout(io.StringIO()):
            return tandem.rpc(self.config, {"operation": "prepare", "run_id": "member-1",
                                            "spec": self.spec})

    def test_prepare_is_idempotent_and_rejects_changed_payload(self):
        self.prepare()
        self.prepare()
        self.assertEqual(len(self.store.jobs()), 1)
        self.spec = {**self.spec, "command": ["/bin/false"]}
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(len(self.store.jobs()), 1)

    def test_pending_jobs_prevent_admission(self):
        self.assertEqual(tandem.rpc(self.config, {"operation": "availability", "resource": "gpu"}), {"idle": True})
        self.prepare()
        self.assertEqual(tandem.rpc(self.config, {"operation": "availability", "resource": "gpu"}), {"idle": False})
        self.store.finish("member-1", os.getuid(), state="failed")
        self.assertEqual(tandem.rpc(self.config, {"operation": "availability", "resource": "gpu"}), {"idle": True})

    def test_token_mismatch_cannot_release_or_cancel(self):
        self.prepare()
        for operation in ("poll", "cancel"):
            with self.assertRaises(PermissionError):
                tandem.rpc(self.config, {"operation": operation, "run_id": "member-1",
                                         "token": "b" * 64, "decision": "go"})
        self.assertEqual(self.store.get("member-1")["state"], "queued")
        control = json.loads((Path(self.store.get("member-1")["run_dir"]) / "tandem-control.json").read_text())
        self.assertEqual(control["decision"], "wait")

    def test_foreign_owner_cannot_renew_member(self):
        self.prepare()
        with patch("gpuq.runner.account", return_value={**account(), "uid": os.getuid() + 1}):
            with self.assertRaises(PermissionError):
                tandem.rpc(self.config, {"operation": "poll", "run_id": "member-1",
                                         "token": self.spec["token"]})

    def test_participant_refuses_manual_execution(self):
        self.prepare()
        with self.assertRaises(RuntimeError):
            tandem.participant(self.config, "member-1")

    def test_expired_lease_prevents_payload_start(self):
        self.prepare()
        self.store.claim("member-1", os.getuid())
        job = self.store.get("member-1")
        control = Path(job["run_dir"]) / "tandem-control.json"
        atomic_json(control, {"token": self.spec["token"], "decision": "go", "deadline": -1})
        with patch.dict(os.environ, GPUQ_RUN_ID="member-1"), patch("gpuq.tandem.signal.signal"), patch(
            "gpuq.tandem.subprocess.Popen"
        ) as launch:
            with self.assertRaises(RuntimeError):
                tandem.participant(self.config, "member-1")
            launch.assert_not_called()
        self.assertEqual(self.store.get("member-1")["state"], "running")

    def coordinator(self, fail=False, duplicate=False):
        directory = self.root / "coordinator"
        directory.mkdir(mode=0o700)
        members = [{"name": "later", "run_id": "run-b", "resource": "gpu", "cwd": str(self.root),
                    "command": ["/bin/true"], "identity_source": "node-b"},
                   {"name": "earlier", "run_id": "run-a", "resource": "gpu", "cwd": str(self.root),
                    "command": ["/bin/true"], "identity_source": "node-a"}]
        atomic_json(directory / "manifest.json", {"members": members, "group_id": "group-1",
            "token": "a" * 64, "experiment": "example", "lease_seconds": 90,
            "wait_seconds": 5, "run_seconds": 5, "poll_seconds": .001})
        prepared = []
        released = set()
        calls = []
        polls = {}

        availability = {}

        def call(member, request):
            operation = request["operation"]
            calls.append((member["name"], operation, request.get("decision")))
            if operation == "identity":
                return {"host": "node-a" if duplicate else member["identity_source"], "resource": "gpu", "protocol": 1}
            if operation == "availability":
                availability[member["name"]] = availability.get(member["name"], 0) + 1
                return {"idle": availability[member["name"]] > 1}
            if operation == "prepare":
                self.assertTrue(all(count >= 2 for count in availability.values()))
                self.assertEqual(len(availability), 2)
                prepared.append(member["name"])
                return {"run_id": member["run_id"]}
            if operation == "cancel":
                return {}
            if operation == "inspect":
                return {"job": {"state": "cancelled"}}
            if request["decision"] == "go":
                self.assertEqual(prepared, ["earlier", "later"])
                # Every member must have reported ready before the first release.
                self.assertGreaterEqual(polls.get("later", 0), 2)
                released.add(member["name"])
            polls[member["name"]] = polls.get(member["name"], 0) + 1
            state = "completed" if len(released) == 2 else "running"
            if fail and member["name"] == "later":
                state = "failed"
            ready = None if member["name"] == "later" and polls[member["name"]] == 1 else {"phase": "ready"}
            return {"job": {"state": state, "cancel_requested": False}, "participant": ready}

        with patch("gpuq.tandem.signal.signal"):
            code = tandem.coordinate(directory, call=call)
        return code, calls, json.loads((directory / "manifest.json").read_text())

    def test_orders_claims_and_waits_for_all_before_release(self):
        code, calls, result = self.coordinator()
        self.assertEqual(code, 0)
        self.assertEqual(result["phase"], "completed")
        self.assertEqual([name for name, op, _ in calls if op == "prepare"], ["earlier", "later"])

    def test_failure_cancels_only_group_participants(self):
        code, calls, result = self.coordinator(fail=True)
        self.assertEqual(code, 1)
        self.assertEqual(result["phase"], "failed")
        self.assertEqual([name for name, op, _ in calls if op == "cancel"], ["later", "earlier"])
        self.assertFalse(any(decision == "go" for _, _, decision in calls))

    def test_duplicate_remote_resource_rejected_before_submission(self):
        code, calls, result = self.coordinator(duplicate=True)
        self.assertEqual(code, 1)
        self.assertFalse(any(op == "prepare" for _, op, _ in calls))

    def test_lost_prepare_reply_still_cancels_known_member(self):
        directory = self.root / "lost-reply"
        directory.mkdir(mode=0o700)
        members = [{"name": name, "run_id": "run-" + name, "resource": "gpu",
                    "cwd": str(self.root), "command": ["/bin/true"]} for name in ("a", "b")]
        atomic_json(directory / "manifest.json", {"members": members, "group_id": "group-1",
            "token": "a" * 64, "experiment": "example", "lease_seconds": 90,
            "wait_seconds": 5, "run_seconds": 5, "poll_seconds": .001})
        cancelled = []

        def call(member, request):
            if request["operation"] == "identity":
                return {"host": member["name"], "resource": "gpu", "protocol": 1}
            if request["operation"] == "availability":
                return {"idle": True}
            if request["operation"] == "prepare":
                raise TimeoutError("reply lost after remote submission")
            if request["operation"] == "cancel":
                cancelled.append(member["run_id"])
                return {}
            return {"job": {"state": "cancelled"}}

        with patch("gpuq.tandem.signal.signal"):
            self.assertEqual(tandem.coordinate(directory, call=call), 1)
        result = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(cancelled, ["run-a"])
        self.assertEqual(result["phase"], "failed")

    def test_expiry_stops_an_already_running_payload_without_freeing_slot(self):
        self.prepare()
        self.store.claim("member-1", os.getuid())
        job = self.store.get("member-1")
        control = Path(job["run_dir"]) / "tandem-control.json"
        atomic_json(control, {"token": self.spec["token"], "decision": "go", "deadline": 100})
        with patch.dict(os.environ, GPUQ_RUN_ID="member-1"), patch("gpuq.tandem.signal.signal"), patch(
            "gpuq.tandem.time.monotonic", side_effect=[0, 101]
        ), patch("gpuq.tandem.time.sleep"), patch("gpuq.runner.guard_idle", return_value=True), patch("gpuq.tandem.subprocess.Popen") as launch:
            launch.return_value.poll.return_value = None
            with self.assertRaises(RuntimeError):
                tandem.participant(self.config, "member-1")
            launch.assert_called_once()
            launch.return_value.terminate.assert_called_once()
            launch.return_value.wait.assert_called_once_with(timeout=3)
        self.assertEqual(self.store.get("member-1")["state"], "running")

    def test_plan_rejects_shell_options_duplicate_names_and_short_lease(self):
        member = {"name": "first", "resource": "gpu", "command": ["/bin/true"], "cwd": "/work"}
        base = {"experiment": "example", "state_root": str(self.root), "members": [member, {**member, "name": "second"}]}
        for bad in ({"ssh": "-oProxyCommand=bad"}, {"cwd": "relative"}, {"command": "echo bad"}, {"name": "second"}):
            value = {**base, "members": [{**member, **bad}, {**member, "name": "second"}]}
            with self.assertRaises(ValueError):
                tandem.plan(value)
        with self.assertRaises(ValueError):
            tandem.plan({**base, "lease_seconds": 20})


if __name__ == "__main__":
    unittest.main()
