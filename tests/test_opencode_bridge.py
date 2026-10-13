#!/usr/bin/env python3
"""Automated acceptance tests for OpenCode persistent bridge."""

import json
import os
import select
import signal
import subprocess
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SCRIPT = REPO_ROOT / "scripts" / "worktree-gate.sh"
RUNNER_SCRIPT = REPO_ROOT / "scripts" / "opencode-seat-runner.sh"
SEAT_WT = REPO_ROOT / ".worktrees" / "opencode-1"


def run_gate(*args: str) -> subprocess.CompletedProcess[str]:
    cmd = ["bash", str(GATE_SCRIPT), *args]
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


class OpenCodeBridgeProcess:
    """Manages an active instance of opencode-seat-runner.sh."""

    def __init__(self, seat_id: str = "opencode-1", cwd: Optional[Path] = None):
        self.seat_id = seat_id
        self.cwd = cwd or SEAT_WT
        env = os.environ.copy()
        env["OPENRIG_SEAT_ID"] = seat_id
        env["OPENCODE_TASK_TIMEOUT_SEC"] = "300"
        self.proc = subprocess.Popen(
            ["bash", str(RUNNER_SCRIPT)],
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

    def send_line(self, raw: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(raw + "\n")
        self.proc.stdin.flush()

    def read_event(self, timeout_sec: float = 60.0) -> Optional[Dict[str, Any]]:
        assert self.proc.stdout is not None
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            rem = max(0.01, deadline - time.time())
            r, _, _ = select.select([self.proc.stdout], [], [], min(rem, 0.2))
            if not r:
                if self.proc.poll() is not None:
                    break
                continue
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    break
                continue
            line = line.strip()
            if line.startswith("BRIDGE_EVENT "):
                try:
                    return json.loads(line[len("BRIDGE_EVENT ") :])
                except json.JSONDecodeError:
                    continue
        return None

    def read_until_event(
        self, event_name: str, timeout_sec: float = 60.0
    ) -> Optional[Dict[str, Any]]:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            rem = deadline - time.time()
            if rem <= 0:
                break
            evt = self.read_event(timeout_sec=rem)
            if evt:
                if evt.get("event") == event_name:
                    return evt
                if evt.get("event") in ("task_result", "task_failed", "task_timeout"):
                    return evt
        return None

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        if self.is_alive():
            try:
                self.send_line(":quit")
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()
                self.proc.wait(timeout=2)


class TestOpenCodeBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        run_gate("init-seat", "opencode-1")
        int_wt = REPO_ROOT / ".worktrees" / "_integration"
        if not int_wt.exists():
            subprocess.run(["git", "worktree", "add", str(int_wt), "integration"], check=True)

    def test_01_invalid_json_handling(self) -> None:
        """Requirement 7: Malformed task envelopes are rejected safely without crashing the bridge."""
        bridge = OpenCodeBridgeProcess()
        try:
            self.assertTrue(bridge.is_alive())
            bridge.send_line("THIS IS NOT VALID JSON")
            evt = bridge.read_until_event("task_failed", timeout_sec=5.0)
            self.assertIsNotNone(evt, "Bridge should emit task_failed on malformed input")
            self.assertEqual(evt.get("status"), "INVALID_JSON")
            self.assertTrue(bridge.is_alive(), "Bridge must survive malformed input")
        finally:
            bridge.close()

    def test_02_sequential_persistence_and_read_only(self) -> None:
        """Requirements 1 & 3: Bridge survives multiple sequential tasks through same process."""
        bridge = OpenCodeBridgeProcess()
        try:
            for i in range(1, 4):
                task_id = f"T-OC-SEQ-0{i}"
                envelope = {
                    "task_id": task_id,
                    "role": "reviewer",
                    "goal": f"Reply with: Review sequence {i} verified.",
                    "timeout_sec": 60,
                }
                bridge.send_line(json.dumps(envelope))
                start_evt = bridge.read_until_event("task_started", timeout_sec=5.0)
                self.assertIsNotNone(start_evt)
                self.assertEqual(start_evt.get("task_id"), task_id)
                self.assertEqual(start_evt.get("role"), "reviewer")

                res_evt = bridge.read_until_event("task_result", timeout_sec=60.0)
                self.assertIsNotNone(res_evt, f"Task {task_id} did not complete")
                self.assertEqual(res_evt.get("task_id"), task_id)
                self.assertEqual(res_evt.get("status"), "SUCCESS")
                self.assertTrue(bridge.is_alive(), "Bridge must remain alive between tasks")
        finally:
            bridge.close()

    def test_03_timeout_enforcement(self) -> None:
        """Requirement 5: Watchdog enforces timeout, terminates child, and reports TIMEOUT."""
        bridge = OpenCodeBridgeProcess()
        try:
            envelope = {
                "task_id": "T-OC-TIMEOUT",
                "role": "general",
                "goal": "Sleep for 30 seconds",
                "timeout_sec": 2,
            }
            bridge.send_line(json.dumps(envelope))
            start_evt = bridge.read_until_event("task_started", timeout_sec=5.0)
            self.assertIsNotNone(start_evt)

            timeout_evt = bridge.read_until_event("task_timeout", timeout_sec=10.0)
            self.assertIsNotNone(timeout_evt, "Bridge must emit task_timeout event")
            self.assertEqual(timeout_evt.get("status"), "TIMEOUT")
            self.assertEqual(timeout_evt.get("data", {}).get("exit_code"), 124)
            self.assertTrue(bridge.is_alive(), "Bridge must remain alive after timeout")
        finally:
            bridge.close()

    def test_04_real_file_modification_and_state_safety(self) -> None:
        """Requirements 2, 8, 9, 10: File write in worktree, commit recorded, not marked DONE automatically."""
        task_id = "T-OC-WRITE-01"
        # Assign seat to task branch via worktree gate
        assign_res = run_gate("assign", task_id, "opencode-1")
        self.assertEqual(assign_res.returncode, 0, f"assign failed: {assign_res.stderr}")

        bridge = OpenCodeBridgeProcess()
        try:
            envelope = {
                "task_id": task_id,
                "role": "implementer",
                "goal": "Write the text 'opencode proof' into a file named oc_proof.txt in the current directory and finish.",
                "timeout_sec": 60,
            }
            bridge.send_line(json.dumps(envelope))
            start_evt = bridge.read_until_event("task_started", timeout_sec=5.0)
            self.assertIsNotNone(start_evt)

            result_evt = bridge.read_until_event("task_result", timeout_sec=60.0)
            self.assertIsNotNone(result_evt)
            self.assertEqual(result_evt.get("status"), "SUCCESS")

            # Invariant: Verify file exists inside seat worktree
            proof_file = SEAT_WT / "oc_proof.txt"
            self.assertTrue(proof_file.exists(), "oc_proof.txt was not created")

            # Commit the change in the seat worktree
            subprocess.run(["git", "add", "oc_proof.txt"], cwd=SEAT_WT, check=True)
            subprocess.run(["git", "commit", "-m", "test: add oc_proof.txt"], cwd=SEAT_WT, check=True)

            # Invariant: State must be ACTIVE, NOT DONE (Agent success != Software DONE)
            status_res = run_gate("status", task_id)
            self.assertEqual(status_res.stdout.strip(), "ACTIVE")

            # Deterministic Gate review transitions to DONE
            review_res = run_gate("review", task_id, "opencode-1", "test -f oc_proof.txt")
            self.assertEqual(review_res.returncode, 0, f"review failed: {review_res.stderr}")

            status_done = run_gate("status", task_id)
            self.assertEqual(status_done.stdout.strip(), "DONE")

            # Release seat
            rel_res = run_gate("release", task_id, "opencode-1")
            self.assertEqual(rel_res.returncode, 0)
        finally:
            bridge.close()

    def test_05_clean_shutdown(self) -> None:
        """Bridge exits cleanly when receiving :quit or :exit."""
        bridge = OpenCodeBridgeProcess()
        self.assertTrue(bridge.is_alive())
        bridge.send_line(":quit")
        bridge.proc.wait(timeout=5)
        self.assertEqual(bridge.proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
