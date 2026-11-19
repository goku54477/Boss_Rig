"""Offline unit and integration tests for Boss Rig Shared Plane v1."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from unittest.mock import patch

from orchestrator.event_emitter import EventEmitter
from orchestrator.mcp_server import McpServer
from orchestrator.models import TaskSpec
from orchestrator.security import create_approval_token
from orchestrator.server import TaskServer, get_or_create_auth_token
from orchestrator.store import TaskStore
from orchestrator.task_service import TaskService
from plane.adapters.listeners import GoldfishChatListener, JsonlBusListener
from plane.adapters.speaker import (
    CompositeSpeaker,
    ReportFileSpeaker,
    ToastNotificationSpeaker,
)
from plane.config import PlaneConfig, _fallback_parse_yaml
from plane.daemon import SharedPlaneDaemon, SpecBuilder, run_doctor
from plane.router import HeuristicRouter, LayaRouter, create_router
from plane.schemas import BuildReport, PlaneMessage, RoutingDecision


class TestPlaneSchemasAndConfig(unittest.TestCase):
    def test_build_report_markdown_format(self) -> None:
        report = BuildReport(
            task_id="T-100",
            status="DONE",
            repo="C:/Users/jacob/Boss Agent/.disposable-live-repo",
            goal="Build a CLI calculator",
            candidate_commit="abc1234",
            merge_commit="def5678",
            files_changed=["calc.py", "test_calc.py"],
            test_summary="2/2 tests passed",
            review_summary="Approved by antigravity-1",
        )
        md = report.to_markdown()
        self.assertIn("[SHARED PLANE REPORT", md)
        self.assertIn("T-100", md)
        self.assertIn("calc.py", md)
        self.assertIn("test_calc.py", md)
        self.assertIn("def5678", md)

        payload = report.to_dict()
        serialized = json.dumps(payload)
        self.assertIn("T-100", serialized)
        self.assertEqual(payload["status"], "DONE")
        self.assertEqual(payload["files_changed"], ["calc.py", "test_calc.py"])

    def test_routing_decision_validates_action(self) -> None:
        msg = PlaneMessage(
            id="m-1",
            source="goldfish",
            conversation_id="conv-1",
            role="user",
            text="Build a calculator",
        )
        self.assertEqual(msg.role, "user")
        decision = RoutingDecision(
            action="PROPOSE_BUILD",
            confidence=0.91,
            target_agent="codex-1",
            reason="Explicit build request",
            router_name="laya",
            latency_ms=18.5,
        )
        self.assertEqual(decision.target_agent, "codex-1")
        with self.assertRaises(ValueError):
            RoutingDecision(
                action="INVALID_ACTION",
                confidence=0.5,
                target_agent="opencode-1",
                reason="bad",
                router_name="test",
            )

    def test_config_loads_defaults_when_file_missing(self) -> None:
        cfg = PlaneConfig.load(Path(tempfile.gettempdir()) / "nonexistent_plane_xyz_123.yaml")
        self.assertEqual(cfg.router_mode, "laya")
        self.assertEqual(cfg.dispatch_mode, "propose_first")
        self.assertGreaterEqual(cfg.laya_threshold, 0.7)
        self.assertEqual(cfg.laya_threshold, 0.75)
        self.assertEqual(cfg.poll_interval_sec, 0.5)
        self.assertEqual(cfg.enabled_agents, ["opencode-1", "codex-1", "antigravity-1"])
        self.assertIn(".disposable-live-repo", cfg.default_repo)
        self.assertEqual(cfg.allowed_merge_target, "integration")

    def test_config_loads_yaml_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "plane.yaml"
            cfg_path.write_text(
                "\n".join(
                    [
                        "router:",
                        "  mode: heuristic",
                        "  threshold: 0.82",
                        "dispatch:",
                        "  mode: auto_dispatch_sandbox",
                        "  default_repo: .disposable-live-repo",
                        "  poll_interval_sec: 1.25",
                        "agents:",
                        "  enabled:",
                        "    - codex-1",
                        "    - antigravity-1",
                        "  default_planner: antigravity-1",
                        "  default_implementer: codex-1",
                    ]
                ),
                encoding="utf-8",
            )
            cfg = PlaneConfig.load(cfg_path)
            self.assertEqual(cfg.router_mode, "heuristic")
            self.assertAlmostEqual(cfg.laya_threshold, 0.82)
            self.assertEqual(cfg.dispatch_mode, "auto_dispatch_sandbox")
            self.assertEqual(cfg.poll_interval_sec, 1.25)
            self.assertEqual(cfg.enabled_agents, ["codex-1", "antigravity-1"])
            self.assertEqual(cfg.default_implementer, "codex-1")


class FakeLayaBackend:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.last_call = None

    def predict(self, state, questions: dict, **kwargs):
        self.last_call = (state, questions, kwargs)
        return self.response


class TestPlaneRouters(unittest.TestCase):
    def test_laya_router_dispatches_high_confidence_build_intent(self) -> None:
        fake = FakeLayaBackend(
            {
                "is_build_task": {"probability": 0.92, "answer": True},
                "action": {"choice": "PROPOSE_BUILD"},
                "target_agent": {"choice": "opencode-1"},
            }
        )
        router = LayaRouter(threshold=0.75, backend=fake)
        msg = PlaneMessage(
            id="m1",
            source="goldfish",
            conversation_id="c1",
            role="user",
            text="I want to build a Python CLI calculator with unit tests",
        )
        decision = router.classify(msg)
        self.assertEqual(decision.action, "PROPOSE_BUILD")
        self.assertEqual(decision.target_agent, "opencode-1")
        self.assertGreaterEqual(decision.confidence, 0.9)
        self.assertEqual(decision.router_name, "laya")
        self.assertIsNotNone(fake.last_call)

    def test_laya_router_parses_real_laya_0431_answers_shape(self) -> None:
        fake = FakeLayaBackend(
            {
                "model": "laya-rl-agent",
                "answers": {
                    "is_build_task": {"type": "noul", "noul": 0.89, "confidence": 0.89},
                    "action": {"type": "choice", "choice": "PROPOSE_BUILD", "confidence": 0.86},
                    "target_agent": {"type": "choice", "choice": "codex-1", "confidence": 0.84},
                },
            }
        )
        router = LayaRouter(
            threshold=0.75,
            enabled_agents=["opencode-1", "codex-1", "antigravity-1"],
            backend=fake,
        )
        msg = PlaneMessage(
            id="m1b",
            source="goldfish",
            conversation_id="c1",
            role="user",
            text="Have codex build a parser module in python",
        )
        decision = router.classify(msg)
        self.assertEqual(decision.action, "PROPOSE_BUILD")
        self.assertEqual(decision.target_agent, "codex-1")
        self.assertGreaterEqual(decision.confidence, 0.89)

    def test_laya_router_ignores_casual_chat_below_threshold(self) -> None:
        fake = FakeLayaBackend(
            {
                "is_build_task": {"probability": 0.22, "answer": False},
                "action": {"choice": "IGNORE"},
                "target_agent": {"choice": "opencode-1"},
            }
        )
        router = LayaRouter(threshold=0.75, backend=fake)
        msg = PlaneMessage(
            id="m2",
            source="goldfish",
            conversation_id="c1",
            role="user",
            text="We should build trust with customers this quarter",
        )
        decision = router.classify(msg)
        self.assertEqual(decision.action, "IGNORE")

    def test_laya_router_ignores_shared_plane_report_marker_and_non_user_role(self) -> None:
        fake = FakeLayaBackend(
            {
                "is_build_task": {"probability": 0.99, "answer": True},
                "action": {"choice": "PROPOSE_BUILD"},
                "target_agent": {"choice": "opencode-1"},
            }
        )
        router = LayaRouter(threshold=0.75, backend=fake)
        report_msg = PlaneMessage(
            id="m3",
            source="goldfish",
            conversation_id="c1",
            role="user",
            text="[SHARED PLANE REPORT — T-100 — DONE] Built a calculator",
        )
        self.assertEqual(router.classify(report_msg).action, "IGNORE")
        self.assertIsNone(fake.last_call)

        assistant_msg = PlaneMessage(
            id="m4",
            source="goldfish",
            conversation_id="c1",
            role="assistant",
            text="Sure, I can build a calculator for you!",
        )
        self.assertEqual(router.classify(assistant_msg).action, "IGNORE")
        self.assertIsNone(fake.last_call)

    def test_laya_router_strips_quoted_and_codeblock_text_before_classifying(self) -> None:
        fake = FakeLayaBackend(
            {
                "is_build_task": {"probability": 0.99, "answer": True},
                "action": {"choice": "PROPOSE_BUILD"},
                "target_agent": {"choice": "opencode-1"},
            }
        )
        router = LayaRouter(threshold=0.75, backend=fake)
        quoted_only = PlaneMessage(
            id="m5",
            source="goldfish",
            conversation_id="c1",
            role="user",
            text="> build a calculator\n```bash\nbuild a CLI app\n```",
        )
        decision = router.classify(quoted_only)
        self.assertEqual(decision.action, "IGNORE")
        self.assertIsNone(fake.last_call)

    def test_laya_router_enforces_text_only_vega_false(self) -> None:
        captured_kwargs = {}

        class FakeRouterCls:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

            def predict(self, state, questions, **kwargs):
                return {"answers": {"is_build_task": {"noul": 0.1}, "action": {"choice": "IGNORE"}}}

        router = LayaRouter(
            threshold=0.75,
            model_kwargs={"vega": True, "default": "english"},
            router_cls=FakeRouterCls,
        )
        msg = PlaneMessage(
            id="m6",
            source="goldfish",
            conversation_id="c1",
            role="user",
            text="Just checking in",
        )
        decision = router.classify(msg)
        self.assertEqual(decision.action, "IGNORE")
        self.assertFalse(captured_kwargs["vega"])
        self.assertEqual(captured_kwargs["default"], "english")

    def test_create_router_falls_back_cleanly_when_laya_unavailable(self) -> None:
        cfg = PlaneConfig(router_mode="laya")
        router = create_router(cfg, force_missing_laya=True)
        self.assertIsInstance(router, HeuristicRouter)

    def test_heuristic_router_handles_build_and_casual_and_codex_hint(self) -> None:
        router = HeuristicRouter(
            prefix_triggers=["@rig", "@boss-rig", "build a", "create a", "implement"],
            enabled_agents=["opencode-1", "codex-1", "antigravity-1"],
            default_agent="opencode-1",
        )
        casual1 = PlaneMessage(
            id="h1",
            source="goldfish",
            conversation_id="c1",
            role="user",
            text="We should build trust with customers this quarter",
        )
        casual2 = PlaneMessage(
            id="h2",
            source="goldfish",
            conversation_id="c1",
            role="user",
            text="I was thinking about how calculators work internally",
        )
        self.assertEqual(router.classify(casual1).action, "IGNORE")
        self.assertEqual(router.classify(casual2).action, "IGNORE")

        build_codex = PlaneMessage(
            id="h3",
            source="goldfish",
            conversation_id="c1",
            role="user",
            text="Use codex to build a Python CLI calculator with unit tests",
        )
        d3 = router.classify(build_codex)
        self.assertEqual(d3.action, "PROPOSE_BUILD")
        self.assertEqual(d3.target_agent, "codex-1")

        status_msg = PlaneMessage(
            id="h4",
            source="goldfish",
            conversation_id="c1",
            role="user",
            text="@rig status",
        )
        self.assertEqual(router.classify(status_msg).action, "CHECK_STATUS")


class TestPlaneListeners(unittest.TestCase):
    def test_goldfish_listener_baselines_old_messages_and_catches_new_user_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chats_dir = Path(tmp) / "chats"
            chats_dir.mkdir()
            chat_file = chats_dir / "chat-1.json"
            chat_data = {
                "id": "chat-1",
                "createdAt": 1700000000000,
                "updatedAt": 1700000001000,
                "messages": [
                    {"id": "old-u1", "role": "user", "content": "Build an old tool"},
                    {"id": "old-a1", "role": "assistant", "content": "Here is how."},
                ],
            }
            chat_file.write_text(json.dumps(chat_data), encoding="utf-8")

            store = TaskStore(db_path=Path(tmp) / "tasks.sqlite")
            listener = GoldfishChatListener(chats_dir=chats_dir, store=store)
            baselined = listener.baseline()
            self.assertEqual(baselined, 2)
            self.assertEqual(listener.poll(), [])

            # Append assistant reply, report marker, and a new user turn
            chat_data["messages"].extend(
                [
                    {"id": "new-a2", "role": "assistant", "content": "Build a calculator"},
                    {
                        "id": "new-u-report",
                        "role": "user",
                        "content": "[SHARED PLANE REPORT — T-1 — DONE] Built calculator",
                    },
                    {
                        "id": "new-u2",
                        "role": "user",
                        "content": "I want to build a CLI calculator in Python",
                    },
                ]
            )
            before_write_bytes = json.dumps(chat_data).encode("utf-8")
            chat_file.write_bytes(before_write_bytes)
            mtime_before = chat_file.stat().st_mtime_ns

            msgs = listener.poll()
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0].id, "new-u2")
            self.assertEqual(msgs[0].source, "goldfish")
            self.assertEqual(msgs[0].conversation_id, "chat-1")
            self.assertEqual(msgs[0].role, "user")
            self.assertIn("CLI calculator", msgs[0].text)
            self.assertTrue(len(msgs[0].recent_context) >= 1)
            self.assertLessEqual(len(msgs[0].recent_context), 3)

            # Deduplicated on next poll, and Goldfish file untouched
            self.assertEqual(listener.poll(), [])
            self.assertEqual(chat_file.read_bytes(), before_write_bytes)
            self.assertEqual(chat_file.stat().st_mtime_ns, mtime_before)

    def test_goldfish_listener_skips_inbox_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chats_dir = Path(tmp) / "chats"
            chats_dir.mkdir()
            (chats_dir / "inbox.json").write_text(
                json.dumps(
                    {
                        "id": "inbox",
                        "messages": [
                            {"id": "inbox-1", "role": "user", "content": "Build a secret"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = TaskStore(db_path=Path(tmp) / "tasks.sqlite")
            listener = GoldfishChatListener(chats_dir=chats_dir, store=store)
            self.assertEqual(listener.baseline(), 0)
            self.assertEqual(listener.poll(), [])

    def test_goldfish_listener_survives_partial_json_write_and_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chats_dir = Path(tmp) / "chats"
            chats_dir.mkdir()
            chat_file = chats_dir / "chat-partial.json"
            chat_file.write_text("{invalid json truncated", encoding="utf-8")

            store = TaskStore(db_path=Path(tmp) / "tasks.sqlite")
            listener = GoldfishChatListener(chats_dir=chats_dir, store=store)
            self.assertEqual(listener.poll(), [])

            # Simulate PermissionError while Goldfish holds the file lock
            chat_file.write_text(
                json.dumps(
                    {
                        "id": "chat-partial",
                        "messages": [{"id": "m-ok", "role": "user", "content": "Build a parser"}],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(Path, "read_text", side_effect=PermissionError("locked")):
                self.assertEqual(listener.poll(), [])

            # Next poll after lock clears recovers the message
            msgs = listener.poll()
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0].id, "m-ok")

    def test_jsonl_bus_listener_ingests_external_brain_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus_path = Path(tmp) / "bus.jsonl"
            bus_path.write_text(
                json.dumps({"id": "bus-old", "role": "user", "source": "claude_desktop", "text": "Old"})
                + "\n",
                encoding="utf-8",
            )
            store = TaskStore(db_path=Path(tmp) / "tasks.sqlite")
            listener = JsonlBusListener(bus_path=bus_path, store=store)
            self.assertEqual(listener.baseline(), 1)

            with open(bus_path, "a", encoding="utf-8") as f:
                f.write("{corrupt-line\n")
                f.write(
                    json.dumps(
                        {
                            "id": "bus-asst",
                            "role": "assistant",
                            "source": "claude_desktop",
                            "text": "I can build a timer",
                        }
                    )
                    + "\n"
                )
                f.write(
                    json.dumps(
                        {
                            "id": "bus-1",
                            "role": "user",
                            "source": "claude_desktop",
                            "conversation_id": "conv-cd",
                            "text": "Build a timer",
                        }
                    )
                    + "\n"
                )

            msgs = listener.poll()
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0].id, "bus-1")
            self.assertEqual(msgs[0].source, "claude_desktop")
            self.assertEqual(msgs[0].conversation_id, "conv-cd")
            self.assertEqual(msgs[0].text, "Build a timer")
            self.assertEqual(listener.poll(), [])


class TestPlaneSpeakerAndReadback(unittest.TestCase):
    def test_report_file_speaker_writes_latest_markdown_and_json_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            speaker = ReportFileSpeaker(reports_dir=reports_dir)
            report = BuildReport(
                task_id="T-99",
                status="DONE",
                repo=tmp,
                goal="Build calculator",
                candidate_commit="aaa1111",
                merge_commit="bbb2222",
                files_changed=["calc.py"],
                test_summary="PASS",
                review_summary="Approved",
            )
            res = speaker.speak(report)
            latest_md_path = reports_dir / "latest_report.md"
            latest_json_path = reports_dir / "latest_report.json"
            per_task_path = reports_dir / "T-99.md"

            self.assertTrue(latest_md_path.exists())
            self.assertTrue(latest_json_path.exists())
            self.assertTrue(per_task_path.exists())
            self.assertEqual(list(reports_dir.glob("*.tmp")), [])
            self.assertEqual(res["latest_markdown"], str(latest_md_path))

            latest_md = latest_md_path.read_text(encoding="utf-8")
            self.assertIn("[SHARED PLANE REPORT", latest_md)
            self.assertIn("T-99", latest_md)
            self.assertIn("calc.py", latest_md)

            parsed = json.loads(latest_json_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["task_id"], "T-99")
            self.assertEqual(parsed["merge_commit"], "bbb2222")

    def test_toast_and_composite_speaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_file = Path(tmp) / "events.jsonl"
            reports_dir = Path(tmp) / "reports"
            emitter = EventEmitter(log_path=events_file, enable_notifications=False)
            speaker = CompositeSpeaker(
                [
                    ReportFileSpeaker(reports_dir=reports_dir),
                    ToastNotificationSpeaker(emitter=emitter),
                ]
            )
            report = BuildReport(
                task_id="T-105",
                status="DONE",
                repo=tmp,
                goal="Build toast test",
                test_summary="1/1 passed",
            )
            res = speaker.speak(report)
            self.assertEqual(len(res["results"]), 2)
            self.assertTrue((reports_dir / "latest_report.md").exists())
            log_lines = events_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(log_lines), 1)
            event_obj = json.loads(log_lines[0])
            self.assertEqual(event_obj["event_type"], "SHARED_PLANE_REPORT")
            self.assertEqual(event_obj["task_id"], "T-105")

    def test_mcp_get_latest_build_report_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(db_path=Path(tmp) / "tasks.sqlite")
            emitter = EventEmitter(log_path=Path(tmp) / "events.jsonl", enable_notifications=False)
            service = TaskService(store=store, event_emitter=emitter, dry_run=True)
            reports_dir = Path(tmp) / "reports"
            mcp = McpServer(service=service, reports_dir=reports_dir)

            tools_resp = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
            self.assertIn("get_latest_build_report", tool_names)

            empty_resp = mcp.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "get_latest_build_report", "arguments": {}},
                }
            )
            empty_payload = json.loads(empty_resp["result"]["content"][0]["text"])
            self.assertFalse(empty_payload["available"])

            speaker = ReportFileSpeaker(reports_dir=reports_dir)
            speaker.speak(
                BuildReport(
                    task_id="T-200",
                    status="DONE",
                    repo=tmp,
                    goal="Expose MCP report",
                    test_summary="3/3 passed",
                )
            )
            full_resp = mcp.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "get_latest_build_report", "arguments": {}},
                }
            )
            full_payload = json.loads(full_resp["result"]["content"][0]["text"])
            self.assertTrue(full_payload["available"])
            self.assertEqual(full_payload["report"]["task_id"], "T-200")
            self.assertIn("[SHARED PLANE REPORT", full_payload["markdown"])

    def test_http_get_reports_latest_and_inbox_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            repo_dir.mkdir()
            store = TaskStore(db_path=Path(tmp) / "tasks.sqlite")
            emitter = EventEmitter(log_path=Path(tmp) / "events.jsonl", enable_notifications=False)
            service = TaskService(
                store=store,
                event_emitter=emitter,
                extra_allowed_repos=[repo_dir],
                dry_run=True,
            )
            reports_dir = Path(tmp) / "reports"
            server = TaskServer(
                service=service,
                host="127.0.0.1",
                port=0,
                token_path=Path(tmp) / "auth.token",
                reports_dir=reports_dir,
            )
            server.start()
            try:
                base_url = f"http://127.0.0.1:{server.port}"
                # Requires Bearer auth
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(f"{base_url}/reports/latest")
                self.assertEqual(ctx.exception.code, 401)

                # Before any report
                req = urllib.request.Request(f"{base_url}/reports/latest")
                req.add_header("Authorization", f"Bearer {server.auth_token}")
                with urllib.request.urlopen(req) as resp:
                    empty_data = json.loads(resp.read().decode("utf-8"))
                self.assertFalse(empty_data["available"])

                # After ReportFileSpeaker writes report
                ReportFileSpeaker(reports_dir=reports_dir).speak(
                    BuildReport(
                        task_id="T-300",
                        status="DONE",
                        repo=str(repo_dir),
                        goal="HTTP report check",
                    )
                )
                req2 = urllib.request.Request(f"{base_url}/reports/latest")
                req2.add_header("Authorization", f"Bearer {server.auth_token}")
                with urllib.request.urlopen(req2) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(data["available"])
                self.assertEqual(data["report"]["task_id"], "T-300")
                self.assertIn("[SHARED PLANE REPORT", data["markdown"])

                # /inbox HTML contains Latest Build Report section
                with urllib.request.urlopen(f"{base_url}/inbox") as resp:
                    html = resp.read().decode("utf-8")
                self.assertIn("latest-report-box", html)
                self.assertIn("/reports/latest", html)
            finally:
                server.stop()

    def test_http_approve_proposal_preserves_codex_assigned_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            repo_dir.mkdir()
            store = TaskStore(db_path=Path(tmp) / "tasks.sqlite")
            emitter = EventEmitter(log_path=Path(tmp) / "events.jsonl", enable_notifications=False)
            service = TaskService(
                store=store,
                event_emitter=emitter,
                extra_allowed_repos=[repo_dir],
                dry_run=True,
            )
            server = TaskServer(
                service=service,
                host="127.0.0.1",
                port=0,
                token_path=Path(tmp) / "auth.token",
            )
            server.start()
            try:
                spec = TaskSpec(
                    repo=str(repo_dir),
                    goal="Codex task via inbox",
                    verification_commands=["python -m unittest"],
                    assigned_agents={
                        "planner": "antigravity-1",
                        "implementer": "codex-1",
                        "reviewer": "opencode-1",
                    },
                )
                prop = service.propose_task(spec=spec, source="shared_plane:goldfish")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server.port}/proposals/{prop['proposal_id']}/approve",
                    data=b"{}",
                    method="POST",
                )
                req.add_header("Authorization", f"Bearer {server.auth_token}")
                req.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(req) as resp:
                    approved = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(approved["status"], "DRY_RUN")
                self.assertEqual(approved["spec_id"], spec.compute_hash())
            finally:
                server.stop()


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "integration"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
    (repo_dir / "README.md").write_text("# Sandbox\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)


class FakeDaemonEngine:
    """Offline mock engine making a real commit in temp git repo."""

    def __init__(self) -> None:
        self.invoked_agents = []

    def execute_bridge_task(self, task_id: str, spec: TaskSpec, ask_user_fn, cancel_check_fn):
        repo_dir = Path(spec.repo)
        self.invoked_agents.append(dict(spec.assigned_agents))
        target_file = repo_dir / "calc.py"
        target_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        subprocess.run(["git", "add", "calc.py"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"Implement {task_id}"], cwd=repo_dir, check=True, capture_output=True)
        cand_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
        return {
            "success": True,
            "candidate_commit": cand_sha,
            "merge_commit": cand_sha,
            "review_findings": f"Approved by {spec.assigned_agents.get('reviewer')}",
            "test_evidence": "2/2 passed",
        }


class TestSharedPlaneDaemonE2E(unittest.TestCase):
    def test_fallback_yaml_parser_handles_block_lists(self) -> None:
        raw = "\n".join(
            [
                "router:",
                "  mode: heuristic",
                "  prefix_triggers:",
                "    - '@rig'",
                "    - 'build a'",
                "agents:",
                "  enabled:",
                "    - opencode-1",
                "    - codex-1",
                "    - antigravity-1",
            ]
        )
        parsed = _fallback_parse_yaml(raw)
        self.assertEqual(parsed["router"]["mode"], "heuristic")
        self.assertEqual(parsed["router"]["prefix_triggers"], ["@rig", "build a"])
        self.assertEqual(parsed["agents"]["enabled"], ["opencode-1", "codex-1", "antigravity-1"])

    def test_daemon_e2e_natural_goldfish_chat_to_proposal_and_speaker_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sandbox_repo = tmp_path / ".disposable-live-repo"
            _init_git_repo(sandbox_repo)
            chats_dir = tmp_path / "chats"
            chats_dir.mkdir()
            chat_file = chats_dir / "conv-1.json"
            chat_file.write_text(
                json.dumps(
                    {
                        "id": "conv-1",
                        "messages": [{"id": "old-1", "role": "user", "content": "hi"}],
                    }
                ),
                encoding="utf-8",
            )
            reports_dir = tmp_path / "reports"
            store = TaskStore(db_path=tmp_path / "tasks.sqlite")
            emitter = EventEmitter(log_path=tmp_path / "events.jsonl", enable_notifications=False)
            mock_engine = FakeDaemonEngine()
            service = TaskService(
                store=store,
                event_emitter=emitter,
                engine_factory=lambda: mock_engine,
                extra_allowed_repos=[sandbox_repo],
                dry_run=False,
            )
            cfg = PlaneConfig(
                router_mode="laya",
                dispatch_mode="propose_first",
                default_repo=str(sandbox_repo),
                sandbox_repos=[str(sandbox_repo)],
                enabled_agents=["opencode-1", "codex-1", "antigravity-1"],
                reports_dir=str(reports_dir),
            )
            fake_laya = FakeLayaBackend(
                {
                    "answers": {
                        "is_build_task": {"type": "noul", "noul": 0.93},
                        "action": {"type": "choice", "choice": "PROPOSE_BUILD"},
                        "target_agent": {"type": "choice", "choice": "codex-1"},
                    }
                }
            )
            router = LayaRouter(threshold=0.75, enabled_agents=cfg.enabled_agents, backend=fake_laya)
            listener = GoldfishChatListener(chats_dir=chats_dir, store=store)
            speaker = ReportFileSpeaker(reports_dir=reports_dir)
            daemon = SharedPlaneDaemon(
                config=cfg,
                service=service,
                listeners=[listener],
                router=router,
                speaker=speaker,
                dry_run=False,
            )
            daemon.baseline()

            # Append natural chat turn
            chat_file.write_text(
                json.dumps(
                    {
                        "id": "conv-1",
                        "messages": [
                            {"id": "old-1", "role": "user", "content": "hi"},
                            {
                                "id": "new-1",
                                "role": "user",
                                "content": "I want to build a calculator in python",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            actions = daemon.step_once()
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["status"], "PROPOSED")
            self.assertEqual(len(mock_engine.invoked_agents), 0)

            proposals = service.list_proposals(status="PENDING_APPROVAL")
            self.assertEqual(len(proposals), 1)
            prop = proposals[0]
            assigned = prop["spec"]["assigned_agents"]
            self.assertEqual(assigned["implementer"], "codex-1")
            self.assertNotEqual(assigned["reviewer"], "codex-1")

            # Approve proposal and wait for completion
            spec_dict = prop["spec"]
            spec = TaskSpec(
                repo=spec_dict["repo"],
                goal=spec_dict["goal"],
                verification_commands=spec_dict["verification_commands"],
                constraints=spec_dict.get("constraints", []),
                acceptance_criteria=spec_dict.get("acceptance_criteria", []),
                allowed_merge_target=spec_dict["allowed_merge_target"],
                assigned_agents=assigned,
            )
            secret = get_or_create_auth_token()
            tok = create_approval_token(
                spec_id=prop["spec_id"],
                repo=prop["repo"],
                conversation_id="inbox",
                message_id=prop["proposal_id"],
                secret_key=secret,
                nonce="n-prop-1",
                allowed_actions=["execute_local"],
            )
            sub_res = service.task_submit(
                spec=spec,
                idempotency_key=f"prop-{prop['proposal_id']}",
                approved=True,
                approved_hash=prop["spec_id"],
                approval_record={
                    "spec_id": prop["spec_id"],
                    "repo": prop["repo"],
                    "source": "local_web",
                    "conversation_id": "inbox",
                    "message_id": prop["proposal_id"],
                    "approval_token": tok,
                    "nonce": "n-prop-1",
                    "allowed_actions": ["execute_local"],
                },
            )
            service.wait_all(timeout_sec=5.0)
            task_id = sub_res["task_id"]
            self.assertEqual(service.task_status(task_id)["state"], "DONE")

            # Speaker emits report once
            report_actions = daemon.step_once()
            self.assertTrue(
                any(a["action"] == "REPORT_SPOKEN" and a["task_id"] == task_id for a in report_actions)
            )
            latest_md = (reports_dir / "latest_report.md").read_text(encoding="utf-8")
            self.assertIn("[SHARED PLANE REPORT", latest_md)
            self.assertIn(task_id, latest_md)
            self.assertIn("calc.py", latest_md)

            # Next step does not duplicate report
            self.assertEqual(daemon.step_once(), [])

    def test_daemon_auto_dispatch_sandbox_only_for_disposable_live_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sandbox_repo = tmp_path / ".disposable-live-repo"
            non_sandbox_repo = tmp_path / "real_prod_repo"
            _init_git_repo(sandbox_repo)
            _init_git_repo(non_sandbox_repo)

            # Case 1: .disposable-live-repo on integration auto-dispatches
            chats1 = tmp_path / "chats1"
            chats1.mkdir()
            store1 = TaskStore(db_path=tmp_path / "t1.sqlite")
            emitter1 = EventEmitter(log_path=tmp_path / "e1.jsonl", enable_notifications=False)
            eng1 = FakeDaemonEngine()
            svc1 = TaskService(
                store=store1,
                event_emitter=emitter1,
                engine_factory=lambda: eng1,
                extra_allowed_repos=[sandbox_repo],
                dry_run=False,
            )
            cfg1 = PlaneConfig(
                dispatch_mode="auto_dispatch_sandbox",
                default_repo=str(sandbox_repo),
                sandbox_repos=[str(sandbox_repo)],
                reports_dir=str(tmp_path / "r1"),
            )
            fake_laya = FakeLayaBackend(
                {
                    "answers": {
                        "is_build_task": {"noul": 0.91},
                        "action": {"choice": "PROPOSE_BUILD"},
                        "target_agent": {"choice": "opencode-1"},
                    }
                }
            )
            daemon1 = SharedPlaneDaemon(
                config=cfg1,
                service=svc1,
                listeners=[GoldfishChatListener(chats_dir=chats1, store=store1)],
                router=LayaRouter(threshold=0.75, backend=fake_laya),
                speaker=ReportFileSpeaker(reports_dir=tmp_path / "r1"),
                dry_run=False,
            )
            daemon1.baseline()
            (chats1 / "c1.json").write_text(
                json.dumps(
                    {
                        "id": "c1",
                        "messages": [{"id": "m-auto", "role": "user", "content": "Build a calculator"}],
                    }
                ),
                encoding="utf-8",
            )
            res1 = daemon1.step_once()
            self.assertEqual(res1[0]["status"], "AUTO_DISPATCHED")
            svc1.wait_all(timeout_sec=5.0)
            self.assertEqual(len(eng1.invoked_agents), 1)
            res1_done = daemon1.step_once()
            self.assertTrue(any(a["action"] == "REPORT_SPOKEN" for a in res1_done))

            # Case 2: Non-sandbox repo refuses auto-dispatch and falls back to PENDING_APPROVAL
            chats2 = tmp_path / "chats2"
            chats2.mkdir()
            store2 = TaskStore(db_path=tmp_path / "t2.sqlite")
            emitter2 = EventEmitter(log_path=tmp_path / "e2.jsonl", enable_notifications=False)
            eng2 = FakeDaemonEngine()
            svc2 = TaskService(
                store=store2,
                event_emitter=emitter2,
                engine_factory=lambda: eng2,
                extra_allowed_repos=[non_sandbox_repo],
                dry_run=False,
            )
            cfg2 = PlaneConfig(
                dispatch_mode="auto_dispatch_sandbox",
                default_repo=str(non_sandbox_repo),
                sandbox_repos=[str(non_sandbox_repo)],
                reports_dir=str(tmp_path / "r2"),
            )
            daemon2 = SharedPlaneDaemon(
                config=cfg2,
                service=svc2,
                listeners=[GoldfishChatListener(chats_dir=chats2, store=store2)],
                router=LayaRouter(threshold=0.75, backend=fake_laya),
                speaker=ReportFileSpeaker(reports_dir=tmp_path / "r2"),
                dry_run=False,
            )
            daemon2.baseline()
            (chats2 / "c2.json").write_text(
                json.dumps(
                    {
                        "id": "c2",
                        "messages": [{"id": "m-non-sb", "role": "user", "content": "Build a calculator"}],
                    }
                ),
                encoding="utf-8",
            )
            res2 = daemon2.step_once()
            self.assertEqual(res2[0]["status"], "PROPOSED")
            self.assertEqual(len(eng2.invoked_agents), 0)

    def test_daemon_rejects_disallowed_repo_path_and_executes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sandbox_repo = tmp_path / ".disposable-live-repo"
            _init_git_repo(sandbox_repo)
            chats_dir = tmp_path / "chats"
            chats_dir.mkdir()
            store = TaskStore(db_path=tmp_path / "tasks.sqlite")
            emitter = EventEmitter(log_path=tmp_path / "events.jsonl", enable_notifications=False)
            eng = FakeDaemonEngine()
            svc = TaskService(
                store=store,
                event_emitter=emitter,
                engine_factory=lambda: eng,
                extra_allowed_repos=[sandbox_repo],
                dry_run=False,
            )
            cfg = PlaneConfig(
                default_repo=str(sandbox_repo),
                sandbox_repos=[str(sandbox_repo)],
                reports_dir=str(tmp_path / "reports"),
            )
            fake_laya = FakeLayaBackend(
                {
                    "answers": {
                        "is_build_task": {"noul": 0.95},
                        "action": {"choice": "PROPOSE_BUILD"},
                        "target_agent": {"choice": "opencode-1"},
                    }
                }
            )
            daemon = SharedPlaneDaemon(
                config=cfg,
                service=svc,
                listeners=[GoldfishChatListener(chats_dir=chats_dir, store=store)],
                router=LayaRouter(threshold=0.75, backend=fake_laya),
                speaker=ReportFileSpeaker(reports_dir=tmp_path / "reports"),
                dry_run=False,
            )
            daemon.baseline()
            (chats_dir / "c-bad.json").write_text(
                json.dumps(
                    {
                        "id": "c-bad",
                        "messages": [
                            {
                                "id": "m-bad-1",
                                "role": "user",
                                "content": r"Build a tool in C:\Windows\System32",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            actions = daemon.step_once()
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["status"], "REJECTED_POLICY_VIOLATION")
            self.assertEqual(svc.list_proposals(), [])
            self.assertEqual(len(eng.invoked_agents), 0)

    def test_doctor_runs_cleanly_without_mutating_goldfish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            gdir = tmp_path / "goldfish_chats"
            gdir.mkdir()
            sample = gdir / "chat1.json"
            sample.write_text('{"id":"chat1","messages":[]}', encoding="utf-8")
            mtime_before = sample.stat().st_mtime_ns
            cfg = PlaneConfig(goldfish_chats_dir=str(gdir))
            with patch("plane.daemon._check_wsl_openrig", return_value=("WARN", "mocked offline")):
                checks = run_doctor(cfg, store=TaskStore(db_path=tmp_path / "doc.sqlite"))
            self.assertTrue(len(checks) >= 6)
            self.assertTrue(all("name" in c and "status" in c and "detail" in c for c in checks))
            self.assertEqual(sample.stat().st_mtime_ns, mtime_before)

    def test_doctor_passes_sandbox_repo_with_linked_integration_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_dir = tmp_path / ".disposable-live-repo"
            _init_git_repo(repo_dir)
            # Switch root repo to main and put integration in linked .worktrees/_integration
            subprocess.run(["git", "checkout", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
            int_wt = repo_dir / ".worktrees" / "_integration"
            subprocess.run(
                ["git", "worktree", "add", str(int_wt), "integration"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
            )
            cfg = PlaneConfig(default_repo=str(repo_dir))
            with patch("plane.daemon._check_wsl_openrig", return_value=("PASS", "0.7.19")):
                checks = run_doctor(cfg, store=TaskStore(db_path=tmp_path / "doc.sqlite"))
            sb_check = next(c for c in checks if c["name"] == "sandbox_repo")
            self.assertEqual(sb_check["status"], "PASS")
            self.assertIn("integration", sb_check["detail"])


class TestLiveSessionRegressions(unittest.TestCase):
    def test_laya_router_does_not_pass_recent_context_to_backend(self) -> None:
        fake = FakeLayaBackend(
            {
                "answers": {
                    "is_build_task": {"type": "noul", "noul": 0.8212},
                    "action": {
                        "type": "choice",
                        "choice": "PROPOSE_BUILD",
                        "probabilities": {
                            "IGNORE": 0.114,
                            "PROPOSE_BUILD": 0.7859,
                            "ANSWER_QUESTION": 0.0558,
                            "CHECK_STATUS": 0.0443,
                        },
                    },
                    "target_agent": {"type": "choice", "choice": "codex-1"},
                }
            }
        )
        router = LayaRouter(
            threshold=0.75,
            enabled_agents=["opencode-1", "codex-1", "antigravity-1"],
            backend=fake,
        )
        msg = PlaneMessage(
            id="5a07ba58-29e7-4d56-b0f6-379a86df0138",
            source="goldfish",
            conversation_id="ab7b206a",
            role="user",
            text=(
                "Build a Pomodoro web app inspired by Pomofocus in the disposable sandbox repo "
                "using codex-1. Queue it for my approval before editing any files. No merge or push."
            ),
            recent_context=[
                "user: [SHARED PLANE] [{'action': 'CHECK_STATUS'}]",
                "assistant: Shared Plane shows CHECK_STATUS with 0 tasks",
            ],
        )
        decision = router.classify(msg)
        self.assertEqual(decision.action, "PROPOSE_BUILD")
        self.assertEqual(decision.target_agent, "codex-1")
        state_sent = fake.last_call[0]
        self.assertEqual(state_sent, {"message": msg.text})
        self.assertNotIn("recent_context", state_sent)

    def test_laya_router_action_precedence_and_confidence_gate(self) -> None:
        # 1. Poisoned 5a07ba58 shape: strong is_build_task (0.8105) + weak CHECK_STATUS (0.3978)
        router_poison = LayaRouter(
            threshold=0.75,
            enabled_agents=["opencode-1", "codex-1", "antigravity-1"],
            backend=FakeLayaBackend(
                {
                    "answers": {
                        "is_build_task": {"type": "noul", "noul": 0.8105},
                        "action": {
                            "type": "choice",
                            "choice": "CHECK_STATUS",
                            "probabilities": {
                                "IGNORE": 0.0987,
                                "PROPOSE_BUILD": 0.3041,
                                "ANSWER_QUESTION": 0.1993,
                                "CHECK_STATUS": 0.3978,
                            },
                            "answer_confidence": 0.3978,
                        },
                        "target_agent": {"type": "choice", "choice": "codex-1"},
                    }
                }
            ),
        )
        m_build = PlaneMessage(
            id="5a07ba58-29e7-4d56-b0f6-379a86df0138",
            source="goldfish",
            conversation_id="ab7b206a",
            role="user",
            text="Build a Pomodoro web app inspired by Pomofocus using codex-1.",
        )
        dec_build = router_poison.classify(m_build)
        self.assertEqual(dec_build.action, "PROPOSE_BUILD")
        self.assertEqual(dec_build.target_agent, "codex-1")
        self.assertGreaterEqual(dec_build.confidence, 0.81)

        # 2. Weak CHECK_STATUS with low is_build_task -> MUST return IGNORE (not CHECK_STATUS)
        router_weak_status = LayaRouter(
            threshold=0.75,
            backend=FakeLayaBackend(
                {
                    "answers": {
                        "is_build_task": {"type": "noul", "noul": 0.1933},
                        "action": {
                            "type": "choice",
                            "choice": "CHECK_STATUS",
                            "probabilities": {
                                "IGNORE": 0.284,
                                "PROPOSE_BUILD": 0.1649,
                                "ANSWER_QUESTION": 0.2393,
                                "CHECK_STATUS": 0.3118,
                            },
                            "answer_confidence": 0.3118,
                        },
                        "target_agent": {"type": "choice", "choice": "codex-1"},
                    }
                }
            ),
        )
        self.assertEqual(router_weak_status.classify(m_build).action, "IGNORE")

        # 3. High-confidence CHECK_STATUS (0.85 >= 0.75) with low is_build_task -> CHECK_STATUS
        router_strong_status = LayaRouter(
            threshold=0.75,
            backend=FakeLayaBackend(
                {
                    "answers": {
                        "is_build_task": {"type": "noul", "noul": 0.05},
                        "action": {
                            "type": "choice",
                            "choice": "CHECK_STATUS",
                            "probabilities": {
                                "IGNORE": 0.08,
                                "PROPOSE_BUILD": 0.04,
                                "ANSWER_QUESTION": 0.03,
                                "CHECK_STATUS": 0.85,
                            },
                            "answer_confidence": 0.85,
                        },
                        "target_agent": {"type": "choice", "choice": "opencode-1"},
                    }
                }
            ),
        )
        dec_status = router_strong_status.classify(
            PlaneMessage(
                id="status-1",
                source="goldfish",
                conversation_id="ab7b206a",
                role="user",
                text="Check status of my tasks",
            )
        )
        self.assertEqual(dec_status.action, "CHECK_STATUS")
        self.assertAlmostEqual(dec_status.confidence, 0.85)

        # 4. Clean Laya shapes for fb254626 (IGNORE) and e7399aa7 (PROPOSE_BUILD)
        router_fb25 = LayaRouter(
            threshold=0.75,
            backend=FakeLayaBackend(
                {
                    "answers": {
                        "is_build_task": {"type": "noul", "noul": 0.0005},
                        "action": {
                            "type": "choice",
                            "choice": "IGNORE",
                            "probabilities": {
                                "IGNORE": 0.5754,
                                "PROPOSE_BUILD": 0.0423,
                                "ANSWER_QUESTION": 0.2606,
                                "CHECK_STATUS": 0.1217,
                            },
                        },
                        "target_agent": {"type": "choice", "choice": "opencode-1"},
                    }
                }
            ),
        )
        self.assertEqual(
            router_fb25.classify(
                PlaneMessage(
                    id="fb254626-de37-43f2-a9cb-afb966e2525b",
                    source="goldfish",
                    conversation_id="ab7b206a",
                    role="user",
                    text="Hey goldfish did you see teh pomodaro clock which I opened?",
                )
            ).action,
            "IGNORE",
        )

        router_e739 = LayaRouter(
            threshold=0.75,
            enabled_agents=["opencode-1", "codex-1", "antigravity-1"],
            backend=FakeLayaBackend(
                {
                    "answers": {
                        "is_build_task": {"type": "noul", "noul": 0.8874},
                        "action": {
                            "type": "choice",
                            "choice": "PROPOSE_BUILD",
                            "probabilities": {
                                "IGNORE": 0.0928,
                                "PROPOSE_BUILD": 0.6516,
                                "ANSWER_QUESTION": 0.1732,
                                "CHECK_STATUS": 0.0824,
                            },
                        },
                        "target_agent": {"type": "choice", "choice": "codex-1"},
                    }
                }
            ),
        )
        dec_e739 = router_e739.classify(
            PlaneMessage(
                id="e7399aa7-ec46-441f-943f-37436aaa8b36",
                source="goldfish",
                conversation_id="ab7b206a",
                role="user",
                text="Great , So I want to code and build it using codex and exactly same you tell it to the rig",
            )
        )
        self.assertEqual(dec_e739.action, "PROPOSE_BUILD")
        self.assertEqual(dec_e739.target_agent, "codex-1")

    def test_shared_plane_log_pastes_filtered_in_listener_and_router(self) -> None:
        log_paste_1 = (
            "return Agent(repo, **kwargs)\n"
            "[SHARED PLANE] [{'action': 'CHECK_STATUS', 'message_id': 'a192e9f0-c48d-46fc-9ddc-274aa0a30199'}]"
        )
        log_paste_2 = (
            "nope nothing yet ,\nhere are the logs \n\n"
            "[SHARED PLANE] [{'action': 'CHECK_STATUS', 'message_id': '5a07ba58-29e7-4d56-b0f6-379a86df0138'}]"
        )
        with tempfile.TemporaryDirectory() as tmp:
            chats_dir = Path(tmp) / "chats"
            chats_dir.mkdir()
            store = TaskStore(db_path=Path(tmp) / "tasks.sqlite")
            listener = GoldfishChatListener(chats_dir=chats_dir, store=store)
            listener.baseline()

            (chats_dir / "ab7b206a.json").write_text(
                json.dumps(
                    {
                        "id": "ab7b206a",
                        "messages": [
                            {"id": "fb12ced1-4e2a-437a-a21d-e2499063dafc", "role": "user", "content": log_paste_1},
                            {"id": "asst-pomofocus", "role": "assistant", "content": "Yep, you opened Pomofocus in Edge at 25:00."},
                            {"id": "ace9fcf2-f6dc-4167-8452-20d3e078325c", "role": "user", "content": log_paste_2},
                            {
                                "id": "5a07ba58-29e7-4d56-b0f6-379a86df0138",
                                "role": "user",
                                "content": "Build a Pomodoro web app inspired by Pomofocus using codex-1.",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            emitted = listener.poll()
            self.assertEqual([m.id for m in emitted], ["5a07ba58-29e7-4d56-b0f6-379a86df0138"])
            self.assertTrue(all("[SHARED PLANE]" not in ctx for ctx in emitted[0].recent_context))
            self.assertTrue(any("Pomofocus" in ctx for ctx in emitted[0].recent_context))

        fake = FakeLayaBackend(
            {
                "answers": {
                    "is_build_task": {"noul": 0.99},
                    "action": {"choice": "PROPOSE_BUILD"},
                    "target_agent": {"choice": "opencode-1"},
                }
            }
        )
        laya_router = LayaRouter(threshold=0.75, backend=fake)
        heur_router = HeuristicRouter()
        for mid, txt in (
            ("fb12ced1-4e2a-437a-a21d-e2499063dafc", log_paste_1),
            ("ace9fcf2-f6dc-4167-8452-20d3e078325c", log_paste_2),
        ):
            msg = PlaneMessage(id=mid, source="goldfish", conversation_id="ab7b206a", role="user", text=txt)
            self.assertEqual(laya_router.classify(msg).action, "IGNORE")
            self.assertEqual(heur_router.classify(msg).action, "IGNORE")
        self.assertIsNone(fake.last_call)

    def test_spec_builder_preserves_recent_context_for_agent_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox_repo = Path(tmp) / ".disposable-live-repo"
            _init_git_repo(sandbox_repo)
            cfg = PlaneConfig(
                default_repo=str(sandbox_repo),
                sandbox_repos=[str(sandbox_repo)],
                enabled_agents=["opencode-1", "codex-1", "antigravity-1"],
            )
            builder = SpecBuilder(config=cfg, extra_allowed_repos=[sandbox_repo])
            msg = PlaneMessage(
                id="e7399aa7-ec46-441f-943f-37436aaa8b36",
                source="goldfish",
                conversation_id="ab7b206a",
                role="user",
                text="Great , So I want to code and build it using codex and exactly same you tell it to the rig",
                recent_context=[
                    r"user: I checked C:\Windows\System32 earlier",
                    "assistant: Yep, you opened Pomofocus in Edge. It was at 25:00 with START still showing.",
                ],
            )
            decision = RoutingDecision(
                action="PROPOSE_BUILD",
                confidence=0.8874,
                target_agent="codex-1",
                reason="Laya build intent",
                router_name="laya",
            )
            spec = builder.build(msg, decision)
            self.assertTrue(spec.goal.startswith("Great , So I want to code and build it using codex"))
            self.assertIn("Pomofocus", spec.goal)
            self.assertEqual(spec.assigned_agents["implementer"], "codex-1")
            self.assertEqual(Path(spec.repo).resolve(), sandbox_repo.resolve())

    def test_spec_builder_defaults_verification_commands_to_empty_and_respects_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sandbox_repo = tmp_path / ".disposable-live-repo"
            _init_git_repo(sandbox_repo)
            cfg_default = PlaneConfig(
                default_repo=str(sandbox_repo),
                sandbox_repos=[str(sandbox_repo)],
                enabled_agents=["opencode-1", "codex-1", "antigravity-1"],
            )
            self.assertEqual(cfg_default.verification_commands, [])
            builder_default = SpecBuilder(config=cfg_default, extra_allowed_repos=[sandbox_repo])
            msg = PlaneMessage(
                id="m-pomodoro",
                source="goldfish",
                conversation_id="conv-pomo",
                role="user",
                text="Build a Pomodoro web app using codex",
            )
            decision = RoutingDecision(
                action="PROPOSE_BUILD",
                confidence=0.95,
                target_agent="codex-1",
                reason="Build request",
                router_name="laya",
            )
            spec_default = builder_default.build(msg, decision)
            self.assertEqual(spec_default.verification_commands, [])

            yaml_path = tmp_path / "plane.yaml"
            yaml_path.write_text(
                f"default_repo: {sandbox_repo}\nverification_commands: [\"pytest -q\"]\n",
                encoding="utf-8",
            )
            cfg_custom = PlaneConfig.load(yaml_path)
            self.assertEqual(cfg_custom.verification_commands, ["pytest -q"])
            spec_custom = SpecBuilder(config=cfg_custom, extra_allowed_repos=[sandbox_repo]).build(
                msg, decision
            )
            self.assertEqual(spec_custom.verification_commands, ["pytest -q"])


if __name__ == "__main__":
    unittest.main()
