"""Pluggable intent routers for Boss Rig Shared Plane (Laya System-1 + Heuristic fallback)."""

from __future__ import annotations

import sys
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from orchestrator.chat_watcher import strip_quotes_and_codeblocks
from plane.config import PlaneConfig
from plane.schemas import PlaneMessage, RoutingDecision, VALID_ACTIONS

REPORT_MARKER = "[SHARED PLANE REPORT"
SHARED_PLANE_MARKERS = (REPORT_MARKER, "[SHARED PLANE]")

AGENT_DESCRIPTIONS: Dict[str, str] = {
    "opencode-1": "opencode-1 autonomous coding agent for general implementation tasks",
    "codex-1": "codex-1 OpenAI Codex coding agent when user asks for codex or fast implementation",
    "antigravity-1": "antigravity-1 Antigravity coding/planning/review agent",
}

_CASUAL_FALSE_POSITIVES = (
    "build trust",
    "build rapport",
    "build consensus",
    "build relationships",
    "build momentum",
    "build awareness",
    "build a relationship",
    "thinking about how",
    "wondering how",
    "curious how",
)

_CODE_NOUNS = (
    "app",
    "api",
    "bot",
    "calculator",
    "cli",
    "command",
    "component",
    "daemon",
    "dashboard",
    "endpoint",
    "feature",
    "function",
    "library",
    "module",
    "package",
    "parser",
    "pipeline",
    "script",
    "server",
    "service",
    "test",
    "tests",
    "timer",
    "tool",
    "ui",
    "utility",
    "validator",
    "website",
    "widget",
)


def _prefilter_message(msg: PlaneMessage) -> Tuple[Optional[str], str]:
    """Enforce loop-of-doom safety filters before invoking any router."""
    if msg.role != "user":
        return None, f"Ignored non-user role '{msg.role}'"
    raw_text = msg.text or ""
    if any(marker in raw_text for marker in SHARED_PLANE_MARKERS):
        return None, "Ignored Shared Plane report/log marker"
    sanitized = strip_quotes_and_codeblocks(raw_text).strip()
    if not sanitized:
        return None, "Ignored empty/quoted-only message"
    return sanitized, ""


def _pick_agent_hint(text: str, enabled_agents: List[str], default_agent: str) -> str:
    """Resolve explicit agent mention in message text, constrained to enabled agents."""
    lower = text.lower()
    for agent_id in enabled_agents:
        short = agent_id.split("-", 1)[0]
        if agent_id in lower or short in lower:
            return agent_id
    if default_agent in enabled_agents:
        return default_agent
    return enabled_agents[0] if enabled_agents else "opencode-1"


class BaseRouter(ABC):
    """Base contract for Shared Plane intent routers."""

    @abstractmethod
    def classify(self, msg: PlaneMessage) -> RoutingDecision:
        """Classify an incoming normalized message into a RoutingDecision."""


class HeuristicRouter(BaseRouter):
    """Zero-dependency deterministic fallback intent router."""

    def __init__(
        self,
        prefix_triggers: Optional[List[str]] = None,
        enabled_agents: Optional[List[str]] = None,
        default_agent: str = "opencode-1",
    ) -> None:
        self.prefix_triggers = prefix_triggers or [
            "@rig",
            "@boss-rig",
            "build a",
            "create a",
            "implement",
        ]
        self.enabled_agents = list(enabled_agents or ["opencode-1", "codex-1", "antigravity-1"])
        self.default_agent = (
            default_agent
            if default_agent in self.enabled_agents
            else (self.enabled_agents[0] if self.enabled_agents else "opencode-1")
        )

    def classify(self, msg: PlaneMessage) -> RoutingDecision:
        started = time.perf_counter()
        sanitized, skip_reason = _prefilter_message(msg)
        if sanitized is None:
            return RoutingDecision(
                action="IGNORE",
                confidence=1.0,
                target_agent=self.default_agent,
                reason=skip_reason,
                router_name="heuristic",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        lower = sanitized.lower()
        target = _pick_agent_hint(sanitized, self.enabled_agents, self.default_agent)

        if any(phrase in lower for phrase in _CASUAL_FALSE_POSITIVES):
            return RoutingDecision(
                action="IGNORE",
                confidence=0.9,
                target_agent=target,
                reason="Casual non-coding phrase matched exclusion list",
                router_name="heuristic",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        if "@boss-rig answer" in lower or "@rig answer" in lower:
            return RoutingDecision(
                action="ANSWER_QUESTION",
                confidence=0.95,
                target_agent=target,
                reason="Explicit operator answer command",
                router_name="heuristic",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        if (
            "@rig status" in lower
            or "@boss-rig status" in lower
            or "check task status" in lower
            or "what is the task status" in lower
        ):
            return RoutingDecision(
                action="CHECK_STATUS",
                confidence=0.9,
                target_agent=target,
                reason="Explicit status query",
                router_name="heuristic",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        # Explicit @rig / @boss-rig invocation (when not status/answer)
        if lower.startswith("@rig ") or lower.startswith("@boss-rig ") or " @rig " in lower or " @boss-rig " in lower:
            return RoutingDecision(
                action="PROPOSE_BUILD",
                confidence=0.95,
                target_agent=target,
                reason="Explicit @rig/@boss-rig trigger",
                router_name="heuristic",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        # Natural coding intent triggers
        has_trigger = any(trig.lower() in lower for trig in self.prefix_triggers if not trig.startswith("@"))
        has_verb = any(v in lower for v in ("build ", "create ", "implement ", "write ", "fix ", "refactor "))
        has_code_noun = any(f" {n}" in lower or f"{n}." in lower or lower.endswith(n) for n in _CODE_NOUNS)

        if (has_trigger and has_code_noun) or (has_verb and has_code_noun and not lower.startswith("i was thinking")):
            return RoutingDecision(
                action="PROPOSE_BUILD",
                confidence=0.82,
                target_agent=target,
                reason="Matched coding build intent heuristic",
                router_name="heuristic",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        return RoutingDecision(
            action="IGNORE",
            confidence=0.85,
            target_agent=target,
            reason="No coding build intent detected",
            router_name="heuristic",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )


class LayaRouter(BaseRouter):
    """Laya System-1 (~33ms) text-only intent router wrapping laya.Router."""

    def __init__(
        self,
        threshold: float = 0.75,
        enabled_agents: Optional[List[str]] = None,
        default_agent: str = "opencode-1",
        model_kwargs: Optional[Dict[str, Any]] = None,
        backend: Optional[Any] = None,
        router_cls: Optional[Any] = None,
        fallback_router: Optional[BaseRouter] = None,
        action_threshold: Optional[float] = None,
    ) -> None:
        self.threshold = float(threshold)
        self.action_threshold = float(action_threshold if action_threshold is not None else threshold)
        self.enabled_agents = list(enabled_agents or ["opencode-1", "codex-1", "antigravity-1"])
        self.default_agent = (
            default_agent
            if default_agent in self.enabled_agents
            else (self.enabled_agents[0] if self.enabled_agents else "opencode-1")
        )
        self.model_kwargs = dict(model_kwargs or {})
        self._owns_backend = backend is None and router_cls is None
        self._fallback_router = fallback_router

        if backend is not None:
            self._backend = backend
        else:
            cls_to_use = router_cls
            if cls_to_use is None:
                from laya import Router as LayaNativeRouter  # type: ignore

                cls_to_use = LayaNativeRouter
            kwargs = dict(self.model_kwargs)
            # Hard constraint: keep Laya text-only (never enable vegaml/multimodal)
            kwargs["vega"] = False
            self._backend = cls_to_use(**kwargs)

    def _build_questions(self) -> Dict[str, Dict[str, Any]]:
        agent_criteria = {
            agent_id: AGENT_DESCRIPTIONS.get(agent_id, f"{agent_id} coding seat")
            for agent_id in self.enabled_agents
        }
        return {
            "is_build_task": {
                "type": "noul",
                "instructions": (
                    "Is the user in `message` explicitly asking to build, implement, "
                    "fix, or modify software code?"
                ),
            },
            "action": {
                "type": "choice",
                "instructions": "Which Shared Plane action best matches `message`?",
                "criteria": {
                    "IGNORE": "casual conversation, theory, or non-executable discussion",
                    "PROPOSE_BUILD": "request to build, implement, fix, or refactor code",
                    "ANSWER_QUESTION": "direct answer to a pending agent question",
                    "CHECK_STATUS": "asking for current task or build status",
                },
            },
            "target_agent": {
                "type": "choice",
                "instructions": "Which coding agent seat should implement `message`?",
                "criteria": agent_criteria,
            },
        }

    def _parse_backend_output(
        self, raw: Any
    ) -> Tuple[float, Optional[bool], str, Optional[float], Optional[float], str]:
        """Support both Laya 0.4.31 Router.predict (`answers` dict) and flat test mocks."""
        if not isinstance(raw, dict):
            return 0.0, False, "IGNORE", None, None, self.default_agent

        answers = raw.get("answers") if isinstance(raw.get("answers"), dict) else raw

        build_entry = answers.get("is_build_task")
        build_prob = 0.0
        build_answer: Optional[bool] = None
        if isinstance(build_entry, dict):
            if "noul" in build_entry:
                build_prob = float(build_entry["noul"])
                build_answer = build_prob >= self.threshold
            elif "probability" in build_entry:
                build_prob = float(build_entry["probability"])
                if "answer" in build_entry:
                    build_answer = bool(build_entry["answer"])
                else:
                    build_answer = build_prob >= self.threshold
        elif isinstance(build_entry, (int, float)):
            build_prob = float(build_entry)
            build_answer = build_prob >= self.threshold

        action_entry = answers.get("action")
        raw_action = "IGNORE"
        action_prob: Optional[float] = None
        propose_build_prob: Optional[float] = None
        if isinstance(action_entry, dict):
            if action_entry.get("choice"):
                raw_action = str(action_entry["choice"])
            probs = action_entry.get("probabilities")
            if isinstance(probs, dict):
                if raw_action in probs:
                    action_prob = float(probs[raw_action])
                if "PROPOSE_BUILD" in probs:
                    propose_build_prob = float(probs["PROPOSE_BUILD"])
            if action_prob is None:
                if "answer_confidence" in action_entry:
                    action_prob = float(action_entry["answer_confidence"])
                elif "probability" in action_entry:
                    action_prob = float(action_entry["probability"])
        elif isinstance(action_entry, str):
            raw_action = action_entry
        if raw_action not in VALID_ACTIONS:
            raw_action = "IGNORE"

        target_entry = answers.get("target_agent")
        raw_target = self.default_agent
        if isinstance(target_entry, dict) and target_entry.get("choice"):
            raw_target = str(target_entry["choice"])
        elif isinstance(target_entry, str):
            raw_target = target_entry
        if raw_target not in self.enabled_agents:
            raw_target = self.default_agent

        return build_prob, build_answer, raw_action, action_prob, propose_build_prob, raw_target

    def classify(self, msg: PlaneMessage) -> RoutingDecision:
        started = time.perf_counter()
        sanitized, skip_reason = _prefilter_message(msg)
        if sanitized is None:
            return RoutingDecision(
                action="IGNORE",
                confidence=1.0,
                target_agent=self.default_agent,
                reason=skip_reason,
                router_name="laya",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        # Classify strictly on the current user message so prior chat turns cannot poison System-1 heads.
        state: Dict[str, Any] = {"message": sanitized}
        questions = self._build_questions()

        try:
            raw = self._backend.predict(state=state, questions=questions)
        except Exception as exc:
            if self._owns_backend and self._fallback_router is not None:
                sys.stderr.write(
                    f"[WARN] Laya predict failed ({exc}); falling back to HeuristicRouter.\n"
                )
                return self._fallback_router.classify(msg)
            raise

        (
            build_prob,
            build_answer,
            raw_action,
            action_prob,
            propose_build_prob,
            target_agent,
        ) = self._parse_backend_output(raw)
        latency_ms = (time.perf_counter() - started) * 1000.0

        is_strong_build = build_prob >= self.threshold and build_answer is not False
        effective_action_prob = action_prob if action_prob is not None else 1.0
        is_strong_non_build = (
            raw_action in ("ANSWER_QUESTION", "CHECK_STATUS")
            and effective_action_prob >= self.action_threshold
        )

        if is_strong_build:
            if is_strong_non_build and (
                propose_build_prob is None or effective_action_prob > propose_build_prob
            ):
                return RoutingDecision(
                    action=raw_action,
                    confidence=effective_action_prob,
                    target_agent=target_agent,
                    reason=(
                        f"Laya classified {raw_action} "
                        f"({effective_action_prob:.2f} >= {self.action_threshold:.2f})"
                    ),
                    router_name="laya",
                    latency_ms=latency_ms,
                )
            if (
                raw_action == "IGNORE"
                and action_prob is not None
                and action_prob >= self.action_threshold
            ):
                return RoutingDecision(
                    action="IGNORE",
                    confidence=action_prob,
                    target_agent=target_agent,
                    reason=f"Laya classified IGNORE ({action_prob:.2f} >= {self.action_threshold:.2f})",
                    router_name="laya",
                    latency_ms=latency_ms,
                )
            return RoutingDecision(
                action="PROPOSE_BUILD",
                confidence=build_prob,
                target_agent=target_agent,
                reason=f"Laya build probability {build_prob:.2f} >= {self.threshold:.2f}",
                router_name="laya",
                latency_ms=latency_ms,
            )

        if is_strong_non_build:
            return RoutingDecision(
                action=raw_action,
                confidence=effective_action_prob,
                target_agent=target_agent,
                reason=(
                    f"Laya classified {raw_action} "
                    f"({effective_action_prob:.2f} >= {self.action_threshold:.2f})"
                ),
                router_name="laya",
                latency_ms=latency_ms,
            )

        return RoutingDecision(
            action="IGNORE",
            confidence=max(0.0, 1.0 - build_prob),
            target_agent=target_agent,
            reason=f"Below Laya threshold ({build_prob:.2f} < {self.threshold:.2f}) or non-build",
            router_name="laya",
            latency_ms=latency_ms,
        )


def create_router(
    config: PlaneConfig,
    force_missing_laya: bool = False,
    backend: Optional[Any] = None,
) -> BaseRouter:
    """Instantiate configured router with graceful HeuristicRouter fallback if Laya is unavailable."""
    heuristic = HeuristicRouter(
        prefix_triggers=config.prefix_triggers,
        enabled_agents=config.enabled_agents,
        default_agent=config.default_implementer,
    )

    if config.router_mode != "laya":
        return heuristic

    if force_missing_laya:
        sys.stderr.write(
            "[WARN] Laya router unavailable (forced fallback); using HeuristicRouter.\n"
        )
        return heuristic

    try:
        return LayaRouter(
            threshold=config.laya_threshold,
            enabled_agents=config.enabled_agents,
            default_agent=config.default_implementer,
            model_kwargs=config.laya_model_kwargs,
            backend=backend,
            fallback_router=heuristic,
        )
    except Exception as exc:
        sys.stderr.write(
            f"[WARN] Laya router unavailable ({exc}); falling back to HeuristicRouter.\n"
        )
        return heuristic
