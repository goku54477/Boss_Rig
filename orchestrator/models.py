"""Data models and state schemas for OpenRig Multi-Agent Orchestrator."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class TaskState(str, enum.Enum):
    QUEUED = "QUEUED"
    ASSIGNED = "ASSIGNED"
    ACTIVE = "ACTIVE"
    REVIEW = "REVIEW"
    REVIEW_FAILED = "REVIEW_FAILED"
    REVIEW_CONFLICT = "REVIEW_CONFLICT"
    MERGE_READY = "MERGE_READY"
    MERGING = "MERGING"
    DONE = "DONE"


KNOWN_ENGINES = ("codex-1", "opencode-1", "antigravity-1")

KNOWN_ROLES = (
    "planner",
    "implementer",
    "reviewer",
    "verifier",
    "debugger",
    "researcher",
    "coordinator",
)


@dataclass
class TaskEnvelope:
    task_id: str
    role: str
    goal: str
    requirements: Dict[str, Any] = field(
        default_factory=lambda: {
            "filesystem_write": True,
            "shell": True,
            "git": True,
        }
    )
    acceptance_criteria: List[str] = field(default_factory=list)
    test_commands: List[str] = field(default_factory=list)
    attempt: int = 1
    max_attempts: int = 2
    timeout_sec: int = 1800
    candidate_commit: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "task_id": self.task_id,
            "role": self.role,
            "goal": self.goal,
            "requirements": self.requirements,
            "acceptance_criteria": self.acceptance_criteria,
            "test_commands": self.test_commands,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "timeout_sec": self.timeout_sec,
        }
        if self.candidate_commit:
            data["candidate_commit"] = self.candidate_commit
        return data


@dataclass
class TaskRecord:
    task_id: str
    state: TaskState = TaskState.QUEUED
    goal: str = ""
    assigned_engine: Optional[str] = None
    assigned_role: Optional[str] = None
    implementer: Optional[str] = None
    reviewer: Optional[str] = None
    planner: Optional[str] = None
    candidate_commit: Optional[str] = None
    verification_commands: List[str] = field(default_factory=list)
    merge_commit: Optional[str] = None
    attempt: int = 1
    max_attempts: int = 2
    history: List[Dict[str, Any]] = field(default_factory=list)

    def transition(self, new_state: TaskState, reason: str = "", **metadata: Any) -> None:
        old_state = self.state
        self.state = new_state
        entry = {
            "from": old_state.value,
            "to": new_state.value,
            "reason": reason,
            **metadata,
        }
        self.history.append(entry)


@dataclass
class BridgeEvent:
    version: int
    source: str
    runtime: str
    seat_id: str
    task_id: str
    role: str
    event: str
    status: str
    attempt: int
    data: Dict[str, Any] = field(default_factory=dict)
