#!/usr/bin/env python3
"""3x3 Agent-Role Interchangeability Matrix Acceptance Test.

Proves that:
  Engine != Role
All 3 engines (codex-1, opencode-1, antigravity-1) execute successfully in all 3
core roles (planner, implementer, reviewer), yielding a verified 3x3 matrix.
"""

import json
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.dispatcher import TaskDispatcher
from orchestrator.models import KNOWN_ENGINES, TaskEnvelope

ROLES_TO_TEST = ("planner", "implementer", "reviewer")


class TestInterchangeabilityMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dispatcher = TaskDispatcher()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.dispatcher.close_all()

    def _test_engine_role(self, engine: str, role: str) -> None:
        task_id = f"T-MAT-{engine}-{role}"
        envelope = TaskEnvelope(
            task_id=task_id,
            role=role,
            goal=f"As interchangeable {role}, verify role assignment and reply with: {engine} {role} verified.",
            timeout_sec=45,
        )
        res = self.dispatcher.dispatch_task(engine, envelope, timeout_sec=45)
        self.assertEqual(
            res.get("status"),
            "SUCCESS",
            f"Failed matrix execution for engine={engine}, role={role}: {res}",
        )
        self.assertEqual(res.get("task_id"), task_id)
        self.assertEqual(res.get("role"), role)
        self.assertEqual(res.get("seat_id"), engine)

    # 1. codex-1 tests
    def test_01_codex_as_planner(self) -> None:
        self._test_engine_role("codex-1", "planner")

    def test_02_codex_as_implementer(self) -> None:
        self._test_engine_role("codex-1", "implementer")

    def test_03_codex_as_reviewer(self) -> None:
        self._test_engine_role("codex-1", "reviewer")

    # 2. opencode-1 tests
    def test_04_opencode_as_planner(self) -> None:
        self._test_engine_role("opencode-1", "planner")

    def test_05_opencode_as_implementer(self) -> None:
        self._test_engine_role("opencode-1", "implementer")

    def test_06_opencode_as_reviewer(self) -> None:
        self._test_engine_role("opencode-1", "reviewer")

    # 3. antigravity-1 tests
    def test_07_antigravity_as_planner(self) -> None:
        self._test_engine_role("antigravity-1", "planner")

    def test_08_antigravity_as_implementer(self) -> None:
        self._test_engine_role("antigravity-1", "implementer")

    def test_09_antigravity_as_reviewer(self) -> None:
        self._test_engine_role("antigravity-1", "reviewer")


if __name__ == "__main__":
    unittest.main()
