#!/usr/bin/env python3
"""Acceptance tests for SQLite TaskStore and Idempotent Event Deduplication."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.models import BridgeEvent, TaskRecord, TaskState
from orchestrator.store import TaskStore


class TestTaskStoreAndIdempotency(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_tasks.sqlite"
        self.store = TaskStore(db_path=self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_01_save_and_retrieve_task(self) -> None:
        task = TaskRecord(
            task_id="T-STORE-01",
            state=TaskState.QUEUED,
            goal="Test SQLite durability",
            verification_commands=["python3 -m unittest"],
        )
        self.store.save_task(task)

        loaded = self.store.get_task("T-STORE-01")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.task_id, "T-STORE-01")
        self.assertEqual(loaded.state, TaskState.QUEUED)
        self.assertEqual(loaded.goal, "Test SQLite durability")
        self.assertEqual(loaded.verification_commands, ["python3 -m unittest"])

    def test_02_task_transitions_and_history(self) -> None:
        task = TaskRecord(task_id="T-STORE-02", state=TaskState.QUEUED, goal="Transition test")
        self.store.save_task(task)

        self.store.transition_task(
            "T-STORE-02",
            TaskState.ASSIGNED,
            reason="Assigned to codex-1",
            engine="codex-1",
        )
        self.store.transition_task(
            "T-STORE-02",
            TaskState.ACTIVE,
            reason="Worktree checkout active",
        )
        self.store.transition_task(
            "T-STORE-02",
            TaskState.DONE,
            reason="Gate pass",
            merge_commit="abcdef123456",
        )

        loaded = self.store.get_task("T-STORE-02")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.state, TaskState.DONE)
        self.assertEqual(len(loaded.history), 3)
        self.assertEqual(loaded.history[0]["from"], "QUEUED")
        self.assertEqual(loaded.history[0]["to"], "ASSIGNED")
        self.assertEqual(loaded.history[2]["to"], "DONE")
        self.assertEqual(loaded.history[2]["merge_commit"], "abcdef123456")

    def test_03_terminal_states_blocked_and_cancelled(self) -> None:
        t_block = TaskRecord(task_id="T-STORE-BLK", state=TaskState.ACTIVE, goal="Block test")
        self.store.save_task(t_block)
        blocked = self.store.block_task("T-STORE-BLK", reason="Dependency missing")
        self.assertEqual(blocked.state, TaskState.BLOCKED)

        t_cancel = TaskRecord(task_id="T-STORE-CNC", state=TaskState.QUEUED, goal="Cancel test")
        self.store.save_task(t_cancel)
        cancelled = self.store.cancel_task("T-STORE-CNC", reason="Operator aborted")
        self.assertEqual(cancelled.state, TaskState.CANCELLED)

        # Retrieve and verify persistence
        self.assertEqual(self.store.get_task("T-STORE-BLK").state, TaskState.BLOCKED)
        self.assertEqual(self.store.get_task("T-STORE-CNC").state, TaskState.CANCELLED)

    def test_04_idempotent_event_deduplication(self) -> None:
        evt = BridgeEvent(
            version=1,
            source="bridge",
            runtime="codex",
            seat_id="codex-1",
            task_id="T-EVT-01",
            role="implementer",
            event="task_result",
            status="SUCCESS",
            attempt=1,
            data={"exit_code": 0},
        )

        # First recording must succeed
        recorded_1 = self.store.record_event(evt)
        self.assertTrue(recorded_1, "First event recording should return True")

        # Duplicate recording must be idempotently ignored
        recorded_2 = self.store.record_event(evt)
        self.assertFalse(recorded_2, "Duplicate event recording should return False")

        # Event with different status or attempt should succeed
        evt_attempt_2 = BridgeEvent(
            version=1,
            source="bridge",
            runtime="codex",
            seat_id="codex-1",
            task_id="T-EVT-01",
            role="implementer",
            event="task_result",
            status="SUCCESS",
            attempt=2,
            data={"exit_code": 0},
        )
        self.assertTrue(self.store.record_event(evt_attempt_2))

    def test_05_concurrent_writers_and_busy_backoff(self) -> None:
        """Prove that concurrent writers succeed without SQLITE_BUSY errors due to busy_timeout & retry backoff."""
        import concurrent.futures

        num_tasks = 30

        def write_worker(idx: int) -> str:
            tid = f"T-CONC-{idx}"
            task = TaskRecord(task_id=tid, state=TaskState.QUEUED, goal=f"Goal {idx}")
            self.store.save_task(task)
            self.store.transition_task(tid, TaskState.ASSIGNED, reason="test assignment")
            self.store.transition_task(tid, TaskState.ACTIVE, reason="test active")
            return tid

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(write_worker, i) for i in range(num_tasks)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        self.assertEqual(len(results), num_tasks)
        for i in range(num_tasks):
            t = self.store.get_task(f"T-CONC-{i}")
            self.assertIsNotNone(t)
            self.assertEqual(t.state, TaskState.ACTIVE)
            self.assertEqual(len(t.history), 2)


if __name__ == "__main__":
    unittest.main()

