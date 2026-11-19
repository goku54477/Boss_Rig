#!/usr/bin/env python3
"""Tests for Codex persistent seat runner and safety semantics."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.codex_seat_runner as codex_runner
from scripts.codex_seat_runner import build_codex_command, run_codex_task


class TestCodexSeatRunner(unittest.TestCase):
    def setUp(self) -> None:
        self.orig_codex_danger = os.environ.get("CODEX_DANGEROUS_SKIP_PERMISSIONS")
        self.orig_agy_danger = os.environ.get("AGY_DANGEROUS_SKIP_PERMISSIONS")
        if "CODEX_DANGEROUS_SKIP_PERMISSIONS" in os.environ:
            del os.environ["CODEX_DANGEROUS_SKIP_PERMISSIONS"]
        if "AGY_DANGEROUS_SKIP_PERMISSIONS" in os.environ:
            del os.environ["AGY_DANGEROUS_SKIP_PERMISSIONS"]

    def tearDown(self) -> None:
        if self.orig_codex_danger is not None:
            os.environ["CODEX_DANGEROUS_SKIP_PERMISSIONS"] = self.orig_codex_danger
        elif "CODEX_DANGEROUS_SKIP_PERMISSIONS" in os.environ:
            del os.environ["CODEX_DANGEROUS_SKIP_PERMISSIONS"]

        if self.orig_agy_danger is not None:
            os.environ["AGY_DANGEROUS_SKIP_PERMISSIONS"] = self.orig_agy_danger
        elif "AGY_DANGEROUS_SKIP_PERMISSIONS" in os.environ:
            del os.environ["AGY_DANGEROUS_SKIP_PERMISSIONS"]

    def test_default_command_is_safe_sandbox(self) -> None:
        cmd = build_codex_command("test prompt", "/tmp/wt")
        self.assertIn("codex", cmd[0])
        self.assertIn("exec", cmd)
        self.assertIn("--json", cmd)
        self.assertIn("-C", cmd)
        self.assertIn("/tmp/wt", cmd)
        self.assertIn("--approve-for-me", cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_dangerous_mode_opt_in_codex(self) -> None:
        os.environ["CODEX_DANGEROUS_SKIP_PERMISSIONS"] = "1"
        cmd = build_codex_command("test prompt", "/tmp/wt")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertNotIn("--approve-for-me", cmd)

    def test_dangerous_mode_opt_in_agy_flag_compatibility(self) -> None:
        os.environ["AGY_DANGEROUS_SKIP_PERMISSIONS"] = "1"
        cmd = build_codex_command("test prompt", "/tmp/wt")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertNotIn("--approve-for-me", cmd)

    def test_run_codex_task_times_out_despite_silent_stdout_and_drains_stderr(self) -> None:
        # Simulate codex emitting one stdout line, flooding stderr (>128KB), then hanging silent on stdout.
        child_script = (
            "import sys, time\n"
            "print('{\"type\":\"turn.started\"}', flush=True)\n"
            "for i in range(400):\n"
            "    sys.stderr.write(f'stderr-noise-{i} ' + ('x' * 500) + '\\n')\n"
            "sys.stderr.flush()\n"
            "time.sleep(8)\n"
        )
        events = []

        def capture_emit(event_name, task_id, role, status, attempt=1, exit_code=None, extra_data=None):
            events.append((event_name, task_id, role, status, exit_code, extra_data or {}))

        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / ".openrig-codex-seat.log"
            t0 = time.monotonic()
            with patch.object(
                codex_runner,
                "build_codex_command",
                return_value=[sys.executable, "-c", child_script],
            ), patch.object(codex_runner, "LOG_PATH", str(log_file)), patch.object(
                codex_runner, "emit_bridge_event", side_effect=capture_emit
            ):
                run_codex_task(
                    task_id="T-CODEX-TO",
                    role="implementer",
                    goal="Test timeout and stderr drain",
                    raw_envelope="{}",
                    attempt=1,
                    timeout_sec=1,
                    model="",
                )
            elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 4.0)
        terminal = [e for e in events if e[0] == "task_timeout"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0][3], "TIMEOUT")
        self.assertEqual(terminal[0][4], 124)


if __name__ == "__main__":
    unittest.main()
