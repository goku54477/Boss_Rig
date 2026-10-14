"""Policy engine enforcing Engine != Role, Independent Review, and Commit-Pinned Verification."""

from __future__ import annotations

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
    ) -> str:
        """Enforce Independent Review Policy: reviewer MUST NOT be implementer."""
        candidates = list(available_engines or KNOWN_ENGINES)
        if implementer in candidates:
            candidates.remove(implementer)

        if not candidates:
            raise PolicyViolation(
                f"Cannot assign independent reviewer: no engines remaining after excluding implementer '{implementer}'"
            )

        if preferred_engine and preferred_engine in candidates:
            return preferred_engine

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

    @staticmethod
    def assert_valid_state_transition(current: TaskState, target: TaskState) -> None:
        """Ensure state transitions adhere to deterministic lifecycle."""
        valid_transitions = {
            TaskState.QUEUED: [TaskState.ASSIGNED],
            TaskState.ASSIGNED: [TaskState.ACTIVE],
            TaskState.ACTIVE: [TaskState.REVIEW, TaskState.REVIEW_FAILED],
            TaskState.REVIEW: [TaskState.MERGE_READY, TaskState.REVIEW_FAILED, TaskState.REVIEW_CONFLICT],
            TaskState.MERGE_READY: [TaskState.MERGING, TaskState.REVIEW_FAILED],
            TaskState.MERGING: [TaskState.DONE, TaskState.REVIEW_FAILED, TaskState.REVIEW_CONFLICT],
            TaskState.REVIEW_FAILED: [TaskState.QUEUED, TaskState.ACTIVE],
            TaskState.REVIEW_CONFLICT: [TaskState.QUEUED, TaskState.ACTIVE],
            TaskState.DONE: [],
        }

        allowed = valid_transitions.get(current, [])
        if target not in allowed:
            raise PolicyViolation(
                f"Illegal state transition from {current.value} to {target.value}. Allowed: {[s.value for s in allowed]}"
            )
