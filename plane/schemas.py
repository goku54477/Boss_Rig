"""Standard wire contracts for Boss Rig Shared Plane."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

VALID_ACTIONS = ("IGNORE", "PROPOSE_BUILD", "ANSWER_QUESTION", "CHECK_STATUS")


@dataclass
class PlaneMessage:
    """Normalized incoming message from a read-only listener."""

    id: str
    source: str
    conversation_id: str
    role: str
    text: str
    created_at: float = field(default_factory=time.time)
    recent_context: List[str] = field(default_factory=list)


@dataclass
class RoutingDecision:
    """Intent screening and agent selection decision emitted by a Router."""

    action: str
    confidence: float
    target_agent: str
    reason: str
    router_name: str
    latency_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.action not in VALID_ACTIONS:
            raise ValueError(
                f"Invalid routing action '{self.action}'. Must be one of {VALID_ACTIONS}."
            )


@dataclass
class BuildReport:
    """Structured terminal task summary delivered by Speaker adapters."""

    task_id: str
    status: str
    repo: str
    goal: str
    candidate_commit: Optional[str] = None
    merge_commit: Optional[str] = None
    files_changed: List[str] = field(default_factory=list)
    test_summary: str = ""
    review_summary: str = ""
    failed_stage: Optional[str] = None
    integration_worktree: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def to_markdown(self) -> str:
        """Render a concise [SHARED PLANE REPORT] markdown block."""
        files_block = (
            "\n".join(f"- `{path}`" for path in self.files_changed)
            if self.files_changed
            else "- (none recorded)"
        )
        lines = [
            f"## [SHARED PLANE REPORT — {self.task_id} — {self.status}]",
            "",
            f"- **Task ID:** `{self.task_id}`",
            f"- **Status:** `{self.status}`",
        ]
        if self.failed_stage:
            lines.append(f"- **Failed Stage:** `{self.failed_stage}`")
        lines.append(f"- **Repo:** `{self.repo}`")
        if self.integration_worktree:
            lines.append(f"- **Integration Worktree:** `{self.integration_worktree}`")
        lines.extend(
            [
                f"- **Goal:** {self.goal}",
                f"- **Candidate Commit:** `{self.candidate_commit or 'none'}`",
                f"- **Merge Commit:** `{self.merge_commit or 'none'}`",
                f"- **Tests:** {self.test_summary or 'n/a'}",
                f"- **Review:** {self.review_summary or 'n/a'}",
                "",
                "### Files Changed",
                files_block,
                "",
            ]
        )
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize report fields to a JSON-compatible dictionary."""
        data = asdict(self)
        data["markdown"] = self.to_markdown()
        return data
