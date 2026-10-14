#!/usr/bin/env python3
"""Tests for Codex persistent seat runner and safety semantics."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO_ROOT))

from scripts.codex_seat_runner import build_codex_command


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


if __name__ == "__main__":
    unittest.main()
