"""Policy engine enforcing Engine != Role, Independent Review, and Commit-Pinned Verification."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional
from orchestrator.models import KNOWN_ENGINES, KNOWN_ROLES, TaskRecord, TaskState


class PolicyViolation(Exception):
    """Raised when an orchestration policy is violated."""


class PolicyEngine:
    """Enforces orchestration governance invariants."""

    @staticmethod
    def validate_engine_and_role(engine: str, role: str) -> None:
        if engine not in KNOWN_ENGINES:
            raise PolicyViolation(f"Unknown engine '{engine}'. Must be one of {KNOWN_ENGINES}")
        if role not in KNOWN_ROLES:
            raise PolicyViolation(f"Unknown role '{role}'. Must be one of {KNOWN_ROLES}")

    @staticmethod
    def select_reviewer(
        implementer: str,
        preferred_engine: Optional[str] = None,
        available_engines: Optional[List[str]] = None,
        planner: Optional[str] = None,
    ) -> str:
        """Enforce Independent Review Policy: reviewer MUST NOT be implementer.

        When >= 3 healthy engines are available, prefers a third distinct engine
        so that reviewer != implementer and reviewer != planner.
        """
        candidates = list(available_engines or KNOWN_ENGINES)
        if implementer in candidates:
            candidates.remove(implementer)

        if not candidates:
            raise PolicyViolation(
                f"Cannot assign independent reviewer: no engines remaining after excluding implementer '{implementer}'"
            )

        if preferred_engine and preferred_engine in candidates:
            return preferred_engine

        # If planner is provided, prefer a distinct third engine (reviewer != planner)
        if planner:
            third_party = [e for e in candidates if e != planner]
            if third_party:
                return third_party[0]

        return candidates[0]

    @staticmethod
    def assert_independent_review(task: TaskRecord, candidate_reviewer: str) -> None:
        """Raise PolicyViolation if reviewer matches implementer."""
        if not task.implementer:
            raise PolicyViolation(f"Task '{task.task_id}' has no recorded implementer")
        if task.implementer == candidate_reviewer:
            raise PolicyViolation(
                f"Independent Review Policy Violation: Engine '{candidate_reviewer}' "
                f"implemented task '{task.task_id}' and cannot review its own changes."
            )

    @staticmethod
    def assert_commit_pinned(task: TaskRecord) -> str:
        """Ensure task has a valid pinned candidate commit before review/verification."""
        if not task.candidate_commit or not task.candidate_commit.strip():
            raise PolicyViolation(
                f"Commit-Pinned Policy Violation: Task '{task.task_id}' has no candidate_commit SHA"
            )
        return task.candidate_commit.strip()

    ALLOWED_REPOSITORIES: List[Path] = [
        Path(r"c:\Users\jacob\Boss Agent"),
        Path("/mnt/c/Users/jacob/Boss Agent"),
    ]

    @classmethod
    def assert_allowed_repository(
        cls, repo: Path | str, extra_allowed: Optional[List[Path | str]] = None
    ) -> Path:
        """Validate repository path against canonical allowlist."""
        resolved = Path(repo).resolve()
        allowed_list = [p.resolve() for p in cls.ALLOWED_REPOSITORIES if p.exists()]
        if extra_allowed:
            for extra in extra_allowed:
                allowed_list.append(Path(extra).resolve())

        # Allow if resolved matches any allowed path
        for allowed in allowed_list:
            if resolved == allowed or str(resolved).lower() == str(allowed).lower():
                return resolved

        raise PolicyViolation(
            f"Repository '{repo}' (resolved: '{resolved}') is not in allowed list: {[str(p) for p in allowed_list]}"
        )

    @staticmethod
    def assert_allowed_merge_target(target: str) -> None:
        """Strictly enforce integration-only merge target. Reject main or others."""
        clean = (target or "").strip().lower()
        if clean != "integration":
            raise PolicyViolation(
                f"Forbidden merge target '{target}'. Merging is strictly locked to 'integration' only. Direct merge to 'main' is prohibited."
            )

    @staticmethod
    def assert_valid_state_transition(current: TaskState, target: TaskState) -> None:
        """Ensure state transitions adhere to deterministic lifecycle."""
        valid_transitions = {
            TaskState.QUEUED: [TaskState.ASSIGNED, TaskState.CANCELLED],
            TaskState.ASSIGNED: [TaskState.ACTIVE, TaskState.CANCELLED],
            TaskState.ACTIVE: [
                TaskState.REVIEW,
                TaskState.REVIEW_FAILED,
                TaskState.WAITING_FOR_USER,
                TaskState.CANCELLED,
            ],
            TaskState.WAITING_FOR_USER: [TaskState.ACTIVE, TaskState.CANCELLED],
            TaskState.REVIEW: [
                TaskState.MERGE_READY,
                TaskState.REVIEW_FAILED,
                TaskState.REVIEW_CONFLICT,
                TaskState.CANCELLED,
            ],
            TaskState.MERGE_READY: [TaskState.MERGING, TaskState.REVIEW_FAILED, TaskState.CANCELLED],
            TaskState.MERGING: [
                TaskState.DONE,
                TaskState.REVIEW_FAILED,
                TaskState.REVIEW_CONFLICT,
            ],
            TaskState.REVIEW_FAILED: [TaskState.QUEUED, TaskState.ACTIVE, TaskState.CANCELLED],
            TaskState.REVIEW_CONFLICT: [TaskState.QUEUED, TaskState.ACTIVE, TaskState.CANCELLED],
            TaskState.BLOCKED: [TaskState.QUEUED, TaskState.CANCELLED],
            TaskState.DONE: [],
            TaskState.CANCELLED: [],
        }

        allowed = valid_transitions.get(current, [])
        if target not in allowed:
            raise PolicyViolation(
                f"Illegal state transition from {current.value} to {target.value}. Allowed: {[s.value for s in allowed]}"
            )

