#!/usr/bin/env python3
"""Fault Injection & Crash Recovery Acceptance Tests."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.engine import OrchestrationEngine, run_gate
from orchestrator.models import TaskRecord, TaskState
from orchestrator.store import TaskStore


class TestFaultInjection(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = OrchestrationEngine()
        cls.store = TaskStore()
        for seat in ("codex-1", "opencode-1", "antigravity-1"):
            run_gate("init-seat", seat)
            run_gate("reset-seat", seat)
        run_gate("recover", "--force")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.close()

    def test_01_dirty_worktree_recovery(self) -> None:
        """Simulate unexpected crash leaving uncommitted modifications, recover seat."""
        seat = "opencode-1"
        seat_wt = REPO_ROOT / ".worktrees" / seat

        task_id = "T-FAULT-DIRTY-01"
        run_gate("assign", task_id, seat)

        # Create uncommitted dirty file
        dirty_file = seat_wt / "uncommitted_corrupt_data.bin"
        dirty_file.write_text("Corrupt binary payload from crashed process")

        # Recover
        res = run_gate("reset-seat", seat)
        self.assertEqual(res.returncode, 0)

        # Assert worktree is clean and back on parked branch
        self.assertFalse(dirty_file.exists())
        branch_proc = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=seat_wt,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(branch_proc.stdout.strip(), f"seat/{seat}")

    def test_02_stale_lock_recovery(self) -> None:
        """Simulate orphaned merge lock from a killed process, recover lock."""
        lock_dir = REPO_ROOT / ".git" / "openrig-merge.lock"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "pid").write_text("999999\n")
        (lock_dir / "host").write_text("orphaned-host\n")
        # Set started to 3 hours ago
        (lock_dir / "started").write_text(str(int(time.time()) - 10800) + "\n")

        self.assertTrue(lock_dir.exists())

        # Recover stale lock
        rec_res = run_gate("recover")
        self.assertEqual(rec_res.returncode, 0)
        self.assertFalse(lock_dir.exists(), "Stale merge lock should be reclaimed")

    def test_03_bridge_process_abrupt_termination(self) -> None:
        """Simulate abrupt SIGKILL of a seat bridge process and ensure dispatcher handles error cleanly."""
        bridge = self.engine.dispatcher.get_or_start_bridge("codex-1")
        self.assertTrue(bridge.is_alive())

        # Send SIGKILL directly to bridge child process
        bridge.proc.kill()
        bridge.proc.wait(timeout=2)
        self.assertFalse(bridge.is_alive())

        # Dispatcher must detect dead bridge and restart fresh bridge on next request
        fresh_bridge = self.engine.dispatcher.get_or_start_bridge("codex-1")
        self.assertTrue(fresh_bridge.is_alive())
        self.assertNotEqual(fresh_bridge.proc.pid, bridge.proc.pid)


if __name__ == "__main__":
    unittest.main()
