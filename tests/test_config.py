# SPDX-License-Identifier: MIT
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpuq.archive import mounted_archive
from gpuq.cli import execute, parser
from gpuq.config import load


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "private-config.json"
        self.data = {
            "host": "deployment-alias",
            "database": str(self.root / "state/queue.sqlite3"),
            "artifact_root": str(self.root / "artifacts/{user}"),
            "resources": {"gpu": {"guard_mode": "exit-zero-idle", "guard": ["true"]}},
        }

    def tearDown(self):
        self.temp.cleanup()

    def load(self):
        self.path.write_text(json.dumps(self.data))
        return load(self.path)

    def test_deployment_identity_and_paths_are_explicit(self):
        config = self.load()
        self.assertEqual(config["host"], "deployment-alias")
        self.assertEqual(config["database"], self.data["database"])
        self.assertEqual(config["artifact_root"], self.data["artifact_root"])
        self.assertIsNone(config["archive_root"])
        with self.assertRaisesRegex(ValueError, "archival is disabled"):
            mounted_archive(config)

    def test_missing_required_settings_have_no_implicit_deployment_defaults(self):
        for key in ("host", "database", "artifact_root"):
            with self.subTest(key=key):
                value = self.data.pop(key)
                with self.assertRaisesRegex(ValueError, key):
                    self.load()
                self.data[key] = value

    def test_archive_enabling_requires_explicit_mount_policy(self):
        self.data["archive_root"] = str(self.root / "archive/experiments")
        with self.assertRaisesRegex(ValueError, "archive_mount"):
            self.load()
        self.data["archive_mount"] = None
        self.assertEqual(self.load()["archive_root"], self.data["archive_root"])
        self.data["archive_mount"] = str(self.root / "archive")
        self.assertEqual(self.load()["archive_mount"], self.data["archive_mount"])
        self.data["archive_mount"] = str(self.root / "different-mount")
        with self.assertRaises(ValueError):
            self.load()

    def test_invalid_relative_paths_are_rejected(self):
        for key in ("database", "artifact_root", "archive_root", "archive_mount"):
            with self.subTest(key=key):
                values = dict(self.data)
                self.data["archive_root"] = str(self.root / "archive/experiments")
                self.data["archive_mount"] = str(self.root / "archive")
                self.data[key] = "relative/directory"
                with self.assertRaisesRegex(ValueError, key):
                    self.load()
                self.data = values

    def test_config_selection_needs_an_explicit_path_or_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            args = parser().parse_args(["status"])
            with self.assertRaisesRegex(ValueError, "--config"):
                execute(args)
        with patch.dict(os.environ, {"GPUQ_CONFIG": str(self.path)}):
            self.assertEqual(parser().parse_args(["status"]).config, str(self.path))
            args = parser().parse_args(["--config", "chosen-config.json", "status"])
            self.assertEqual(args.config, "chosen-config.json")

    def test_archival_without_configuration_cannot_reach_queue_or_storage(self):
        self.load()
        args = parser().parse_args(["--config", str(self.path), "archive", "--dry-run"])
        with self.assertRaisesRegex(ValueError, "archival is disabled"):
            execute(args)
        self.assertFalse((self.root / "state").exists())

    def test_poll_intervals_must_be_finite_and_positive(self):
        for key in ("poll_seconds", "telemetry_seconds"):
            for value in (0, -1, float("nan"), float("inf")):
                with self.subTest(key=key, value=value):
                    self.data[key] = value
                    with self.assertRaisesRegex(ValueError, "intervals"):
                        self.load()
            del self.data[key]


if __name__ == "__main__":
    unittest.main()
