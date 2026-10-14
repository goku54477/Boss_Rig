#!/usr/bin/env python3
"""Antigravity persistent seat runner for OpenRig."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from typing import Any, Dict, Optional

SEAT_ID = os.environ.get("OPENRIG_SEAT_ID", "antigravity-1")
NOTIFY_SEAT = os.environ.get("OPENRIG_NOTIFY_SEAT", "")
DEFAULT_TIMEOUT = int(os.environ.get("ANTIGRAVITY_TASK_TIMEOUT_SEC", "1800"))
LOG_PATH = os.environ.get("ANTIGRAVITY_SEAT_LOG", os.path.join(os.getcwd(), ".openrig-antigravity-seat.log"))


def emit_bridge_event(
    event_name: str,
    task_id: str,
    role: str,
    status: str,
    attempt: int = 1,
    exit_code: Optional[int] = None,
    extra_data: Optional[Dict[str, Any]] = None,
) -> None:
    data = dict(extra_data or {})
    if exit_code is not None:
        data["exit_code"] = exit_code

    payload = {
        "version": 1,
        "source": "bridge",
        "runtime": "antigravity",
        "seat_id": SEAT_ID,
        "task_id": task_id,
        "role": role,
        "event": event_name,
        "status": status,
        "attempt": attempt,
        "data": data,
    }

    line = f"BRIDGE_EVENT {json.dumps(payload, separators=(',', ':'), ensure_ascii=False)}"
    print(line, flush=True)

    if NOTIFY_SEAT:
        try:
            subprocess.run(
                ["rig", "send", NOTIFY_SEAT, line],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass


def build_agy_command(extra_args: Optional[list[str]] = None) -> list[str]:
    """Construct agy command. Safe by default; dangerous permissions require explicit opt-in."""
    cmd = [
        "agy",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
    ]
    if os.environ.get("AGY_DANGEROUS_SKIP_PERMISSIONS", "0").lower() in ("1", "true", "yes"):
        cmd.append("--dangerously-skip-permissions")
    if extra_args:
        cmd.extend(extra_args)
    return cmd


class AntigravitySession:
    """Manages an active agy process with stream-json input/output."""

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen[str]] = None
        self.conversation_id: Optional[str] = None
        self._unread_line: Optional[str] = None
        self._start_process()

    def _start_process(self) -> None:
        cmd = build_agy_command()
        self.proc = subprocess.Popen(
            cmd,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                    self.proc.wait(timeout=2)
                except Exception:
                    pass
        self.proc = None
        self._unread_line = None

    def restart(self) -> None:
        self.kill()
        self._start_process()

    def unread_line(self, line: str) -> None:
        self._unread_line = line

    def read_line(self, timeout_sec: float) -> Optional[str]:
        if self._unread_line is not None:
            line = self._unread_line
            self._unread_line = None
            return line
        if not self.proc or self.proc.stdout is None:
            return None
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            rem = max(0.01, deadline - time.time())
            r, _, _ = select.select([self.proc.stdout], [], [], min(rem, 0.2))
            if not r:
                if self.proc.poll() is not None:
                    return None
                continue
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    return None
                continue
            return line.strip()
        return None

    def run_task(
        self,
        task_id: str,
        role: str,
        goal: str,
        attempt: int,
        timeout_sec: int,
    ) -> None:
        if not self.is_alive():
            self._start_process()

        emit_bridge_event(
            "task_started",
            task_id,
            role,
            "RUNNING",
            attempt=attempt,
            extra_data={"cwd": self.cwd},
        )

        prompt = (
            f"You are an interchangeable OpenRig seat ({SEAT_ID}).\n"
            f"Your temporary role for THIS task is '{role}'.\n"
            f"Stay strictly inside the repository worktree ({self.cwd}).\n\n"
            f"Goal: {goal}"
        )

        # Send NDJSON message
        msg = {
            "event": "user",
            "message": {
                "content": prompt,
            },
        }

        try:
            assert self.proc and self.proc.stdin is not None
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except Exception as exc:
            emit_bridge_event(
                "task_failed",
                task_id,
                role,
                "ERROR",
                attempt=attempt,
                exit_code=1,
                extra_data={"error": f"Failed to send input: {exc}"},
            )
            self.restart()
            return

        start_time = time.time()
        resp_text = ""
        with open(LOG_PATH, "a", encoding="utf-8") as log_file:
            while True:
                elapsed = time.time() - start_time
                remaining = timeout_sec - elapsed
                if remaining <= 0:
                    # Timeout expired
                    self.kill()
                    emit_bridge_event(
                        "task_timeout",
                        task_id,
                        role,
                        "TIMEOUT",
                        attempt=attempt,
                        exit_code=124,
                        extra_data={"timeout_sec": timeout_sec},
                    )
                    return

                line = self.read_line(timeout_sec=remaining)
                if line is None:
                    if not self.is_alive():
                        # Child died unexpectedly
                        emit_bridge_event(
                            "task_result",
                            task_id,
                            role,
                            "ERROR",
                            attempt=attempt,
                            exit_code=self.proc.returncode if self.proc else 1,
                            extra_data={"error": "Antigravity process terminated prematurely"},
                        )
                        self.restart()
                        return
                    continue

                log_file.write(line + "\n")
                log_file.flush()

                try:
                    event_obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                provider_event = event_obj.get("event")

                if provider_event == "init":
                    init_data = event_obj.get("init", {})
                    self.conversation_id = event_obj.get("conversation_id")
                    emit_bridge_event(
                        "runtime_ready",
                        task_id,
                        role,
                        "READY",
                        attempt=attempt,
                        extra_data={
                            "conversation_id": self.conversation_id,
                            "cwd": init_data.get("cwd"),
                            "tools_count": len(init_data.get("tools", [])),
                        },
                    )

                elif provider_event == "step_update":
                    step_data = event_obj.get("step_update", {})
                    delta = step_data.get("text_delta") or ""
                    resp_text += delta
                    emit_bridge_event(
                        "progress",
                        task_id,
                        role,
                        "RUNNING",
                        attempt=attempt,
                        extra_data={
                            "step_index": step_data.get("step_index"),
                            "step_type": step_data.get("step_type"),
                            "state": step_data.get("state"),
                            "text_delta": delta,
                        },
                    )

                    if step_data.get("state") == "DONE" and step_data.get("step_type") == "agent_response":
                        # Peek ahead with 2.0s window to consume follow-up tool execution or result event
                        next_line = self.read_line(timeout_sec=2.0)
                        if next_line is not None:
                            self.unread_line(next_line)
                        else:
                            emit_bridge_event(
                                "task_result",
                                task_id,
                                role,
                                "SUCCESS",
                                attempt=attempt,
                                exit_code=0,
                                extra_data={
                                    "response": resp_text,
                                    "duration_seconds": step_data.get("duration_seconds", 0),
                                    "usage": step_data.get("usage", {}),
                                },
                            )
                            return

                elif provider_event == "result":
                    res_data = event_obj.get("result", {})
                    status_str = res_data.get("status", "SUCCESS")
                    resp_text = res_data.get("response", "")
                    duration = res_data.get("duration_seconds", 0)
                    emit_bridge_event(
                        "task_result",
                        task_id,
                        role,
                        status_str,
                        attempt=attempt,
                        exit_code=0 if status_str == "SUCCESS" else 1,
                        extra_data={
                            "response": resp_text,
                            "duration_seconds": duration,
                            "usage": res_data.get("usage", {}),
                        },
                    )
                    # Turn complete. NOTE: Agent turn SUCCESS != Software DONE
                    return


def main() -> int:
    session = AntigravitySession(cwd=os.getcwd())
    seq = 0

    try:
        for line in sys.stdin:
            raw = line.strip()
            if not raw:
                continue
            if raw in (":quit", ":exit"):
                break

            seq += 1

            try:
                obj = json.loads(raw)
                if not isinstance(obj, dict):
                    raise ValueError("Task envelope must be a JSON object")
            except Exception as exc:
                emit_bridge_event(
                    "task_failed",
                    f"{SEAT_ID}-{seq}",
                    "general",
                    "INVALID_JSON",
                    attempt=1,
                    exit_code=2,
                    extra_data={"error": str(exc)},
                )
                continue

            task_id = str(obj.get("task_id") or f"{SEAT_ID}-{seq}")
            role = str(obj.get("role") or "general")
            goal = str(obj.get("goal") or "")
            attempt = int(obj.get("attempt") or 1)
            timeout_sec = int(obj.get("timeout_sec") or DEFAULT_TIMEOUT)
            if timeout_sec <= 0:
                timeout_sec = DEFAULT_TIMEOUT

            session.run_task(
                task_id=task_id,
                role=role,
                goal=goal,
                attempt=attempt,
                timeout_sec=timeout_sec,
            )
    finally:
        session.kill()

    return 0


if __name__ == "__main__":
    sys.exit(main())
