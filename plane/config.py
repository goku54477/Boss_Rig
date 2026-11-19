"""Declarative configuration loader for Boss Rig Shared Plane (plane.yaml)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "plane.yaml"


def resolve_repo_path(repo_str: str, base_dir: Optional[Path] = None) -> Path:
    """Resolve a repository path relative to base_dir (or repo root) if not absolute."""
    candidate = Path(repo_str)
    if candidate.is_absolute():
        return candidate.resolve()
    root = base_dir or REPO_ROOT
    return (root / candidate).resolve()


def _parse_scalar(raw: str) -> Any:
    """Parse basic scalar YAML/KV values without external dependencies."""
    val = raw.strip()
    if not val or val.lower() in ("null", "none", "~"):
        return None
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    if (val.startswith('"') and val.endswith('"')) or (
        val.startswith("'") and val.endswith("'")
    ):
        return val[1:-1]
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in inner.split(",")]
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        return val


def _fallback_parse_yaml(text: str) -> Dict[str, Any]:
    """Minimal JSON / indented YAML subset fallback when PyYAML is unavailable."""
    stripped = text.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {}

    clean_lines: List[tuple[int, str]] = []
    for raw_line in text.splitlines():
        line_no_comment = raw_line.split("#", 1)[0].rstrip()
        if line_no_comment.strip():
            indent = len(line_no_comment) - len(line_no_comment.lstrip(" "))
            clean_lines.append((indent, line_no_comment.strip()))

    root: Dict[str, Any] = {}
    stack: List[tuple[int, Any]] = [(-1, root)]
    for idx, (indent, content) in enumerate(clean_lines):
        if content.startswith("- "):
            while len(stack) > 1 and indent < stack[-1][0]:
                stack.pop()
            if len(stack) > 1 and indent == stack[-1][0] and not isinstance(stack[-1][1], list):
                stack.pop()
            parent = stack[-1][1]
            item_val = _parse_scalar(content[2:])
            if isinstance(parent, list):
                parent.append(item_val)
            continue

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if ":" not in content:
            continue
        key, rest = content.split(":", 1)
        key = key.strip()
        rest = rest.strip()
        if isinstance(parent, dict):
            if rest == "":
                is_list = False
                if idx + 1 < len(clean_lines):
                    next_indent, next_content = clean_lines[idx + 1]
                    if next_content.startswith("- ") and next_indent >= indent:
                        is_list = True
                container: Any = [] if is_list else {}
                parent[key] = container
                stack.append((indent, container))
            else:
                parent[key] = _parse_scalar(rest)
    return root


def _load_raw_mapping(path: Path) -> Dict[str, Any]:
    """Load YAML/JSON mapping from path using PyYAML with stdlib fallback."""
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(text)
        return loaded if isinstance(loaded, dict) else {}
    except ImportError:
        # Handle simple list values in fallback parser cleanly by normalizing empty dicts before '-'
        lines = text.splitlines()
        normalized_lines: List[str] = []
        for idx, line in enumerate(lines):
            normalized_lines.append(line)
        data = _fallback_parse_yaml("\n".join(normalized_lines))
        return data


def _extract_value(data: Dict[str, Any], *paths: tuple[str, ...], default: Any = None) -> Any:
    """Look up first matching flat or nested key path in a mapping."""
    for path in paths:
        cur: Any = data
        matched = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                matched = False
                break
        if matched and cur is not None:
            return cur
    return default


@dataclass
class PlaneConfig:
    """Typed configuration for the Boss Rig Shared Plane."""

    router_mode: str = "laya"
    dispatch_mode: str = "propose_first"
    laya_threshold: float = 0.75
    laya_model_kwargs: Dict[str, Any] = field(default_factory=dict)
    default_repo: str = ".disposable-live-repo"
    sandbox_repos: List[str] = field(default_factory=lambda: [".disposable-live-repo"])
    allowed_merge_target: str = "integration"
    enabled_agents: List[str] = field(
        default_factory=lambda: ["opencode-1", "codex-1", "antigravity-1"]
    )
    default_planner: str = "antigravity-1"
    default_implementer: str = "opencode-1"
    poll_interval_sec: float = 0.5
    goldfish_chats_dir: Optional[str] = None
    jsonl_bus_path: Optional[str] = None
    enable_jsonl_bus: bool = True
    reports_dir: str = ".openrig-bridge/reports"
    enable_toasts: bool = True
    prefix_triggers: List[str] = field(
        default_factory=lambda: ["@rig", "@boss-rig", "build a", "create a", "implement"]
    )
    config_path: Optional[Path] = None

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "PlaneConfig":
        """Load PlaneConfig from plane.yaml, falling back to safe defaults if missing."""
        target = path if path is not None else DEFAULT_CONFIG_PATH
        if not target.exists():
            return cls(config_path=target)

        raw = _load_raw_mapping(target)
        cfg = cls(config_path=target)

        cfg.router_mode = str(
            _extract_value(raw, ("router_mode",), ("router", "mode"), default=cfg.router_mode)
        ).strip().lower()
        cfg.dispatch_mode = str(
            _extract_value(
                raw, ("dispatch_mode",), ("dispatch", "mode"), default=cfg.dispatch_mode
            )
        ).strip().lower()
        cfg.laya_threshold = float(
            _extract_value(
                raw,
                ("laya_threshold",),
                ("router", "threshold"),
                ("router", "laya_threshold"),
                default=cfg.laya_threshold,
            )
        )
        model_kwargs = _extract_value(
            raw, ("laya_model_kwargs",), ("router", "model_kwargs"), default=cfg.laya_model_kwargs
        )
        if isinstance(model_kwargs, dict):
            cfg.laya_model_kwargs = dict(model_kwargs)

        cfg.default_repo = str(
            _extract_value(
                raw, ("default_repo",), ("dispatch", "default_repo"), default=cfg.default_repo
            )
        )
        sandbox_repos = _extract_value(
            raw, ("sandbox_repos",), ("dispatch", "sandbox_repos"), default=cfg.sandbox_repos
        )
        if isinstance(sandbox_repos, list) and sandbox_repos:
            cfg.sandbox_repos = [str(r) for r in sandbox_repos]

        cfg.allowed_merge_target = str(
            _extract_value(
                raw,
                ("allowed_merge_target",),
                ("dispatch", "allowed_merge_target"),
                default=cfg.allowed_merge_target,
            )
        )
        enabled = _extract_value(
            raw, ("enabled_agents",), ("agents", "enabled"), default=cfg.enabled_agents
        )
        if isinstance(enabled, list) and enabled:
            cfg.enabled_agents = [str(a) for a in enabled]

        cfg.default_planner = str(
            _extract_value(
                raw, ("default_planner",), ("agents", "default_planner"), default=cfg.default_planner
            )
        )
        cfg.default_implementer = str(
            _extract_value(
                raw,
                ("default_implementer",),
                ("agents", "default_implementer"),
                default=cfg.default_implementer,
            )
        )
        cfg.poll_interval_sec = float(
            _extract_value(
                raw,
                ("poll_interval_sec",),
                ("dispatch", "poll_interval_sec"),
                ("listeners", "poll_interval_sec"),
                default=cfg.poll_interval_sec,
            )
        )
        gdir = _extract_value(
            raw, ("goldfish_chats_dir",), ("listeners", "goldfish_chats_dir"), default=None
        )
        cfg.goldfish_chats_dir = str(gdir) if gdir else None

        bus = _extract_value(
            raw, ("jsonl_bus_path",), ("listeners", "jsonl_bus_path"), default=None
        )
        cfg.jsonl_bus_path = str(bus) if bus else None

        cfg.enable_jsonl_bus = bool(
            _extract_value(
                raw,
                ("enable_jsonl_bus",),
                ("listeners", "enable_jsonl_bus"),
                default=cfg.enable_jsonl_bus,
            )
        )
        cfg.reports_dir = str(
            _extract_value(
                raw, ("reports_dir",), ("speaker", "reports_dir"), default=cfg.reports_dir
            )
        )
        cfg.enable_toasts = bool(
            _extract_value(
                raw, ("enable_toasts",), ("speaker", "enable_toasts"), default=cfg.enable_toasts
            )
        )
        triggers = _extract_value(
            raw, ("prefix_triggers",), ("router", "prefix_triggers"), default=cfg.prefix_triggers
        )
        if isinstance(triggers, list) and triggers:
            cfg.prefix_triggers = [str(t) for t in triggers]

        return cfg
