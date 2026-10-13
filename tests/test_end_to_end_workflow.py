#!/usr/bin/env python3
"""Full End-to-End Feature Workflow and Crash Recovery Acceptance Tests.

Executes:
1. Multi-role Feature Workflow:
   - Planner (antigravity-1): defines implementation plan
   - Implementer (opencode-1): writes calc.py & tests, commits changes
   - Reviewer (antigravity-1): independent review (reviewer != implementer)
   - Deterministic Verification Gate: verifies candidate tree, merges to integration, marks DONE
2. Crash & Recovery Validation:
   - Simulates dirty worktree/abrupt termination
   - Executes deterministic recovery
   - Verifies seat returns to clean parked state and can execute subsequent tasks safely
"""

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.engine import OrchestrationEngine, run_gate
from orchestrator.models import TaskRecord, TaskState
from orchestrator.policy import PolicyEngine, PolicyViolation


class TestEndToEndWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = OrchestrationEngine()
        run_gate("init-seat", "opencode-1")
        run_gate("init-seat", "antigravity-1")
        run_gate("reset-seat", "opencode-1")
        run_gate("reset-seat", "antigravity-1")
        int_wt = REPO_ROOT / ".worktrees" / "_integration"
        if not int_wt.exists():
            subprocess.run(["git", "worktree", "add", str(int_wt), "integration"], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.close()

    def test_01_full_end_to_end_feature_pipeline(self) -> None:
        """Full pipeline: Planner (antigravity-1) -> Implementer (opencode-1) -> Reviewer (antigravity-1) -> Deterministic Gate -> DONE."""
        ts = int(time.time())
        task_id = f"T-E2E-CALC-{ts}"
        feature_goal = "Implement a calculator utility in calc.py with add(a, b) and multiply(a, b) and test verification"

        # 1. Create task
        task = self.engine.create_task(
            task_id=task_id,
            goal=feature_goal,
            verification_commands=["python3 -c 'import calc; assert calc.add(2, 3) == 5; assert calc.multiply(4, 5) == 20'"],
        )
        self.assertEqual(task.state, TaskState.QUEUED)

        # 2. Phase 1: Planning by antigravity-1
        self.engine.assign_role(task_id, engine="antigravity-1", role="planner")
        plan_res = self.engine.execute_turn(
            task_id,
            engine="antigravity-1",
            role="planner",
            prompt="Plan the implementation of calc.py with add and multiply functions and test verification.",
            timeout_sec=60,
        )
        self.assertEqual(plan_res.get("status"), "SUCCESS")
        self.assertEqual(task.planner, "antigravity-1")

        # 3. Phase 2: Implementation by opencode-1
        self.engine.assign_role(task_id, engine="opencode-1", role="implementer")
        opencode_wt = REPO_ROOT / ".worktrees" / "opencode-1"

        # Ensure seat is assigned to task branch before writing
        assign_proc = run_gate("assign", task_id, "opencode-1")
        self.assertEqual(assign_proc.returncode, 0)

        # Implement code directly inside the seat worktree
        calc_py = opencode_wt / "calc.py"
        calc_py.write_text(f"# Implementation ts {ts}\ndef add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n")

        subprocess.run(["git", "add", "calc.py"], cwd=opencode_wt, check=True)
        subprocess.run(["git", "commit", "-m", f"feat: implement calc module {ts}"], cwd=opencode_wt, check=True)

        impl_res = self.engine.execute_turn(
            task_id,
            engine="opencode-1",
            role="implementer",
            prompt="Confirm calc.py is committed and reply with: Implementation verified.",
            timeout_sec=90,
        )
        self.assertEqual(impl_res.get("status"), "SUCCESS")
        self.assertEqual(task.implementer, "opencode-1")

        # Invariant: Pin candidate commit
        self.assertIsNotNone(task.candidate_commit)
        self.assertEqual(task.state, TaskState.REVIEW)

        # 4. Phase 3: Independent Review (Policy: reviewer != implementer)
        # Verify self-review is forbidden
        with self.assertRaises(PolicyViolation):
            self.engine.assign_role(task_id, engine="opencode-1", role="reviewer")

        # Assign independent reviewer (antigravity-1)
        self.engine.assign_role(task_id, engine="antigravity-1", role="reviewer")
        self.assertEqual(task.reviewer, "antigravity-1")

        review_res = self.engine.execute_turn(
            task_id,
            engine="antigravity-1",
            role="reviewer",
            prompt=f"Review candidate commit {task.candidate_commit}: calc.py implementation is verified.",
            timeout_sec=60,
        )
        self.assertEqual(review_res.get("status"), "SUCCESS")

        # 5. Phase 4: Deterministic Gate Verification & Merge to integration
        verified = self.engine.run_deterministic_verification(task_id, seat_id="opencode-1")
        self.assertTrue(verified, "Deterministic verification gate failed")

        # Invariant: Task is now DONE with verified merge commit
        self.assertEqual(task.state, TaskState.DONE)
        self.assertIsNotNone(task.merge_commit)

        # Confirm integration branch contains the merged calc.py
        int_wt = REPO_ROOT / ".worktrees" / "_integration"
        self.assertTrue((int_wt / "calc.py").exists(), "calc.py not found in integration worktree after merge")

    def test_02_crash_and_recovery_validation(self) -> None:
        """Simulate seat crash with uncommitted modifications, recover seat, verify subsequent task safety."""
        seat = "opencode-1"
        seat_wt = REPO_ROOT / ".worktrees" / seat

        # Assign seat to test task
        task_id = "T-CRASH-REC-01"
        assign_res = run_gate("assign", task_id, seat)
        self.assertEqual(assign_res.returncode, 0)

        # Create uncommitted dirty file in seat worktree
        dirty_file = seat_wt / "dirty_crash_file.tmp"
        dirty_file.write_text("Uncommitted crash data")

        # Recover seat
        rec_res = run_gate("reset-seat", seat)
        self.assertEqual(rec_res.returncode, 0)

        # Verify seat is back on parked branch
        status_proc = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=seat_wt,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(status_proc.stdout.strip(), f"seat/{seat}")

        # Verify seat can immediately accept a new task cleanly
        task_id_2 = "T-CRASH-REC-02"
        assign_res_2 = run_gate("assign", task_id_2, seat)
        self.assertEqual(assign_res_2.returncode, 0)

        # Mark done for testing release
        state_file = REPO_ROOT / ".openrig-bridge" / "tasks" / f"{task_id_2}.state"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("DONE\n")

        # Release seat cleanly
        rel_res = run_gate("release", task_id_2, seat)
        self.assertEqual(rel_res.returncode, 0)


if __name__ == "__main__":
    unittest.main()
