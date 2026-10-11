# SPDX-License-Identifier: MIT
import datetime as dt
import json
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from gpuq.archive import archive_run
from gpuq.config import account
from gpuq.store import Store
from gpuq.util import atomic_json


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.user = account()
        self.config = {"artifact_root": str(self.root / "runs"), "archive_root": str(self.root / "cold"),
                       "archive_mount": None, "host": "test-host"}
        self.store = Store(self.root / "db" / "queue.sqlite3")
        self.store.initialize()
        self.directory = self.root / "runs" / "demo" / "test-run"
        for folder in ("logs", "telemetry", "checkpoints"):
            (self.directory / folder).mkdir(parents=True)
        (self.directory / "logs/stdout.log").write_text("experiment output\n")
        (self.directory / "telemetry/gpu.jsonl").write_text('{"power": 40}\n')
        (self.directory / "checkpoints/best.pt").write_bytes(b"important checkpoint")
        atomic_json(self.directory / "manifest.json", {"run_id": "test-run"})
        self.store.submit(
            user=self.user, resource="gpu", description="Example", experiment_id="demo",
            run_id="test-run", command=["/bin/true"], cwd=self.root,
            run_dir=self.directory, expected_seconds=60,
        )
        self.store.finish("test-run", self.user["uid"], state="completed", exit_code=0)
        self.age(31)

    def age(self, days):
        value = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
        with self.store.connection(write=True) as db:
            db.execute("UPDATE jobs SET ended_at=? WHERE run_id='test-run'", (value,))

    def tearDown(self):
        self.temp.cleanup()

    def test_archives_old_logs_and_keeps_checkpoints_and_local_manifest(self):
        result = archive_run(self.config, self.store, "test-run", prune_local=True)
        destination = Path(result["destination"])
        self.assertEqual((destination / "logs/stdout.log").read_text(), "experiment output\n")
        self.assertFalse((self.directory / "logs/stdout.log").exists())
        self.assertTrue((self.directory / "checkpoints/best.pt").exists())
        self.assertTrue((self.directory / "manifest.json").exists())
        self.assertEqual(self.store.get("test-run")["archive_path"], str(destination))

    def test_checkpoint_archiving_requires_an_explicit_option(self):
        result = archive_run(self.config, self.store, "test-run",
                             include_checkpoints=True, prune_local=True)
        self.assertEqual((Path(result["destination"]) / "checkpoints/best.pt").read_bytes(),
                         b"important checkpoint")
        self.assertFalse((self.directory / "checkpoints/best.pt").exists())

    def test_cold_manifest_preserves_source_provenance_and_lifecycle(self):
        provenance = {"git_revision_at_start": "source-revision", "kernel": "test-kernel"}
        atomic_json(self.directory / "manifest.json", {"run_id": "test-run", "provenance": provenance})
        result = archive_run(self.config, self.store, "test-run", prune_local=True)
        saved = json.loads((Path(result["destination"]) / "manifest.json").read_text())
        self.assertEqual(saved["provenance"], provenance)
        self.assertEqual([event["event"] for event in saved["events"]], ["submitted", "completed"])

    def test_copy_failure_retains_every_local_source(self):
        from gpuq.archive import copy_verified
        counter = 0

        def fail_second(*args):
            nonlocal counter
            counter += 1
            if counter == 2:
                raise OSError("simulated NAS outage")
            return copy_verified(*args)

        with patch("gpuq.archive.copy_verified", side_effect=fail_second):
            with self.assertRaises(OSError):
                archive_run(self.config, self.store, "test-run", prune_local=True)
        self.assertTrue((self.directory / "logs/stdout.log").exists())
        self.assertTrue((self.directory / "telemetry/gpu.jsonl").exists())

    def test_destination_corruption_retains_local_source(self):
        destination = Path(self.config["archive_root"]) / "test-host" / self.user["user"] / "demo/test-run"
        (destination / "logs").mkdir(parents=True)
        (destination / "logs/stdout.log").write_text("different data")
        with self.assertRaises(ValueError):
            archive_run(self.config, self.store, "test-run", prune_local=True)
        self.assertTrue((self.directory / "logs/stdout.log").exists())

    def test_recent_pinned_or_running_runs_are_not_archived(self):
        self.age(1)
        with self.assertRaises(ValueError):
            archive_run(self.config, self.store, "test-run")
        self.age(31)
        self.store.pin("test-run", self.user["uid"], True)
        with self.assertRaises(ValueError):
            archive_run(self.config, self.store, "test-run")
        self.store.pin("test-run", self.user["uid"], False)
        with self.store.connection(write=True) as db:
            db.execute("UPDATE jobs SET state='running' WHERE run_id='test-run'")
        with self.assertRaises(ValueError):
            archive_run(self.config, self.store, "test-run")

    def test_symlink_cannot_archive_an_unrelated_file(self):
        outside = self.root / "unrelated.txt"
        outside.write_text("retain")
        (self.directory / "logs/link").symlink_to(outside)
        with self.assertRaises(ValueError):
            archive_run(self.config, self.store, "test-run", prune_local=True)
        self.assertEqual(outside.read_text(), "retain")

    def test_dry_run_does_not_copy_or_delete(self):
        result = archive_run(self.config, self.store, "test-run", dry_run=True, prune_local=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse(Path(result["destination"]).exists())
        self.assertTrue((self.directory / "logs/stdout.log").exists())

    def test_repeated_archive_preserves_previous_checksums(self):
        result = archive_run(self.config, self.store, "test-run", prune_local=True)
        archive_run(self.config, self.store, "test-run", include_checkpoints=True, prune_local=True)
        saved = json.loads((Path(result["destination"]) / "manifest.json").read_text())
        self.assertIn("logs/stdout.log", saved["files"])
        self.assertIn("checkpoints/best.pt", saved["files"])

    def test_unmounted_archive_keeps_local_files(self):
        mount = self.root / "cold"
        mount.mkdir()
        self.config["archive_mount"] = str(mount)
        with patch("gpuq.archive.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1, "", "")):
            with self.assertRaises(ValueError):
                archive_run(self.config, self.store, "test-run", prune_local=True)
        self.assertTrue((self.directory / "logs/stdout.log").exists())

    def test_source_changed_during_copy_is_not_pruned(self):
        from gpuq.archive import copy_verified

        def mutate(source, destination, checksum):
            copy_verified(source, destination, checksum)
            source.write_text("changed after copy")

        with patch("gpuq.archive.copy_verified", side_effect=mutate):
            with self.assertRaises(ValueError):
                archive_run(self.config, self.store, "test-run", prune_local=True)
        self.assertTrue((self.directory / "logs/stdout.log").exists())
        self.assertTrue((self.directory / "telemetry/gpu.jsonl").exists())
        self.assertIsNone(self.store.get("test-run")["archive_path"])


if __name__ == "__main__":
    unittest.main()
