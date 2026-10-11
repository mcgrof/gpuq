# SPDX-License-Identifier: MIT
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from gpuq.store import Store


def attempt(path, run_id, uid, output):
    output.put((run_id, Store(path).claim(run_id, uid)))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "queue.sqlite3")
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, name, uid=1000, resource="gpu"):
        return self.store.submit(
            user={"uid": uid, "user": f"user{uid}"}, resource=resource,
            description="Example", experiment_id="demo", run_id=name, command=["/bin/true"],
            cwd="/tmp", run_dir=self.root / name, expected_seconds=60,
        )

    def test_fifo_and_durable_history(self):
        self.submit("first")
        self.submit("second", 1001)
        self.assertFalse(self.store.claim("second", 1001))
        self.assertTrue(self.store.claim("first", 1000))
        self.assertFalse(self.store.claim("second", 1001))
        self.store.finish("first", 1000, state="completed", exit_code=0)
        self.assertTrue(self.store.claim("second", 1001))
        history = self.store.jobs(include_history=True)
        self.assertEqual([row["state"] for row in history], ["completed", "running"])
        self.assertEqual([row["event"] for row in self.store.events("first")],
                         ["submitted", "started", "completed"])

    def test_cancellation_does_not_release_a_live_slot(self):
        self.submit("first")
        self.submit("second")
        self.store.claim("first", 1000)
        self.store.cancel("first", 1000)
        self.assertEqual(self.store.get("first")["state"], "running")
        self.assertFalse(self.store.claim("second", 1000))
        self.store.finish("first", 1000, state="cancelled")
        self.assertTrue(self.store.claim("second", 1000))

    def test_wrong_owner_cannot_mutate(self):
        self.submit("first")
        operations = [
            lambda: self.store.claim("first", 1001),
            lambda: self.store.cancel("first", 1001),
            lambda: self.store.finish("first", 1001, state="failed"),
            lambda: self.store.pin("first", 1001, True),
            lambda: self.store.estimate("first", 1001, 120),
        ]
        for operation in operations:
            with self.assertRaises(PermissionError):
                operation()
        self.assertEqual(self.store.get("first")["state"], "queued")

    def test_independent_resources_do_not_block_each_other(self):
        self.submit("first", resource="gpu-a")
        self.submit("second", resource="gpu-b")
        self.assertTrue(self.store.claim("first", 1000))
        self.assertTrue(self.store.claim("second", 1000))

    def test_review_keeps_the_gpu_reserved(self):
        self.submit("first")
        self.submit("second")
        self.store.claim("first", 1000)
        self.store.review("first", 1000, "unit state uncertain")
        self.assertFalse(self.store.claim("second", 1000))
        self.assertEqual(self.store.get("first")["state"], "needs_review")

    def test_competing_processes_only_claim_fifo_head(self):
        for index in range(4):
            self.submit("job-" + str(index))
        context = multiprocessing.get_context("fork")
        output = context.Queue()
        processes = [context.Process(target=attempt, args=(
            str(self.store.path), "job-" + str(index), 1000, output
        )) for index in reversed(range(4))]
        for process in processes:
            process.start()
        results = dict(output.get(timeout=10) for _ in processes)
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(results, {"job-0": True, "job-1": False, "job-2": False, "job-3": False})

    def test_terminal_finish_is_idempotent(self):
        self.submit("first")
        self.assertTrue(self.store.finish("first", 1000, state="failed", exit_code=2))
        self.assertFalse(self.store.finish("first", 1000, state="completed", exit_code=0))
        self.assertEqual(self.store.get("first")["state"], "failed")
        self.assertEqual(len(self.store.events("first")), 2)

    def test_identifier_traversal_and_invalid_runtime_are_rejected(self):
        with self.assertRaises(ValueError):
            self.submit("../escape")
        self.submit("first")
        for seconds in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.store.estimate("first", 1000, seconds)

    def test_init_does_not_change_permissions_on_unrelated_directories(self):
        directory = self.root / "home"
        directory.mkdir(mode=0o700)
        (directory / "unrelated.txt").write_text("keep")
        with self.assertRaises(ValueError):
            Store(directory / "queue.sqlite3").initialize()
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertFalse((directory / "queue.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
