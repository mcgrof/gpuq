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

    def submit(self, name, uid=1000, resource="gpu", priority="normal"):
        return self.store.submit(
            user={"uid": uid, "user": f"user{uid}"}, resource=resource,
            description="Example", experiment_id="demo", run_id=name, command=["/bin/true"],
            cwd="/tmp", run_dir=self.root / name, expected_seconds=60, priority=priority,
        )

    def test_limits_are_validated_stored_and_recorded(self):
        for bad in ({"timeout_seconds": 0}, {"idle_seconds": float("inf")}, {"idle_percent": 100}):
            with self.assertRaises(ValueError):
                self.store.submit(
                    user={"uid": 1000, "user": "user1000"}, resource="gpu", description="Example",
                    experiment_id="demo", run_id="bad", command=["/bin/true"], cwd="/tmp",
                    run_dir=self.root / "bad", expected_seconds=60, **bad,
                )
        job = self.store.submit(
            user={"uid": 1000, "user": "user1000"}, resource="gpu", description="Example",
            experiment_id="demo", run_id="limited", command=["/bin/true"], cwd="/tmp",
            run_dir=self.root / "limited", expected_seconds=60, timeout_seconds=600, idle_seconds=300,
        )
        self.assertEqual((job["timeout_seconds"], job["idle_seconds"], job["idle_percent"]), (600, 300, 5))
        self.assertIsNone(self.submit("unlimited")["timeout_seconds"])
        with self.assertRaises(PermissionError):
            self.store.record("limited", 1001, "limit_reached")
        self.store.record("limited", 1000, "limit_reached", {"limit": "idle"})
        self.assertEqual(self.store.events("limited")[-1]["event"], "limit_reached")

    def test_normal_jobs_claim_before_earlier_low_priority_jobs(self):
        self.submit("low-1", priority="low")
        self.submit("low-2", priority="low")
        self.submit("normal", 1001)
        self.assertFalse(self.store.claim("low-1", 1000))
        self.assertTrue(self.store.claim("normal", 1001))
        self.store.finish("normal", 1001, state="completed", exit_code=0)
        self.assertFalse(self.store.claim("low-2", 1000))
        self.assertTrue(self.store.claim("low-1", 1000))

    def test_waiting_normal_job_preempts_only_a_running_low_job(self):
        self.submit("low", priority="low")
        self.store.claim("low", 1000)
        self.submit("normal-1", 1001)
        self.submit("normal-2", 1001)
        self.assertIsNone(self.store.preempt_for("normal-2", 1001))  # not the queue head
        self.assertEqual(self.store.preempt_for("normal-1", 1001), "low")
        self.assertIsNone(self.store.preempt_for("normal-1", 1001))  # already requested
        self.assertEqual(self.store.get("low")["preempt_requested"], 1)
        self.assertEqual(self.store.get("low")["state"], "running")  # the slot stays held
        self.assertFalse(self.store.claim("normal-1", 1001))

    def test_normal_running_job_is_never_preempted_by_the_queue(self):
        self.submit("first")
        self.store.claim("first", 1000)
        self.submit("second", 1001)
        self.assertIsNone(self.store.preempt_for("second", 1001))
        self.assertEqual(self.store.get("first")["preempt_requested"], 0)

    def test_preemption_permissions(self):
        self.submit("normal")
        self.store.claim("normal", 1000)
        with self.assertRaises(PermissionError):
            self.store.request_preemption("normal", 1001)
        self.assertTrue(self.store.request_preemption("normal", 1000))
        self.store.finish("normal", 1000, state="completed", exit_code=0)
        self.submit("low", priority="low")
        self.store.claim("low", 1000)
        with self.assertRaises(PermissionError):
            self.store.request_preemption("low", 1001, mode=2)  # only the owner may stop it
        self.assertTrue(self.store.request_preemption("low", 1001))
        self.assertFalse(self.store.request_preemption("low", 1001))
        self.assertTrue(self.store.request_preemption("low", 1000, mode=2))  # owner upgrades to stop

    def test_requeue_releases_the_slot_and_keeps_the_low_job_first_among_low(self):
        self.submit("low-1", priority="low")
        self.store.claim("low-1", 1000)
        self.submit("low-2", priority="low")
        self.submit("normal", 1001)
        self.store.preempt_for("normal", 1001)
        self.store.requeue("low-1", 1000, "gpuq-low-1-r1.service")
        row = self.store.get("low-1")
        self.assertEqual((row["state"], row["preempt_requested"], row["requeues"], row["unit"], row["started_at"]),
                         ("queued", 0, 1, "gpuq-low-1-r1.service", None))
        self.assertFalse(self.store.claim("low-1", 1000))
        self.assertTrue(self.store.claim("normal", 1001))
        self.store.finish("normal", 1001, state="completed", exit_code=0)
        self.assertFalse(self.store.claim("low-2", 1000))
        self.assertTrue(self.store.claim("low-1", 1000))
        self.assertEqual([e["event"] for e in self.store.events("low-1")],
                         ["submitted", "started", "preemption_requested", "requeued", "started"])

    def test_invalid_priority_and_signal_are_rejected(self):
        with self.assertRaises(ValueError):
            self.submit("first", priority="urgent")
        with self.assertRaises(ValueError):
            self.store.submit(user={"uid": 1000, "user": "u"}, resource="gpu", description="x",
                              experiment_id="demo", run_id="x", command=["/bin/true"], cwd="/tmp",
                              run_dir=self.root / "x", expected_seconds=60, preempt_signal="SIGBOGUS")

    def test_database_from_an_older_release_gains_the_new_columns(self):
        import sqlite3
        path = self.root / "old" / "queue.sqlite3"
        path.parent.mkdir()
        with sqlite3.connect(path) as db:
            db.executescript(Path(__file__).with_name("old_schema.sql").read_text())
            db.execute("""INSERT INTO jobs(run_id,experiment_id,uid,username,resource,description,
                          submitted_at,expected_seconds,cwd,command_json,run_dir,unit)
                          VALUES('old','demo',1000,'u','gpu','d','2026-01-01',60,'/tmp','["/bin/true"]','/tmp/x','u')""")
        db = None
        store = Store(path)
        row = store.get("old")
        self.assertEqual((row["priority"], row["preempt_requested"], row["requeues"]), ("normal", 0, 0))

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
