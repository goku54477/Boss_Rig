"""Offline unit and integration tests for Boss Rig Shared Plane v1."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from unittest.mock import patch

from orchestrator.store import TaskStore
from plane.adapters.listeners import GoldfishChatListener, JsonlBusListener
from plane.config import PlaneConfig
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


if __name__ == "__main__":
    unittest.main()
