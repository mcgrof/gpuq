# SPDX-License-Identifier: MIT
import importlib.util
import json
import threading
import time
from pathlib import Path

from .snapshot import publish_live
from .store import now
from .util import atomic_json


class Telemetry:
    """Reuse gputop's monitor, without its terminal UI or growing JSON array."""

    def __init__(self, config, job):
        self.config = config
        self.job = job
        self.stop_event = threading.Event()
        self.thread = None
        self.monitor = None
        self.started = time.monotonic()
        self.count = 0
        self.sums = {}
        self.valid_counts = {}
        self.maximum = {}
        self.minimum = {}
        self.energy_j = 0.0
        self.previous = None
        self.error = None
        self.device = {}
        self.directory = Path(job["run_dir"]) / "telemetry"
        self.directory.mkdir(exist_ok=True)

    def start(self):
        resource = self.config["resources"][self.job["resource"]]
        source = resource.get("gputop_module", self.config.get("gputop_module"))
        if not source:
            if resource.get("telemetry_required", False):
                raise RuntimeError("required telemetry needs a configured gputop module")
            return
        try:
            spec = importlib.util.spec_from_file_location("gpuq_gputop", source)
            if spec is None or spec.loader is None:
                raise ValueError("gputop module cannot be loaded")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.monitor = module.GPUMonitor()
            if not self.monitor.initialized:
                raise ValueError("gputop did not detect a GPU")
            index = int(resource.get("telemetry_index", 0))
            gpu = self.monitor.gpus[index]
            self.device = {"index": index, "name": gpu["name"], "type": gpu["type"]}
            if gpu["type"] == "nvidia_desktop":
                import pynvml
                self.device["uuid"] = pynvml.nvmlDeviceGetUUID(gpu["handle"])
                bus = pynvml.nvmlDeviceGetPciInfo(gpu["handle"]).busId
                self.device["pci_bus"] = bus.decode() if isinstance(bus, bytes) else bus
            elif gpu["type"] == "amd":
                self.device["pci_bus"] = Path(gpu["device_path"]).resolve().name
            expected = resource.get("gpu_uuid")
            if expected and self.device.get("uuid") != expected:
                raise ValueError("telemetry device UUID does not match the reserved GPU")
            self.thread = threading.Thread(target=self.collect, daemon=True)
            self.thread.start()
        except Exception as error:
            self.error = str(error)
            if self.monitor is not None:
                self.monitor.cleanup()
                self.monitor = None
            if resource.get("telemetry_required", False):
                raise RuntimeError("required telemetry failed: " + self.error) from error

    def collect(self):
        try:
            with (self.directory / "gpu.jsonl").open("a") as file:
                while not self.stop_event.is_set():
                    monotonic = time.monotonic()
                    stats = self.monitor.get_stats(self.device["index"])
                    sample = {
                        "timestamp_utc": now(),
                        "elapsed_seconds": monotonic - self.started,
                        "run_id": self.job["run_id"],
                        "experiment_id": self.job["experiment_id"],
                        "host": self.config["host"],
                        "resource": self.job["resource"],
                        "scope": "device",
                        "device": self.device,
                        "stats": stats,
                    }
                    file.write(json.dumps(sample, sort_keys=True) + "\n")
                    file.flush()
                    try:
                        publish_live(self.config, self.job["resource"], sample)
                    except Exception:
                        pass  # Best effort: the run's own JSONL is the durable record.
                    self.count += 1
                    for key in ("utilization", "memory_used", "power"):
                        value = stats.get(key)
                        if isinstance(value, (float, int)):
                            self.sums[key] = self.sums.get(key, 0.0) + value
                            self.valid_counts[key] = self.valid_counts.get(key, 0) + 1
                            self.maximum[key] = max(self.maximum.get(key, value), value)
                            self.minimum[key] = min(self.minimum.get(key, value), value)
                    power = stats.get("power")
                    if isinstance(power, (int, float)):
                        if self.previous:
                            previous_time, previous_power = self.previous
                            self.energy_j += (previous_power + power) / 2 * (monotonic - previous_time)
                        self.previous = monotonic, power
                    self.stop_event.wait(self.config["telemetry_seconds"])
                file.flush()
        except Exception as error:
            self.error = str(error)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)
            if self.thread.is_alive():
                raise RuntimeError("telemetry collector did not stop")
        if self.monitor is not None:
            self.monitor.cleanup()
        atomic_json(self.directory / "summary.json", {
            "run_id": self.job["run_id"],
            "experiment_id": self.job["experiment_id"],
            "scope": "device",
            "device": self.device,
            "sample_count": self.count,
            "duration_seconds": time.monotonic() - self.started,
            "mean": {key: value / self.valid_counts[key] for key, value in self.sums.items()},
            "valid_sample_counts": self.valid_counts,
            "max": self.maximum,
            "min": self.minimum,
            "estimated_energy_j": self.energy_j if self.count > 1 else None,
            "error": self.error,
            "enabled": bool(self.config.get("gputop_module") or
                            self.config["resources"][self.job["resource"]].get("gputop_module")),
        })
