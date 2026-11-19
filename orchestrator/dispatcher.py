"""Task dispatcher interfacing with OpenRig Codex seat, OpenCode bridge, and Antigravity bridge."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional
from orchestrator.models import BridgeEvent, SeatMode, TaskEnvelope
from orchestrator.security import to_wsl_path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKTREES_DIR = REPO_ROOT / ".worktrees"


class SeatHoldError(RuntimeError):
    """Raised when dispatch is attempted on a seat currently in MANUAL_HOLD."""
    pass


class SeatBridgeProcess:
    """Manages an active bridge process for a seat using cross-platform threaded pipe reading."""

    def __init__(
        self,
        seat_id: str,
        script_name: str,
        cwd: Path,
        command: Optional[List[str]] = None,
    ):
        self.seat_id = seat_id
        self.cwd = cwd
        self.script_path = REPO_ROOT / "scripts" / script_name
        env = os.environ.copy()
        env["OPENRIG_SEAT_ID"] = seat_id
        cmd = list(command) if command is not None else ["bash", to_wsl_path(self.script_path)]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        assert self.proc.stdout is not None
        self._line_queue: queue.Queue[Optional[str]] = queue.Queue()
        self._stderr_tail: Deque[str] = deque(maxlen=50)
        self._eof = False
        self._stdout_thread = threading.Thread(
            target=self._read_stdout_loop, daemon=True, name=f"bridge-out-{seat_id}"
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr_loop, daemon=True, name=f"bridge-err-{seat_id}"
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout_loop(self) -> None:
        stdout = self.proc.stdout
        try:
            if stdout is not None:
                for raw_line in iter(stdout.readline, ""):
                    self._line_queue.put(raw_line.rstrip("\r\n"))
        except Exception:
            pass
        finally:
            self._line_queue.put(None)

    def _read_stderr_loop(self) -> None:
        stderr = self.proc.stderr
        try:
            if stderr is not None:
                for raw_line in iter(stderr.readline, ""):
                    stripped = raw_line.rstrip("\r\n")
                    if stripped:
                        self._stderr_tail.append(stripped)
        except Exception:
            pass

    @property
    def stderr_summary(self) -> str:
        return "\n".join(self._stderr_tail)

    def send_line(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def read_event(self, timeout_sec: float = 60.0) -> Optional[Dict[str, Any]]:
        if self._eof and self._line_queue.empty():
            return None
        deadline = time.time() + timeout_sec
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                line = self._line_queue.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                if self._eof or self.proc.poll() is not None and self._line_queue.empty():
                    return None
                continue
            if line is None:
                self._eof = True
                return None
            stripped = line.strip()
            if stripped.startswith("BRIDGE_EVENT "):
                try:
                    return json.loads(stripped[len("BRIDGE_EVENT ") :])
                except json.JSONDecodeError:
                    continue

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
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
                except Exception:
                    pass
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
        for th in (self._stdout_thread, self._stderr_thread):
            if th.is_alive():
                th.join(timeout=1.0)


class TaskDispatcher:
    """Dispatches tasks to the appropriate engine seat."""

    def __init__(self, store: Optional[Any] = None, worktrees_dir: Optional[Path] = None) -> None:
        self.active_bridges: Dict[str, SeatBridgeProcess] = {}
        self.store = store
        self.worktrees_dir = Path(worktrees_dir or WORKTREES_DIR).resolve()

    def get_or_start_bridge(self, engine: str) -> SeatBridgeProcess:
        if engine in self.active_bridges and self.active_bridges[engine].is_alive():
            return self.active_bridges[engine]

        seat_wt = self.worktrees_dir / engine
        if not seat_wt.exists() or not (seat_wt / ".git").exists():
            raise RuntimeError(
                f"Seat worktree '{seat_wt}' is missing or not a valid Git worktree (.git missing). "
                f"Run worktree-gate.sh init-seat {engine} before dispatching."
            )

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
