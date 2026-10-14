"""Tests for manual operator intervention, seat holds, and safety boundaries."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.dispatcher import SeatHoldError, TaskDispatcher
from orchestrator.engine import OrchestrationEngine
from orchestrator.models import SeatMode, TaskEnvelope, TaskRecord, TaskState
from orchestrator.store import TaskStore


class DummyBridge:
    def __init__(self):
        self.is_alive_val = True

    def is_alive(self):
        return self.is_alive_val

    def send_line(self, line: str):
        pass

    def read_until_event(self, event_name: str, timeout_sec: float = 60.0):
        if event_name == "task_started":
            return {"event": "task_started", "status": "STARTED"}
        return {
            "version": 1,
            "source": "seat-runner",
            "runtime": "mock",
            "seat_id": "codex-1",
            "task_id": "mock-task",
            "role": "planner",
            "event": "task_result",
            "status": "SUCCESS",
            "attempt": 1,
            "data": {"result": "Plan generated"},
        }


class TestOperatorIntervention(unittest.TestCase):
    def test_01_seat_mode_storage(self):
        """Verify seat control mode CRUD operations in SQLite store."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tasks.sqlite"
            store = TaskStore(db_path=db_path)

            # Default mode is AUTOMATED
            info = store.get_seat_mode("codex-1")
            self.assertEqual(info["seat_id"], "codex-1")
            self.assertEqual(info["mode"], SeatMode.AUTOMATED)

            # Place on hold
            info = store.set_seat_mode(
                "codex-1",
                SeatMode.MANUAL_HOLD,
                reason="Investigating worktree divergence",
                held_by="human-lead",
            )
            self.assertEqual(info["mode"], SeatMode.MANUAL_HOLD)
            self.assertEqual(info["reason"], "Investigating worktree divergence")
            self.assertEqual(info["held_by"], "human-lead")

            # Check list
            all_modes = store.list_seat_modes()
            self.assertIn("codex-1", all_modes)
            self.assertEqual(all_modes["codex-1"]["mode"], SeatMode.MANUAL_HOLD)

            # Release hold
            info = store.set_seat_mode("codex-1", SeatMode.AUTOMATED, reason="Investigation finished")
            self.assertEqual(info["mode"], SeatMode.AUTOMATED)

    def test_02_dispatcher_blocks_on_manual_hold(self):
        """Verify TaskDispatcher blocks task injection when target seat is on MANUAL_HOLD."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tasks.sqlite"
            store = TaskStore(db_path=db_path)
            dispatcher = TaskDispatcher(store=store)

            dummy = DummyBridge()
            dispatcher.active_bridges["opencode-1"] = dummy

            envelope = TaskEnvelope(task_id="task-100", role="implementer", goal="Build feature")

            # When automated, dispatch proceeds normally
            res = dispatcher.dispatch_task("opencode-1", envelope)
            self.assertEqual(res["status"], "SUCCESS")

            # Put seat on manual hold
            store.set_seat_mode("opencode-1", SeatMode.MANUAL_HOLD, reason="Human operator attached")

            # Dispatch MUST raise SeatHoldError
            with self.assertRaises(SeatHoldError) as ctx:
                dispatcher.dispatch_task("opencode-1", envelope)
            self.assertIn("is on MANUAL_HOLD", str(ctx.exception))
            self.assertIn("Human operator attached", str(ctx.exception))

            # Releasing hold allows dispatch again
            store.set_seat_mode("opencode-1", SeatMode.AUTOMATED)
            res2 = dispatcher.dispatch_task("opencode-1", envelope)
            self.assertEqual(res2["status"], "SUCCESS")

    def test_03_engine_blocks_turn_and_sets_task_blocked(self):
        """Verify OrchestrationEngine transitions task to BLOCKED if assigned seat is held."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tasks.sqlite"
            store = TaskStore(db_path=db_path)
            dispatcher = TaskDispatcher(store=store)
            dummy = DummyBridge()
            dispatcher.active_bridges["antigravity-1"] = dummy

            engine = OrchestrationEngine(dispatcher=dispatcher, store=store)
            task = engine.create_task("task-intervene-1", "Test operator intervention safety")
            engine.assign_role("task-intervene-1", "antigravity-1", "planner")

            # Place antigravity-1 on manual hold
            store.set_seat_mode("antigravity-1", SeatMode.MANUAL_HOLD, reason="Reviewing logs interactively")

            # Execute turn must raise SeatHoldError and transition task to BLOCKED
            with self.assertRaises(SeatHoldError):
                engine.execute_turn("task-intervene-1", "antigravity-1", "planner")

            updated_task = store.get_task("task-intervene-1")
            self.assertIsNotNone(updated_task)
            self.assertEqual(updated_task.state, TaskState.BLOCKED)
            self.assertTrue(any("MANUAL_HOLD" in h.get("reason", "") for h in updated_task.history))

    def test_04_operator_pause_and_resume_lifecycle(self):
        """Verify operator can pause (block) and resume tasks idempotently."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tasks.sqlite"
            store = TaskStore(db_path=db_path)

            task = TaskRecord(task_id="task-pause-test", state=TaskState.ACTIVE, goal="Check pausing")
            store.save_task(task)

            # Pause
            blocked_task = store.block_task("task-pause-test", reason="Pausing for design alignment")
            self.assertEqual(blocked_task.state, TaskState.BLOCKED)

            # Verify persisted
            loaded = store.get_task("task-pause-test")
            self.assertEqual(loaded.state, TaskState.BLOCKED)

            # Resume
            resumed_task = store.transition_task(
                "task-pause-test",
                TaskState.ACTIVE,
                reason="Resuming task execution",
            )
            self.assertEqual(resumed_task.state, TaskState.ACTIVE)
            self.assertEqual(store.get_task("task-pause-test").state, TaskState.ACTIVE)

    def test_05_cli_seat_hold_and_release(self):
        """Verify CLI seat hold and release commands."""
        import argparse
        from orchestrator.cli import cmd_seat_hold, cmd_seat_release, cmd_seat_status

        # Hold codex-1
        args_hold = argparse.Namespace(seat_id="codex-1", reason="Manual debugging session")
        ret_hold = cmd_seat_hold(args_hold)
        self.assertEqual(ret_hold, 0)

        store = TaskStore()
        info = store.get_seat_mode("codex-1")
        self.assertEqual(info["mode"], SeatMode.MANUAL_HOLD)
        self.assertEqual(info["reason"], "Manual debugging session")

        # Status check
        args_stat = argparse.Namespace()
        ret_stat = cmd_seat_status(args_stat)
        self.assertEqual(ret_stat, 0)

        # Release codex-1
        args_rel = argparse.Namespace(seat_id="codex-1")
        ret_rel = cmd_seat_release(args_rel)
        self.assertEqual(ret_rel, 0)

        info_rel = store.get_seat_mode("codex-1")
        self.assertEqual(info_rel["mode"], SeatMode.AUTOMATED)


if __name__ == "__main__":
    unittest.main()
