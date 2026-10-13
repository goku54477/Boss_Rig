"""Task dispatcher interfacing with OpenRig Codex seat, OpenCode bridge, and Antigravity bridge."""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from orchestrator.models import BridgeEvent, TaskEnvelope

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKTREES_DIR = REPO_ROOT / ".worktrees"


class SeatBridgeProcess:
    """Manages an active bridge process for a seat."""

    def __init__(self, seat_id: str, script_name: str, cwd: Path):
        self.seat_id = seat_id
        self.cwd = cwd
        self.script_path = REPO_ROOT / "scripts" / script_name
        env = os.environ.copy()
        env["OPENRIG_SEAT_ID"] = seat_id
        self.proc = subprocess.Popen(
            ["bash", str(self.script_path)],
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

    def send_line(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
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


class TaskDispatcher:
    """Dispatches tasks to the appropriate engine seat."""

    def __init__(self) -> None:
        self.active_bridges: Dict[str, SeatBridgeProcess] = {}

    def get_or_start_bridge(self, engine: str) -> SeatBridgeProcess:
        if engine in self.active_bridges and self.active_bridges[engine].is_alive():
            return self.active_bridges[engine]

        seat_wt = WORKTREES_DIR / engine
        if not seat_wt.exists():
            seat_wt.mkdir(parents=True, exist_ok=True)

        if engine == "opencode-1":
            bridge = SeatBridgeProcess(engine, "opencode-seat-runner.sh", seat_wt)
        elif engine == "antigravity-1":
            bridge = SeatBridgeProcess(engine, "antigravity-seat-runner.sh", seat_wt)
        elif engine == "codex-1":
            # For testing and execution under OpenRig, we also provide a bridge adapter
            bridge = SeatBridgeProcess(engine, "opencode-seat-runner.sh", seat_wt)
        else:
            raise ValueError(f"Unknown engine: {engine}")

        self.active_bridges[engine] = bridge
        return bridge

    def dispatch_task(
        self,
        engine: str,
        envelope: TaskEnvelope,
        timeout_sec: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Dispatch task to engine and wait for completion event."""
        timeout = timeout_sec or envelope.timeout_sec
        bridge = self.get_or_start_bridge(engine)

        raw_payload = json.dumps(envelope.to_dict())
        bridge.send_line(raw_payload)

        # Wait for task_started
        start_evt = bridge.read_until_event("task_started", timeout_sec=10.0)

        # Wait for terminal event: task_result, task_timeout, or task_failed
        term_evt = bridge.read_until_event("task_result", timeout_sec=float(timeout))
        if not term_evt:
            return {
                "status": "TIMEOUT",
                "error": f"Task timed out after {timeout} seconds",
                "exit_code": 124,
            }

        return term_evt

    def close_all(self) -> None:
        for bridge in self.active_bridges.values():
            bridge.close()
        self.active_bridges.clear()
