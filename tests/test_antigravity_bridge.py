#!/usr/bin/env python3
"""Automated acceptance tests for Antigravity persistent bridge."""

import json
import os
import signal
import subprocess
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SCRIPT = REPO_ROOT / "scripts" / "worktree-gate.sh"
RUNNER_SCRIPT = REPO_ROOT / "scripts" / "antigravity-seat-runner.sh"
SEAT_WT = REPO_ROOT / ".worktrees" / "antigravity-1"


def run_gate(*args: str) -> subprocess.CompletedProcess[str]:
    cmd = ["bash", str(GATE_SCRIPT), *args]
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


class AntigravityBridgeProcess:
    """Manages an active instance of antigravity-seat-runner.sh."""

    def __init__(self, seat_id: str = "antigravity-1", cwd: Optional[Path] = None):
        self.seat_id = seat_id
        self.cwd = cwd or SEAT_WT
        env = os.environ.copy()
        env["OPENRIG_SEAT_ID"] = seat_id
        env["ANTIGRAVITY_TASK_TIMEOUT_SEC"] = "300"
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
        assert self.proc.stdout is not None
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.line_buffer = ""

    def send_line(self, raw: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(raw + "\n")
        self.proc.stdin.flush()

    def read_event(self, timeout_sec: float = 60.0) -> Optional[Dict[str, Any]]:
        assert self.proc.stdout is not None
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            while "\n" in self.line_buffer:
                line, self.line_buffer = self.line_buffer.split("\n", 1)
                line = line.strip()
                if line.startswith("BRIDGE_EVENT "):
                    try:
                        return json.loads(line[len("BRIDGE_EVENT ") :])
                    except json.JSONDecodeError:
                        continue

            try:
                chunk = self.proc.stdout.read()
            except (BlockingIOError, TypeError):
                chunk = None

            if chunk:
                self.line_buffer += chunk
                continue

            if self.proc.poll() is not None:
                break

            time.sleep(0.02)

        while "\n" in self.line_buffer:
            line, self.line_buffer = self.line_buffer.split("\n", 1)
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
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
        if self.proc.stdout:
            try:
                self.proc.stdout.close()
            except Exception:
                pass
        if self.proc.stderr:
            try:
                self.proc.stderr.close()
            except Exception:
                pass


class TestAntigravityBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        run_gate("init-seat", "antigravity-1")
        int_wt = REPO_ROOT / ".worktrees" / "_integration"
        if not int_wt.exists():
            subprocess.run(["git", "worktree", "add", str(int_wt), "integration"], check=True)

    def test_01_simple_prompt(self) -> None:
        """Requirement 1: Simple prompt executes and emits structured lifecycle events."""
        bridge = AntigravityBridgeProcess()
        try:
            task_id = "T-AGY-01"
            envelope = {
                "task_id": task_id,
                "role": "coordinator",
                "goal": "Reply with 'ANTIGRAVITY_SMOKE_OK'",
                "timeout_sec": 30,
            }
            bridge.send_line(json.dumps(envelope))
            start_evt = bridge.read_until_event("task_started", timeout_sec=10.0)
            self.assertIsNotNone(start_evt)
            self.assertEqual(start_evt.get("task_id"), task_id)
            self.assertEqual(start_evt.get("role"), "coordinator")

            result_evt = bridge.read_until_event("task_result", timeout_sec=30.0)
            self.assertIsNotNone(result_evt)
            self.assertEqual(result_evt.get("task_id"), task_id)
            self.assertEqual(result_evt.get("status"), "SUCCESS")
            self.assertTrue(bridge.is_alive())
        finally:
            bridge.close()

    def test_02_inspection_prompt(self) -> None:
        """Requirement 2: Inspection prompt in reviewer role verifies worktree."""
        bridge = AntigravityBridgeProcess()
        try:
            task_id = "T-AGY-02"
            envelope = {
                "task_id": task_id,
                "role": "reviewer",
                "goal": "Inspect the repository and state the seat directory.",
                "timeout_sec": 45,
            }
            bridge.send_line(json.dumps(envelope))
            start_evt = bridge.read_until_event("task_started", timeout_sec=10.0)
            self.assertIsNotNone(start_evt)
            self.assertEqual(start_evt.get("role"), "reviewer")

            result_evt = bridge.read_until_event("task_result", timeout_sec=45.0)
            self.assertIsNotNone(result_evt)
            self.assertEqual(result_evt.get("status"), "SUCCESS")
            self.assertTrue(bridge.is_alive())
        finally:
            bridge.close()

    def test_03_controlled_write_and_state_safety(self) -> None:
        """Requirement 3: Write file in worktree, commit, verify state safety through gate."""
        import time
        task_id = f"T-AGY-WRITE-{int(time.time())}"
        subprocess.run(["git", "clean", "-fd"], cwd=SEAT_WT, check=False)
        subprocess.run(["git", "checkout", "-B", "seat/antigravity-1", "integration"], cwd=SEAT_WT, check=False)
        assign_res = run_gate("assign", task_id, "antigravity-1")
        self.assertEqual(assign_res.returncode, 0, f"assign failed: {assign_res.stderr}")

        proof_name = f"agy_proof_{task_id}.txt"
        proof_file = SEAT_WT / proof_name
        if proof_file.exists():
            proof_file.unlink()

        bridge = AntigravityBridgeProcess()
        try:
            envelope = {
                "task_id": task_id,
                "role": "implementer",
                "goal": f"Write the text 'antigravity verified' into a file named {proof_name} in current directory.",
                "timeout_sec": 60,
            }
            bridge.send_line(json.dumps(envelope))
            start_evt = bridge.read_until_event("task_started", timeout_sec=10.0)
            self.assertIsNotNone(start_evt)

            result_evt = bridge.read_until_event("task_result", timeout_sec=60.0)
            self.assertIsNotNone(result_evt)
            self.assertEqual(result_evt.get("status"), "SUCCESS")

            # Verify file exists inside seat worktree
            self.assertTrue(proof_file.exists(), f"{proof_name} was not created")

            # Commit the change
            subprocess.run(["git", "add", proof_name], cwd=SEAT_WT, check=True)
            subprocess.run(["git", "commit", "-m", f"test: add {proof_name}"], cwd=SEAT_WT, check=True)

            # Invariant: State must be ACTIVE, NOT DONE (Agent success != Software DONE)
            status_res = run_gate("status", task_id)
            self.assertEqual(status_res.stdout.strip(), "ACTIVE")

            # Deterministic Gate review transitions to DONE
            review_res = run_gate("review", task_id, "antigravity-1", f"test -f {proof_name}")
            self.assertEqual(review_res.returncode, 0, f"review failed: {review_res.stderr}")

            status_done = run_gate("status", task_id)
            self.assertEqual(status_done.stdout.strip(), "DONE")

            # Release seat
            rel_res = run_gate("release", task_id, "antigravity-1")
            self.assertEqual(rel_res.returncode, 0)
        finally:
            bridge.close()

    def test_04_sequential_prompt_series(self) -> None:
        """Requirement 4: At least 5 prompts sequentially executed through the same bridge process."""
        bridge = AntigravityBridgeProcess()
        try:
            for i in range(1, 6):
                task_id = f"T-AGY-SEQ-0{i}"
                envelope = {
                    "task_id": task_id,
                    "role": "planner",
                    "goal": f"Respond with: Plan step {i} acknowledged.",
                    "timeout_sec": 30,
                }
                bridge.send_line(json.dumps(envelope))
                start_evt = bridge.read_until_event("task_started", timeout_sec=10.0)
                self.assertIsNotNone(start_evt)
                self.assertEqual(start_evt.get("task_id"), task_id)

                res_evt = bridge.read_until_event("task_result", timeout_sec=30.0)
                self.assertIsNotNone(res_evt, f"Task {task_id} did not complete")
                self.assertEqual(res_evt.get("task_id"), task_id)
                self.assertEqual(res_evt.get("status"), "SUCCESS")
                self.assertTrue(bridge.is_alive(), f"Bridge must survive turn {i}")
        finally:
            bridge.close()

    def test_05_timeout_enforcement(self) -> None:
        """Requirement 5: Watchdog enforces timeout, terminates child, and reports TIMEOUT."""
        bridge = AntigravityBridgeProcess()
        try:
            envelope = {
                "task_id": "T-AGY-TIMEOUT",
                "role": "general",
                "goal": "Sleep or generate extensive prose for 60 seconds",
                "timeout_sec": 1,
            }
            bridge.send_line(json.dumps(envelope))
            start_evt = bridge.read_until_event("task_started", timeout_sec=10.0)
            self.assertIsNotNone(start_evt)

            timeout_evt = bridge.read_until_event("task_timeout", timeout_sec=10.0)
            self.assertIsNotNone(timeout_evt, "Bridge must emit task_timeout event")
            self.assertEqual(timeout_evt.get("status"), "TIMEOUT")
            self.assertEqual(timeout_evt.get("data", {}).get("exit_code"), 124)
            self.assertTrue(bridge.is_alive(), "Bridge must remain alive after child timeout")
        finally:
            bridge.close()

    def test_06_child_crash_and_recovery(self) -> None:
        """Requirement 6: Bridge recovers and executes next task after child crash/failure."""
        bridge = AntigravityBridgeProcess()
        try:
            # First send a task to establish session
            t1 = {
                "task_id": "T-AGY-CRASH-PRE",
                "role": "debugger",
                "goal": "Reply with OK",
                "timeout_sec": 30,
            }
            bridge.send_line(json.dumps(t1))
            res1 = bridge.read_until_event("task_result", timeout_sec=30.0)
            self.assertIsNotNone(res1)

            # Now kill any running agy processes to simulate a crash
            subprocess.run(["pkill", "-9", "-f", "agy"], check=False)

            # Send second task to prove bridge recovers automatically
            t2 = {
                "task_id": "T-AGY-CRASH-POST",
                "role": "debugger",
                "goal": "Reply with 'RECOVERED_OK'",
                "timeout_sec": 30,
            }
            bridge.send_line(json.dumps(t2))
            start_evt = bridge.read_until_event("task_started", timeout_sec=10.0)
            self.assertIsNotNone(start_evt)

            res2 = bridge.read_until_event("task_result", timeout_sec=30.0)
            self.assertIsNotNone(res2)
            self.assertEqual(res2.get("status"), "SUCCESS")
            self.assertTrue(bridge.is_alive())
        finally:
            bridge.close()

    def test_07_clean_termination(self) -> None:
        """Requirement 7: Bridge exits cleanly with return code 0 on :quit."""
        bridge = AntigravityBridgeProcess()
        self.assertTrue(bridge.is_alive())
        bridge.send_line(":quit")
        bridge.proc.wait(timeout=5)
        self.assertEqual(bridge.proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
