# SPDX-License-Identifier: MIT
import json
import argparse
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gpuq import motd
from gpuq.runner import guard_idle
from gpuq.telemetry import Telemetry
from gpuq.store import Store
from gpuq.cli import execute


class ComponentTests(unittest.TestCase):
    def test_submission_preserves_virtualenv_interpreter_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            interpreter = root / "venv/bin/python"
            interpreter.parent.mkdir(parents=True)
            interpreter.symlink_to("/usr/bin/python3")
            store = Store(root / "state/queue.sqlite3")
            store.initialize()
            config = {"database": str(store.path), "artifact_root": str(root / "runs"),
                      "resources": {"gpu": {}}, "host": "test", "path": "/example/config.json"}
            args = argparse.Namespace(action="submit", config="/example/config.json",
                                      resource="gpu", experiment="demo", description="test",
                                      command=[str(interpreter), "-V"], cwd=str(root),
                                      expected_minutes=1, parent_run=None)
            with patch("gpuq.cli.load", return_value=config), patch(
                "gpuq.cli.account", return_value={"uid": 1000, "user": "alice", "home": temp}
            ), patch("gpuq.cli.runner.launch"), patch("builtins.print"):
                execute(args)
            self.assertEqual(store.jobs()[0]["command"][0], str(interpreter))

    def test_gpu_guard_distinguishes_busy_idle_and_errors(self):
        resource = {"guard": ["/bin/true"], "guard_mode": "nvidia"}
        with patch("gpuq.runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertTrue(guard_idle(resource))
        with patch("gpuq.runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "123\n", "")):
            self.assertFalse(guard_idle(resource))
        with patch("gpuq.runner.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "NVML failed")):
            with self.assertRaises(RuntimeError):
                guard_idle(resource)
        resource["guard_mode"] = "fuser"
        with patch("gpuq.runner.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "permission denied")):
            with self.assertRaises(RuntimeError):
                guard_idle(resource)

    def test_motd_preserves_other_text_and_sanitizes_descriptions(self):
        jobs = [{
            "seq": 1, "username": "alice", "state": "queued", "resource": "gpu",
            "run_id": "test-run", "description": "a\nb\033[31m|malicious",
            "started_at": None, "expected_end": None, "expected_seconds": 60,
        }]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "motd"
            path.write_text("Existing administrator notice.\n")
            block = motd.render(jobs)
            motd.update(path, block)
            first = path.read_text()
            motd.update(path, block)
            self.assertEqual(path.read_text(), first)
            self.assertTrue(first.startswith("Existing administrator notice.\n"))
            self.assertNotIn("\033", first)
            self.assertIn("alice | ", first)
            self.assertEqual(first.count(motd.BEGIN), 1)

    def test_streaming_telemetry_uses_jsonl_and_constant_size_aggregates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            module = root / "fake_gputop.py"
            module.write_text("""
class GPUMonitor:
    initialized = True
    gpus = [{"name": "Fake GPU", "type": "fake"}]
    def get_stats(self, index):
        return {"name": "Fake GPU", "power": 10, "utilization": 25, "memory_used": 5}
    def cleanup(self):
        pass
""")
            config = {"host": "test", "telemetry_seconds": 0.02,
                      "resources": {"gpu": {"gputop_module": str(module)}}}
            job = {"run_dir": str(root), "resource": "gpu", "run_id": "test-run",
                   "experiment_id": "demo"}
            monitor = Telemetry(config, job)
            monitor.start()
            time.sleep(0.12)
            monitor.stop()
            samples = [json.loads(line) for line in (root / "telemetry/gpu.jsonl").read_text().splitlines()]
            summary = json.loads((root / "telemetry/summary.json").read_text())
            self.assertGreaterEqual(len(samples), 3)
            self.assertEqual(summary["sample_count"], len(samples))
            self.assertEqual(summary["mean"]["power"], 10)
            self.assertEqual(len(monitor.sums), 3)
            self.assertGreater(summary["estimated_energy_j"], 0)
            self.assertEqual(samples[0]["scope"], "device")
            self.assertTrue(samples[0]["timestamp_utc"].endswith("+00:00"))

    def test_required_telemetry_failure_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {"host": "test", "telemetry_seconds": 1,
                      "resources": {"gpu": {"gputop_module": "/nonexistent/module.py",
                                            "telemetry_required": True}}}
            job = {"run_dir": temp, "resource": "gpu", "run_id": "test-run", "experiment_id": "demo"}
            with self.assertRaises(RuntimeError):
                Telemetry(config, job).start()

    def test_required_telemetry_cannot_be_silently_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {"resources": {"gpu": {"telemetry_required": True}}}
            job = {"run_dir": temp, "resource": "gpu"}
            with self.assertRaises(RuntimeError):
                Telemetry(config, job).start()


if __name__ == "__main__":
    unittest.main()
