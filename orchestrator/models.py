"""Data models and state schemas for OpenRig Multi-Agent Orchestrator."""

from __future__ import annotations

import enum
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class TaskState(str, enum.Enum):
    QUEUED = "QUEUED"
    ASSIGNED = "ASSIGNED"
    ACTIVE = "ACTIVE"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    REVIEW = "REVIEW"
    REVIEW_FAILED = "REVIEW_FAILED"
    REVIEW_CONFLICT = "REVIEW_CONFLICT"
    MERGE_READY = "MERGE_READY"
    MERGING = "MERGING"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class SeatMode(str, enum.Enum):
    AUTOMATED = "AUTOMATED"
    MANUAL_HOLD = "MANUAL_HOLD"


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
    context: Optional[Dict[str, Any]] = None

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
        if self.context:
            data["context"] = self.context
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
    context: Optional[Dict[str, Any]] = None
    history: List[Dict[str, Any]] = field(default_factory=list)
    spec_id: Optional[str] = None
    repo: Optional[str] = None
    allowed_merge_target: str = "integration"
    questions: List[Dict[str, Any]] = field(default_factory=list)

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


@dataclass
class TaskSpec:
    repo: str
    goal: str
    verification_commands: List[str]
    constraints: List[str] = field(default_factory=list)
    acceptance_criteria: List[str] = field(default_factory=list)
    allowed_merge_target: str = "integration"
    timeout_sec: int = 1800
    spec_version: int = 1
    assigned_agents: Dict[str, str] = field(
        default_factory=lambda: {
            "planner": "antigravity-1",
            "implementer": "opencode-1",
            "reviewer": "antigravity-1",
        }
    )

    def to_canonical_dict(self) -> Dict[str, Any]:
        """Produce dictionary with sorted keys for deterministic hashing."""
        return {
            "acceptance_criteria": sorted(self.acceptance_criteria),
            "allowed_merge_target": self.allowed_merge_target,
            "assigned_agents": {k: self.assigned_agents[k] for k in sorted(self.assigned_agents)},
            "constraints": sorted(self.constraints),
            "goal": self.goal.strip(),
            "repo": self.repo.strip(),
            "spec_version": self.spec_version,
            "timeout_sec": self.timeout_sec,
            "verification_commands": self.verification_commands,
        }

    def compute_hash(self) -> str:
        """Compute stable SHA-256 hash over canonical JSON representation."""
        data = self.to_canonical_dict()
        canonical_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"


def compute_spec_hash(data: Dict[str, Any]) -> str:
    """Compute sha256:<hex> hash from a raw spec dict using canonical formatting."""
    # Normalize dictionary fields
    normalized = {
        "acceptance_criteria": sorted(data.get("acceptance_criteria") or []),
        "allowed_merge_target": data.get("allowed_merge_target", "integration"),
        "assigned_agents": {
            k: data.get("assigned_agents", {}).get(k, "")
            for k in sorted(data.get("assigned_agents") or {"planner": "antigravity-1", "implementer": "opencode-1", "reviewer": "antigravity-1"})
        },
        "constraints": sorted(data.get("constraints") or []),
        "goal": str(data.get("goal", "")).strip(),
        "repo": str(data.get("repo", "")).strip(),
        "spec_version": int(data.get("spec_version", 1)),
        "timeout_sec": int(data.get("timeout_sec", 1800)),
        "verification_commands": list(data.get("verification_commands") or []),
    }
    canonical_json = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()}"


@dataclass
class UserQuestion:
    question_id: str
    task_id: str
    prompt: str
    conversation_id: Optional[str] = None
    status: str = "PENDING"  # PENDING, ANSWERED, EXPIRED, CANCELLED
    answer_text: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    answered_at: Optional[float] = None


@dataclass
class ApprovalRecord:
    spec_id: str
    repo: str
    source: str  # "goldfish_chat", "operator_cli"
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    approval_token: Optional[str] = None
    approved_by: str = "user"
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "repo": self.repo,
            "source": self.source,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "approval_token": self.approval_token,
            "approved_by": self.approved_by,
            "timestamp": self.timestamp,
        }


