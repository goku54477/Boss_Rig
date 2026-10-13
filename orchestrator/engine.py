"""Orchestrator engine coordinating task lifecycles, worktrees, policies, and deterministic gates."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from orchestrator.dispatcher import TaskDispatcher
from orchestrator.models import TaskEnvelope, TaskRecord, TaskState
from orchestrator.policy import PolicyEngine, PolicyViolation

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SCRIPT = REPO_ROOT / "scripts" / "worktree-gate.sh"
WORKTREES_DIR = REPO_ROOT / ".worktrees"


def run_gate(*args: str) -> subprocess.CompletedProcess[str]:
    cmd = ["bash", str(GATE_SCRIPT), *args]
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


class OrchestrationEngine:
    """Production coordination supervisor managing multi-agent tasks."""

    def __init__(self, dispatcher: Optional[TaskDispatcher] = None):
        self.tasks: Dict[str, TaskRecord] = {}
        self.dispatcher = dispatcher or TaskDispatcher()

    def create_task(
        self,
        task_id: str,
        goal: str,
        verification_commands: Optional[List[str]] = None,
    ) -> TaskRecord:
        """Create an idempotent task record."""
        if task_id in self.tasks:
            return self.tasks[task_id]

        record = TaskRecord(
            task_id=task_id,
            state=TaskState.QUEUED,
            goal=goal,
            verification_commands=verification_commands or ["true"],
        )
        record.transition(TaskState.QUEUED, reason="Task created")
        self.tasks[task_id] = record
        return record

    def assign_role(self, task_id: str, engine: str, role: str) -> TaskRecord:
        """Assign engine and role with policy validation."""
        task = self.tasks[task_id]
        PolicyEngine.validate_engine_and_role(engine, role)

        if role == "reviewer":
            PolicyEngine.assert_independent_review(task, engine)
            task.reviewer = engine
        elif role == "implementer":
            task.implementer = engine
        elif role == "planner":
            task.planner = engine

        task.assigned_engine = engine
        task.assigned_role = role
        task.transition(TaskState.ASSIGNED, reason=f"Assigned {engine} as {role}")
        return task

    def execute_turn(
        self,
        task_id: str,
        engine: str,
        role: str,
        prompt: Optional[str] = None,
        timeout_sec: int = 60,
    ) -> Dict[str, Any]:
        """Execute a turn with an engine and manage worktree binding."""
        task = self.tasks[task_id]
        PolicyEngine.validate_engine_and_role(engine, role)

        # Ensure seat worktree is initialized
        seat_wt = WORKTREES_DIR / engine
        if not seat_wt.exists():
            run_gate("init-seat", engine)

        # Bind seat to task branch for implementer role requiring writable branch mutations
        if role == "implementer":
            status_res = run_gate("status", task_id)
            if status_res.stdout.strip() != "ACTIVE":
                assign_res = run_gate("assign", task_id, engine)
                if assign_res.returncode != 0:
                    raise RuntimeError(f"Worktree assign failed: {assign_res.stderr}")

        task.transition(TaskState.ACTIVE, reason=f"Started execution by {engine} as {role}")

        envelope = TaskEnvelope(
            task_id=task_id,
            role=role,
            goal=prompt or task.goal,
            candidate_commit=task.candidate_commit,
            timeout_sec=timeout_sec,
        )

        event = self.dispatcher.dispatch_task(engine, envelope, timeout_sec=timeout_sec)
        status = event.get("status", "ERROR")

        if status == "SUCCESS":
            # Record current candidate commit from worktree
            rev_res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=seat_wt,
                capture_output=True,
                text=True,
            )
            if rev_res.returncode == 0:
                task.candidate_commit = rev_res.stdout.strip()

            # Invariant: Agent completion NEVER transitions directly to DONE!
            # It only transitions to REVIEW!
            task.transition(
                TaskState.REVIEW,
                reason="Agent completed turn successfully; awaiting review/verification",
                candidate_commit=task.candidate_commit,
            )
        else:
            task.transition(
                TaskState.REVIEW_FAILED,
                reason=f"Agent turn failed with status {status}",
                error=event.get("error", "Execution failure"),
            )

        return event

    def run_deterministic_verification(
        self,
        task_id: str,
        seat_id: Optional[str] = None,
    ) -> bool:
        """Run the deterministic verification supervisor and merge to integration."""
        task = self.tasks[task_id]
        seat = seat_id or task.assigned_engine or "opencode-1"

        PolicyEngine.assert_commit_pinned(task)
        PolicyEngine.assert_valid_state_transition(task.state, TaskState.MERGE_READY)
        task.transition(TaskState.MERGE_READY, reason="Ready for verification gate")

        # Run verification command via worktree-gate.sh review
        verify_cmd = " && ".join(task.verification_commands)
        task.transition(TaskState.MERGING, reason="Acquiring merge lock and running verification gate")

        gate_res = run_gate("review", task_id, seat, verify_cmd)
        if gate_res.returncode == 0:
            # Capture merge commit
            int_rev = subprocess.run(
                ["git", "rev-parse", "integration"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            if int_rev.returncode == 0:
                task.merge_commit = int_rev.stdout.strip()

            # Deterministic gate verifies and produces DONE!
            task.transition(
                TaskState.DONE,
                reason="Deterministic gate verified candidate tree and committed to integration",
                merge_commit=task.merge_commit,
            )

            # Release seat
            run_gate("release", task_id, seat)
            return True
        else:
            task.transition(
                TaskState.REVIEW_FAILED,
                reason=f"Verification gate failed: {gate_res.stderr}",
            )
            return False

    def close(self) -> None:
        self.dispatcher.close_all()
