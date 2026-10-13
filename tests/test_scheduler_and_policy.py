#!/usr/bin/env python3
"""Tests for Milestone 5: Dynamic Scheduler, Independent-Review Policy, and Commit-Pinned Verification."""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.models import KNOWN_ENGINES, KNOWN_ROLES, TaskRecord, TaskState
from orchestrator.policy import PolicyEngine, PolicyViolation


class TestSchedulerAndPolicy(unittest.TestCase):
    def test_01_engine_not_equal_to_role(self) -> None:
        """Requirement: Any engine can receive any role on any task. ENGINE != ROLE."""
        for engine in KNOWN_ENGINES:
            for role in KNOWN_ROLES:
                # Must not raise
                PolicyEngine.validate_engine_and_role(engine, role)

        # Invalid engine/role must be rejected
        with self.assertRaises(PolicyViolation):
            PolicyEngine.validate_engine_and_role("invalid-engine", "implementer")

        with self.assertRaises(PolicyViolation):
            PolicyEngine.validate_engine_and_role("codex-1", "invalid-role")

    def test_02_independent_review_policy(self) -> None:
        """Requirement: Reviewer must NEVER be the implementer (Self-review forbidden)."""
        # When opencode-1 is implementer, reviewer cannot be opencode-1
        reviewer = PolicyEngine.select_reviewer(implementer="opencode-1")
        self.assertNotEqual(reviewer, "opencode-1")
        self.assertIn(reviewer, ["codex-1", "antigravity-1"])

        # When antigravity-1 is implementer, reviewer cannot be antigravity-1
        reviewer = PolicyEngine.select_reviewer(implementer="antigravity-1")
        self.assertNotEqual(reviewer, "antigravity-1")
        self.assertIn(reviewer, ["codex-1", "opencode-1"])

        # Explicit self-review assertion must raise PolicyViolation
        task = TaskRecord(task_id="T-POL-01", implementer="opencode-1")
        with self.assertRaises(PolicyViolation):
            PolicyEngine.assert_independent_review(task, candidate_reviewer="opencode-1")

        # Independent review passes
        PolicyEngine.assert_independent_review(task, candidate_reviewer="antigravity-1")
        PolicyEngine.assert_independent_review(task, candidate_reviewer="codex-1")

    def test_03_commit_pinned_verification_policy(self) -> None:
        """Requirement: Candidate commit SHA must be pinned before verification/review."""
        task_unpinned = TaskRecord(task_id="T-POL-02", candidate_commit=None)
        with self.assertRaises(PolicyViolation):
            PolicyEngine.assert_commit_pinned(task_unpinned)

        task_pinned = TaskRecord(task_id="T-POL-03", candidate_commit="a1b2c3d4e5f6")
        pinned_sha = PolicyEngine.assert_commit_pinned(task_pinned)
        self.assertEqual(pinned_sha, "a1b2c3d4e5f6")

    def test_04_state_machine_safety(self) -> None:
        """Requirement: Agent turn success only produces REVIEW; only deterministic gate produces DONE."""
        task = TaskRecord(task_id="T-POL-04")
        self.assertEqual(task.state, TaskState.QUEUED)

        # Transition to ASSIGNED
        task.transition(TaskState.ASSIGNED)
        self.assertEqual(task.state, TaskState.ASSIGNED)

        # Transition to ACTIVE
        task.transition(TaskState.ACTIVE)
        self.assertEqual(task.state, TaskState.ACTIVE)

        # INVARIANT: Direct transition from ACTIVE to DONE is strictly illegal!
        with self.assertRaises(PolicyViolation):
            PolicyEngine.assert_valid_state_transition(TaskState.ACTIVE, TaskState.DONE)

        # Agent completion transitions to REVIEW
        PolicyEngine.assert_valid_state_transition(TaskState.ACTIVE, TaskState.REVIEW)
        task.transition(TaskState.REVIEW)

        # Deterministic gate verification flow
        PolicyEngine.assert_valid_state_transition(TaskState.REVIEW, TaskState.MERGE_READY)
        task.transition(TaskState.MERGE_READY)

        PolicyEngine.assert_valid_state_transition(TaskState.MERGE_READY, TaskState.MERGING)
        task.transition(TaskState.MERGING)

        PolicyEngine.assert_valid_state_transition(TaskState.MERGING, TaskState.DONE)
        task.transition(TaskState.DONE)
        self.assertEqual(task.state, TaskState.DONE)


if __name__ == "__main__":
    unittest.main()
