"""Task dispatcher interfacing with OpenRig Codex seat, OpenCode bridge, and Antigravity bridge."""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from orchestrator.models import BridgeEvent, SeatMode, TaskEnvelope

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKTREES_DIR = REPO_ROOT / ".worktrees"


class SeatHoldError(RuntimeError):
    """Raised when dispatch is attempted on a seat currently in MANUAL_HOLD."""
    pass



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
        self,
        event_name: str,
        timeout_sec: float = 60.0,
        on_activity: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        start_time = time.time()
        last_hb = start_time
        deadline = start_time + timeout_sec
        while time.time() < deadline:
            rem = deadline - time.time()
            if rem <= 0:
                break
            # Use small slice (max 1.0s) so timer heartbeats fire reliably
            evt = self.read_event(timeout_sec=min(rem, 1.0))
            now = time.time()
            if evt:
                if on_activity:
                    try:
                        on_activity(evt)
                    except Exception:
                        pass
                if evt.get("event") == event_name:
                    return evt
                if evt.get("event") in ("task_result", "task_failed", "task_timeout"):
                    return evt

            if now - last_hb >= 3.0:
                last_hb = now
                if on_activity:
                    try:
                        on_activity({
                            "event": "heartbeat",
                            "seat_id": self.seat_id,
                            "elapsed_sec": round(now - start_time, 1),
                        })
                    except Exception:
                        pass
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

    def __init__(self, store: Optional[Any] = None) -> None:
        self.active_bridges: Dict[str, SeatBridgeProcess] = {}
        self.store = store

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
            bridge = SeatBridgeProcess(engine, "codex-seat-runner.sh", seat_wt)
        else:
            raise ValueError(f"Unknown engine: {engine}")

        self.active_bridges[engine] = bridge
        return bridge

    def dispatch_task(
        self,
        engine: str,
        envelope: TaskEnvelope,
        timeout_sec: Optional[int] = None,
        on_activity: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Dispatch task to engine and wait for completion event."""
        if self.store is not None:
            mode_info = self.store.get_seat_mode(engine)
            if mode_info.get("mode") == SeatMode.MANUAL_HOLD:
                reason = mode_info.get("reason", "Operator hold active")
                raise SeatHoldError(
                    f"Seat '{engine}' is on MANUAL_HOLD: {reason}. Automated dispatch rejected."
                )

        timeout = timeout_sec or envelope.timeout_sec
        bridge = self.get_or_start_bridge(engine)

        raw_payload = json.dumps(envelope.to_dict())
        bridge.send_line(raw_payload)

        def _read_evt(event_name: str, t_sec: float) -> Optional[Dict[str, Any]]:
            try:
                return bridge.read_until_event(event_name, timeout_sec=t_sec, on_activity=on_activity)
            except TypeError:
                return bridge.read_until_event(event_name, timeout_sec=t_sec)

        # Wait for task_started
        start_evt = _read_evt("task_started", 10.0)

        # Wait for terminal event: task_result, task_timeout, or task_failed
        term_evt = _read_evt("task_result", float(timeout))
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
