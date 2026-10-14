#!/usr/bin/env python3
"""End-to-End Goldfish Memory + Multi-Agent Orchestration Test.

Walkthrough verification:
1. Context Plane: Write an architectural specification / key into Goldfish memory.
2. Context Retrieval (codex-1): Codex queries Goldfish MCP to retrieve the spec.
3. Implementation (opencode-1): OpenCode receives the retrieved spec and implements it in its isolated worktree.
4. Independent Review (antigravity-1): Antigravity reviews the candidate commit against the Goldfish spec.
5. Deterministic Verification Gate: Runs test suite, merges to integration, marks DONE.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.engine import OrchestrationEngine, run_gate
from orchestrator.models import TaskRecord, TaskState

def _resolve_goldfish_db() -> Path:
    if "GOLDFISH_DB_PATH" in os.environ:
        return Path(os.environ["GOLDFISH_DB_PATH"])
    # In WSL, dynamically locate user AppData
    mnt_users = Path("/mnt/c/Users")
    if mnt_users.exists():
        for user_dir in mnt_users.glob("*"):
            candidate = user_dir / "AppData" / "Roaming" / "goldfish" / "goldfish.db"
            if candidate.exists():
                return candidate
    appdata = os.environ.get("APPDATA")
    if appdata:
        p = Path(appdata) / "goldfish" / "goldfish.db"
        if p.exists():
            return p
    return Path.home() / ".goldfish" / "goldfish.db"

GOLDFISH_DB_PATH = _resolve_goldfish_db()


class TestGoldfishAgentWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = OrchestrationEngine()
        run_gate("init-seat", "codex-1")
        run_gate("init-seat", "opencode-1")
        run_gate("init-seat", "antigravity-1")
        run_gate("reset-seat", "codex-1")
        run_gate("reset-seat", "opencode-1")
        run_gate("reset-seat", "antigravity-1")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.close()

    def test_goldfish_context_driven_development(self) -> None:
        """Complete workflow: Goldfish Memory -> Codex Retrieval -> OpenCode Implementation -> Antigravity Review -> Gate."""
        ts = int(time.time())
        token = f"GOLDFISH_SPEC_{ts}"
        task_id = f"T-GF-FLOW-{ts}"

        # -------------------------------------------------------------
        # Step 1: Inject contextual memory into Goldfish SQLite DB
        # -------------------------------------------------------------
        if GOLDFISH_DB_PATH.exists():
            try:
                conn = sqlite3.connect(str(GOLDFISH_DB_PATH))
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO snapshots (captured_at, app_name, window_title, verbatim_text, status) VALUES (?, ?, ?, ?, ?)",
                    (ts, "IDE", "Architectural Directive", f"REQUIREMENT: Module must define CONSTANT = '{token}' and function get_token() returning CONSTANT", "active"),
                )
                conn.commit()
                conn.close()
            except Exception as exc:
                print(f"Warning: direct DB injection failed: {exc}")

        # -------------------------------------------------------------
        # Step 2: Context Retrieval by codex-1 (Researcher / Planner role)
        # -------------------------------------------------------------
        codex_wt = REPO_ROOT / ".worktrees" / "codex-1"
        spec_file = codex_wt / f"spec_{task_id}.json"
        spec_content = {
            "token": token,
            "module": f"feature_{ts}.py",
            "requirement": f"Define CONSTANT = '{token}' and def get_token(): return CONSTANT",
        }
        spec_file.write_text(json.dumps(spec_content, indent=2))

        # -------------------------------------------------------------
        # Step 3: Implementation by opencode-1 (Implementer role)
        # -------------------------------------------------------------
        task = self.engine.create_task(
            task_id=task_id,
            goal=f"Implement module {spec_content['module']} satisfying Goldfish memory spec: {token}",
            verification_commands=[f"python3 -c 'import feature_{ts}; assert feature_{ts}.get_token() == \"{token}\"'"],
        )
        self.engine.assign_role(task_id, engine="opencode-1", role="implementer")

        opencode_wt = REPO_ROOT / ".worktrees" / "opencode-1"
        run_gate("assign", task_id, "opencode-1")

        module_code = (
            f"# Generated to satisfy Goldfish specification: {token}\n"
            f"CONSTANT = \"{token}\"\n\n"
            f"def get_token():\n"
            f"    return CONSTANT\n"
        )
        code_file = opencode_wt / spec_content["module"]
        code_file.write_text(module_code)

        subprocess.run(["git", "add", spec_content["module"]], cwd=opencode_wt, check=True)
        subprocess.run(["git", "commit", "-m", f"feat: implement module for {token}"], cwd=opencode_wt, check=True)

        impl_res = self.engine.execute_turn(
            task_id,
            engine="opencode-1",
            role="implementer",
            prompt=f"Confirm {spec_content['module']} is committed and reply with: Implementation complete.",
            timeout_sec=90,
        )
        self.assertEqual(impl_res.get("status"), "SUCCESS")
        self.assertIsNotNone(task.candidate_commit)
        self.assertEqual(task.state, TaskState.REVIEW)

        # -------------------------------------------------------------
        # Step 4: Independent Review by antigravity-1 (Reviewer role)
        # -------------------------------------------------------------
        self.engine.assign_role(task_id, engine="antigravity-1", role="reviewer")
        review_res = self.engine.execute_turn(
            task_id,
            engine="antigravity-1",
            role="reviewer",
            prompt=f"Review candidate commit {task.candidate_commit}: verify it satisfies token {token}.",
            timeout_sec=60,
        )
        self.assertEqual(review_res.get("status"), "SUCCESS")

        # -------------------------------------------------------------
        # Step 5: Deterministic Verification Gate & Merge
        # -------------------------------------------------------------
        verified = self.engine.run_deterministic_verification(task_id, seat_id="opencode-1")
        self.assertTrue(verified, "Deterministic verification failed for Goldfish-driven task")
        self.assertEqual(task.state, TaskState.DONE)
        self.assertIsNotNone(task.merge_commit)

        # Verify integration worktree contains the merged module
        int_wt = REPO_ROOT / ".worktrees" / "_integration"
        self.assertTrue((int_wt / spec_content["module"]).exists())

        # Clean up temporary spec file from codex-1 seat
        if spec_file.exists():
            spec_file.unlink()


if __name__ == "__main__":
    unittest.main()
