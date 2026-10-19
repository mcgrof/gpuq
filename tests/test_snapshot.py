# SPDX-License-Identifier: MIT
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from gpuq import snapshot
from gpuq.store import Store, now


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {
            "host": "node-a", "database": str(self.root / "state/queue.sqlite3"),
            "telemetry_seconds": 5.0,
            "resources": {"gpu": {}, "gpu1": {}},
        }
        self.store = Store(self.config["database"])
        self.store.initialize()
        for name, uid in (("first", 1000), ("second", 1001)):
            self.store.submit(
                user={"uid": uid, "user": f"user{uid}"}, resource="gpu", description="Example",
                experiment_id="demo", run_id=name, command=["/bin/true"], cwd="/tmp",
                run_dir=self.root / name, expected_seconds=600,
            )
        self.store.claim("first", 1000)

    def tearDown(self):
        self.temp.cleanup()

    def test_active_queue_and_idle_resources(self):
        view = snapshot.build(self.config, self.store.jobs())
        self.assertEqual(view["schema"], snapshot.SCHEMA)
        gpu, gpu1 = view["resources"]
        self.assertEqual(gpu["active"]["run_id"], "first")
        self.assertEqual([(job["run_id"], job["position"]) for job in gpu["queue"]], [("second", 1)])
        self.assertGreater(gpu["queue"][0]["remaining_seconds"], gpu["active"]["remaining_seconds"])
        self.assertIsNone(gpu1["active"])
        self.assertIsNone(gpu["telemetry"])
        bar = snapshot.waybar(view)
        self.assertEqual((bar["text"], bar["class"]), ("gpu +1", "busy"))

    def test_live_telemetry_is_shared_and_marked_stale(self):
        snapshot.publish_live(self.config, "gpu", {"timestamp_utc": now(), "stats": {"utilization": 87.0}})
        self.assertEqual(oct((snapshot.live_dir(self.config) / "gpu.json").stat().st_mode & 0o777), "0o660")
        view = snapshot.build(self.config, self.store.jobs())
        self.assertFalse(view["resources"][0]["telemetry"]["stale"])
        self.assertEqual(snapshot.waybar(view)["text"], "gpu 87% +1")
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
        view = snapshot.build(self.config, self.store.jobs(), later)
        self.assertTrue(view["resources"][0]["telemetry"]["stale"])
        self.assertEqual(snapshot.waybar(view)["text"], "gpu +1")
        Store(self.config["database"]).initialize()  # live/ must not block re-init


if __name__ == "__main__":
    unittest.main()
