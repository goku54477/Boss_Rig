"""Shared Plane Daemon, safe SpecBuilder, and `plane doctor` CLI."""

from __future__ import annotations

import argparse
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from orchestrator.chat_watcher import resolve_goldfish_chats_dir, strip_quotes_and_codeblocks
from orchestrator.event_emitter import EventEmitter
from orchestrator.goldfish import resolve_goldfish_db
from orchestrator.models import ApprovalRecord, SeatMode, TaskSpec
from orchestrator.policy import PolicyEngine, PolicyViolation
from orchestrator.security import create_approval_token, normalize_repo_path
from orchestrator.server import get_or_create_auth_token
from orchestrator.store import TaskStore
from orchestrator.task_service import TaskService
from plane.adapters.listeners import BaseListener, GoldfishChatListener, JsonlBusListener
from plane.adapters.speaker import (
    BaseSpeaker,
    CompositeSpeaker,
    ReportFileSpeaker,
    ToastNotificationSpeaker,
)
from plane.config import PlaneConfig, resolve_repo_path
from plane.router import BaseRouter, create_router
from plane.schemas import BuildReport, PlaneMessage, RoutingDecision

TERMINAL_STATES = ("DONE", "REVIEW_FAILED", "CANCELLED", "BLOCKED")

_EXPLICIT_REPO_KV_RE = re.compile(r"\brepo=[\"']?([^\"'\r\n]+?)[\"']?(?:\s|$)", re.IGNORECASE)
_WIN_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z]:[\\/][^\s\"'`]+)")
_POSIX_SYS_PATH_RE = re.compile(
    r"(?:^|\s)(/(?:etc|usr|var|opt|root|mnt|home|tmp|System|Library)[^\s\"'`]*)"
)


def _extract_explicit_repo_mention(text: str) -> Optional[str]:
    """Extract an explicit repository or filesystem path mentioned in message text."""
    kv = _EXPLICIT_REPO_KV_RE.search(text)
    if kv:
        return kv.group(1).strip().rstrip(".,;:")
    win = _WIN_PATH_RE.search(text)
    if win:
        return win.group(1).strip().rstrip(".,;:")
    posix = _POSIX_SYS_PATH_RE.search(text)
    if posix:
        return posix.group(1).strip().rstrip(".,;:")
    return None


def _clean_goal_text(text: str) -> str:
    """Normalize natural-language build request into a clean task goal."""
    stripped = strip_quotes_and_codeblocks(text).strip()
    stripped = re.sub(r"^@(?:boss-rig|rig)\b[:,\s]*", "", stripped, flags=re.IGNORECASE).strip()
    stripped = _EXPLICIT_REPO_KV_RE.sub(" ", stripped).strip()
    return re.sub(r"\s+", " ", stripped)[:500] or "Execute Shared Plane build request"


def _git_changed_files(repo: str, commit_sha: Optional[str]) -> List[str]:
    """Best-effort lookup of files modified in a candidate or merge commit."""
    if not commit_sha or not repo:
        return []
    repo_path = Path(repo)
    if not repo_path.exists():
        return []
    try:
        out = subprocess.check_output(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha],
            cwd=repo_path,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def _format_evidence_summary(raw: Any, fallback: str) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return str(raw.get("status") or raw.get("verdict") or raw.get("evidence") or fallback)
    return fallback


class SpecBuilder:
    """Builds policy-validated TaskSpec objects from natural-language PlaneMessages."""

    def __init__(
        self,
        config: PlaneConfig,
        extra_allowed_repos: Optional[List[Path | str]] = None,
    ) -> None:
        self.config = config
        self.extra_allowed_repos = list(extra_allowed_repos or [])

    def build(self, msg: PlaneMessage, decision: RoutingDecision) -> TaskSpec:
        """Construct and policy-validate a TaskSpec; raises PolicyViolation on scope escape."""
        sanitized = strip_quotes_and_codeblocks(msg.text)
        explicit_repo = _extract_explicit_repo_mention(sanitized)
        raw_repo = explicit_repo or str(resolve_repo_path(self.config.default_repo))

        resolved_repo = PolicyEngine.assert_allowed_repository(
            raw_repo, extra_allowed=self.extra_allowed_repos
        )
        merge_target = self.config.allowed_merge_target or "integration"
        PolicyEngine.assert_allowed_merge_target(merge_target)

        enabled = list(self.config.enabled_agents or ["opencode-1", "codex-1", "antigravity-1"])
        implementer = (
            decision.target_agent
            if decision.target_agent in enabled
            else (
                self.config.default_implementer
                if self.config.default_implementer in enabled
                else enabled[0]
            )
        )
        planner = (
            self.config.default_planner
            if self.config.default_planner in enabled
            else enabled[0]
        )
        if planner == implementer and len(enabled) >= 2:
            planner = next(a for a in enabled if a != implementer)
        reviewer = PolicyEngine.select_reviewer(
            implementer=implementer,
            available_engines=enabled,
            planner=planner,
        )

        return TaskSpec(
            repo=str(resolved_repo),
            goal=_clean_goal_text(msg.text),
            verification_commands=["python -m unittest"],
            constraints=[
                f"Stay strictly inside {resolved_repo.name}",
                f"Merge target locked to {merge_target}",
            ],
            acceptance_criteria=["All verification commands pass cleanly"],
            allowed_merge_target=merge_target,
            assigned_agents={
                "planner": planner,
                "implementer": implementer,
                "reviewer": reviewer,
            },
        )


class SharedPlaneDaemon:
    """Orchestrates Listener -> Router -> TaskService -> Speaker loop."""

    def __init__(
        self,
        config: PlaneConfig,
        service: TaskService,
        listeners: List[BaseListener],
        router: BaseRouter,
        speaker: BaseSpeaker,
        dry_run: bool = True,
    ) -> None:
        self.config = config
        self.service = service
        self.listeners = list(listeners)
        self.router = router
        self.speaker = speaker
        self.dry_run = dry_run
        self.spec_builder = SpecBuilder(
            config=config,
            extra_allowed_repos=service.extra_allowed_repos,
        )

    def baseline(self) -> Dict[str, int]:
        """Baseline existing listener messages and pre-existing terminal tasks."""
        counts: Dict[str, int] = {}
        for idx, listener in enumerate(self.listeners):
            name = f"{type(listener).__name__}-{idx}"
            counts[name] = listener.baseline()

        terminal_claimed = 0
        for task in self.service.task_list(limit=200):
            if task.get("state") in TERMINAL_STATES:
                event_id = f"plane-report:{task['task_id']}"
                if self.service.store.claim_event_durable(
                    event_id=event_id,
                    task_id=task["task_id"],
                    action="PLANE_REPORT_BASELINED",
                    status="IGNORED",
                    detail="Terminal task existed before Shared Plane startup",
                ):
                    terminal_claimed += 1
        counts["terminal_tasks"] = terminal_claimed
        return counts

    def _is_sandbox_auto_dispatch_eligible(self, spec: TaskSpec) -> bool:
        """Auto-dispatch only when explicitly configured AND strictly targeting .disposable-live-repo on integration."""
        if self.dry_run or self.config.dispatch_mode != "auto_dispatch_sandbox":
            return False
        if (spec.allowed_merge_target or "").strip().lower() != "integration":
            return False
        try:
            norm_repo = normalize_repo_path(spec.repo)
        except Exception:
            return False
        if norm_repo.name != ".disposable-live-repo":
            return False
        allowed_sandboxes = {
            normalize_repo_path(resolve_repo_path(item))
            for item in self.config.sandbox_repos
        }
        return norm_repo in allowed_sandboxes

    def _dispatch_message(self, msg: PlaneMessage) -> Dict[str, Any]:
        decision = self.router.classify(msg)

        if decision.action == "IGNORE":
            self.service.store.update_processed_event(
                msg.id, status="IGNORED", detail=decision.reason
            )
            return {
                "action": "IGNORED",
                "message_id": msg.id,
                "reason": decision.reason,
                "router": decision.router_name,
            }

        if decision.action == "CHECK_STATUS":
            recent = self.service.task_list(limit=5)
            self.service.store.update_processed_event(
                msg.id, status="CHECK_STATUS", detail=f"Listed {len(recent)} tasks"
            )
            return {
                "action": "CHECK_STATUS",
                "message_id": msg.id,
                "status": "CHECK_STATUS",
                "task_count": len(recent),
            }

        if decision.action == "ANSWER_QUESTION":
            self.service.store.update_processed_event(
                msg.id, status="ANSWER_DEFERRED", detail="Use /inbox or bridge_cli answer"
            )
            return {
                "action": "ANSWER_QUESTION",
                "message_id": msg.id,
                "status": "ANSWER_DEFERRED",
            }

        try:
            spec = self.spec_builder.build(msg, decision)
        except PolicyViolation as exc:
            self.service.store.update_processed_event(
                msg.id, status="REJECTED_POLICY_VIOLATION", detail=str(exc)
            )
            self.service.emitter.emit(
                "SHARED_PLANE_POLICY_REJECTED",
                task_id="N/A",
                data={"message_id": msg.id, "error": str(exc)},
            )
            return {
                "action": "PROPOSE_BUILD",
                "message_id": msg.id,
                "status": "REJECTED_POLICY_VIOLATION",
                "error": str(exc),
            }

        if self._is_sandbox_auto_dispatch_eligible(spec):
            spec_hash = spec.compute_hash()
            nonce = secrets.token_hex(16)
            secret = get_or_create_auth_token()
            token = create_approval_token(
                spec_id=spec_hash,
                repo=spec.repo,
                conversation_id=msg.conversation_id or "shared_plane",
                message_id=msg.id,
                secret_key=secret,
                nonce=nonce,
                allowed_actions=["execute_local"],
            )
            approval = ApprovalRecord(
                spec_id=spec_hash,
                repo=spec.repo,
                source="operator_cli",
                conversation_id=msg.conversation_id or "shared_plane",
                message_id=msg.id,
                approval_token=token,
                approved_by="shared_plane_auto_sandbox",
                nonce=nonce,
                allowed_actions=["execute_local"],
            )
            submit_res = self.service.task_submit(
                spec=spec,
                idempotency_key=f"plane-{msg.source}-{msg.id}",
                approved=True,
                approved_hash=spec_hash,
                conversation_id=msg.conversation_id,
                approval_record=approval,
            )
            task_id = submit_res.get("task_id")
            self.service.store.update_processed_event(
                msg.id, status="AUTO_DISPATCHED", task_id=task_id, detail=decision.reason
            )
            return {
                "action": "PROPOSE_BUILD",
                "message_id": msg.id,
                "status": "AUTO_DISPATCHED",
                "task_id": task_id,
                "target_agent": spec.assigned_agents.get("implementer"),
            }

        prop = self.service.propose_task(
            spec=spec,
            source=f"shared_plane:{msg.source}",
            conversation_id=msg.conversation_id,
            message_id=msg.id,
        )
        self.service.store.update_processed_event(
            msg.id,
            status="PROPOSED",
            detail=f"Proposal {prop['proposal_id']} created (target={spec.assigned_agents.get('implementer')})",
        )
        self.service.emitter.emit(
            "SHARED_PLANE_PROPOSAL",
            task_id=prop["proposal_id"],
            spec_id=prop["spec_id"],
            data=prop,
            notification_title="[Shared Plane] Proposal Ready",
            notification_body=f"{prop['proposal_id']}: {spec.goal[:70]}",
        )
        return {
            "action": "PROPOSE_BUILD",
            "message_id": msg.id,
            "status": "PROPOSED",
            "proposal_id": prop["proposal_id"],
            "target_agent": spec.assigned_agents.get("implementer"),
        }

    def _emit_terminal_reports(self) -> List[Dict[str, Any]]:
        emitted: List[Dict[str, Any]] = []
        for task in self.service.task_list(limit=50):
            state = task.get("state")
            if state not in TERMINAL_STATES:
                continue
            task_id = task["task_id"]
            report_event_id = f"plane-report:{task_id}"
            if self.service.store.is_event_claimed(report_event_id):
                continue
            if not self.service.store.claim_event_durable(
                event_id=report_event_id,
                task_id=task_id,
                action="PLANE_REPORT",
                status="SPOKEN",
                detail=f"Terminal report emitted for state {state}",
            ):
                continue

            res = self.service.task_result(task_id)
            commit_ref = res.get("merge_commit") or res.get("candidate_commit")
            files_changed = _git_changed_files(res.get("repo", ""), commit_ref)
            report = BuildReport(
                task_id=task_id,
                status=state,
                repo=res.get("repo", ""),
                goal=res.get("goal", ""),
                candidate_commit=res.get("candidate_commit"),
                merge_commit=res.get("merge_commit"),
                files_changed=files_changed,
                test_summary=_format_evidence_summary(res.get("test_evidence"), state),
                review_summary=_format_evidence_summary(res.get("review_findings"), "n/a"),
            )
            speak_res = self.speaker.speak(report)
            emitted.append(
                {
                    "action": "REPORT_SPOKEN",
                    "task_id": task_id,
                    "status": state,
                    "speaker": speak_res,
                }
            )
        return emitted

    def step_once(self) -> List[Dict[str, Any]]:
        """Execute one poll-route-propose/dispatch-speak tick."""
        actions: List[Dict[str, Any]] = []
        for listener in self.listeners:
            for msg in listener.poll():
                result = self._dispatch_message(msg)
                if result.get("action") != "IGNORED":
                    actions.append(result)
        actions.extend(self._emit_terminal_reports())
        return actions

    def run_forever(self) -> None:
        """Baseline existing state and continuously poll Shared Plane listeners."""
        self.baseline()
        try:
            while True:
                self.step_once()
                time.sleep(max(0.1, self.config.poll_interval_sec))
        except KeyboardInterrupt:
            return


def _check_wsl_openrig() -> Tuple[str, str]:
    """Read-only probe of OpenRig WSL CLI version and daemon status."""
    try:
        proc = subprocess.run(
            ["wsl", "-e", "bash", "-ic", "rig --version && rig daemon status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        combined = " | ".join(
            line.strip()
            for line in (proc.stdout + "\n" + proc.stderr).splitlines()
            if line.strip()
        )
        if proc.returncode == 0 and "0.7.19" in combined:
            return "PASS", combined
        if proc.returncode == 0:
            return "WARN", combined or "rig responded with unexpected version"
        return "WARN", combined or f"wsl exited with code {proc.returncode}"
    except Exception as exc:
        return "WARN", f"WSL OpenRig probe unavailable ({exc})"


def run_doctor(config: PlaneConfig, store: Optional[TaskStore] = None) -> List[Dict[str, str]]:
    """Run read-only environment diagnostics for Boss Rig Shared Plane."""
    checks: List[Dict[str, str]] = []

    checks.append(
        {
            "name": "python",
            "status": "PASS",
            "detail": sys.version.split()[0],
        }
    )
    checks.append(
        {
            "name": "config",
            "status": "PASS",
            "detail": (
                f"router={config.router_mode}, dispatch={config.dispatch_mode}, "
                f"agents={config.enabled_agents}, merge_target={config.allowed_merge_target}"
            ),
        }
    )

    chats_dir = (
        Path(config.goldfish_chats_dir)
        if config.goldfish_chats_dir
        else resolve_goldfish_chats_dir()
    )
    if chats_dir and chats_dir.exists():
        chat_count = sum(1 for p in chats_dir.glob("*.json") if p.name.lower() != "inbox.json")
        checks.append(
            {
                "name": "goldfish_chats",
                "status": "PASS",
                "detail": f"read-only dir={chats_dir} ({chat_count} chat JSON files)",
            }
        )
    else:
        checks.append(
            {
                "name": "goldfish_chats",
                "status": "WARN",
                "detail": "Goldfish chats directory not found",
            }
        )

    gdb = resolve_goldfish_db()
    if gdb and gdb.exists():
        try:
            conn = sqlite3.connect(f"file:{gdb.as_posix()}?mode=ro&immutable=1", uri=True, timeout=3.0)
            conn.execute("SELECT 1").fetchone()
            conn.close()
            checks.append(
                {
                    "name": "goldfish_db",
                    "status": "PASS",
                    "detail": f"read-only immutable open ok ({gdb})",
                }
            )
        except Exception as exc:
            checks.append({"name": "goldfish_db", "status": "WARN", "detail": str(exc)})
    else:
        checks.append({"name": "goldfish_db", "status": "WARN", "detail": "goldfish.db not found"})

    try:
        import laya  # type: ignore

        ver = getattr(laya, "__version__", "unknown")
        checks.append(
            {
                "name": "laya",
                "status": "PASS",
                "detail": f"laya {ver} installed (text-only vega=False enforced)",
            }
        )
    except Exception as exc:
        checks.append(
            {
                "name": "laya",
                "status": "WARN",
                "detail": f"not importable ({exc}); HeuristicRouter fallback active",
            }
        )

    wsl_status, wsl_detail = _check_wsl_openrig()
    checks.append({"name": "openrig_wsl", "status": wsl_status, "detail": wsl_detail})

    sandbox_path = resolve_repo_path(config.default_repo)
    if sandbox_path.exists():
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=sandbox_path,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
            st = "PASS" if branch == "integration" else "WARN"
            detail = f"{sandbox_path} (branch={branch})"
        except Exception as exc:
            st = "WARN"
            detail = f"{sandbox_path} (git branch probe failed: {exc})"
        checks.append({"name": "sandbox_repo", "status": st, "detail": detail})
    else:
        checks.append(
            {
                "name": "sandbox_repo",
                "status": "WARN",
                "detail": f"default_repo does not exist: {sandbox_path}",
            }
        )

    try:
        active_store = store or TaskStore()
        modes = active_store.list_seat_modes()
        held = [
            seat
            for seat in config.enabled_agents
            if modes.get(seat, {}).get("mode") == SeatMode.MANUAL_HOLD
        ]
        if held:
            checks.append(
                {
                    "name": "seat_modes",
                    "status": "WARN",
                    "detail": (
                        f"Enabled seat(s) currently in MANUAL_HOLD: {held}. "
                        f"Release via: python -m orchestrator.cli seat release <seat_id>"
                    ),
                }
            )
        else:
            checks.append(
                {
                    "name": "seat_modes",
                    "status": "PASS",
                    "detail": f"All enabled agents ({config.enabled_agents}) in AUTOMATED mode",
                }
            )
    except Exception as exc:
        checks.append({"name": "seat_modes", "status": "WARN", "detail": str(exc)})

    return checks


def build_daemon_from_config(config: PlaneConfig, dry_run: bool = True) -> SharedPlaneDaemon:
    """Wire default TaskStore, EventEmitter, TaskService, Listeners, Router, and Speakers."""
    store = TaskStore()
    emitter = EventEmitter(enable_notifications=config.enable_toasts)
    extra_repos = [resolve_repo_path(r) for r in [config.default_repo, *config.sandbox_repos]]
    service = TaskService(
        store=store,
        event_emitter=emitter,
        extra_allowed_repos=extra_repos,
        dry_run=dry_run,
    )
    chats_dir = (
        Path(config.goldfish_chats_dir)
        if config.goldfish_chats_dir
        else resolve_goldfish_chats_dir()
    )
    listeners: List[BaseListener] = [GoldfishChatListener(chats_dir=chats_dir, store=store)]
    if config.enable_jsonl_bus:
        bus_path = Path(config.jsonl_bus_path).expanduser() if config.jsonl_bus_path else None
        listeners.append(JsonlBusListener(bus_path=bus_path, store=store))

    router = create_router(config)
    reports_dir = resolve_repo_path(config.reports_dir)
    speakers: List[BaseSpeaker] = [ReportFileSpeaker(reports_dir=reports_dir)]
    if config.enable_toasts:
        speakers.append(ToastNotificationSpeaker(emitter=emitter))
    return SharedPlaneDaemon(
        config=config,
        service=service,
        listeners=listeners,
        router=router,
        speaker=CompositeSpeaker(speakers),
        dry_run=dry_run,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plane.daemon", description="Boss Rig Shared Plane CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doc = sub.add_parser("doctor", help="Run read-only Shared Plane health checks")
    p_doc.add_argument("--config", type=Path, default=None, help="Path to plane.yaml")

    p_up = sub.add_parser("up", help="Start Shared Plane Listener/Router/Speaker daemon")
    p_up.add_argument("--config", type=Path, default=None, help="Path to plane.yaml")
    p_up.add_argument("--live", action="store_true", help="Enable live dispatch (defaults to dry-run)")
    p_up.add_argument("--once", action="store_true", help="Baseline and run a single poll tick")

    args = parser.parse_args(argv)
    config = PlaneConfig.load(args.config)

    if args.command == "doctor":
        checks = run_doctor(config)
        print("Boss Rig Shared Plane Doctor")
        print("=" * 60)
        for item in checks:
            print(f"[{item['status']}] {item['name']}: {item['detail']}")
        return 0

    if args.command == "up":
        daemon = build_daemon_from_config(config, dry_run=not args.live)
        if args.once:
            daemon.baseline()
            print(daemon.step_once())
            return 0
        daemon.run_forever()
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
