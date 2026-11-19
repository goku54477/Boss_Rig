"""Offline unit and integration tests for Boss Rig Shared Plane v1."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from plane.config import PlaneConfig
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


if __name__ == "__main__":
    unittest.main()
