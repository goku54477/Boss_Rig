#!/usr/bin/env python3
"""OpenCode persistent seat runner for OpenRig."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, Optional

SEAT_ID = os.environ.get("OPENRIG_SEAT_ID", "opencode-1")
NOTIFY_SEAT = os.environ.get("OPENRIG_NOTIFY_SEAT", "")
DEFAULT_TIMEOUT = int(os.environ.get("OPENCODE_TASK_TIMEOUT_SEC", "1800"))
DEFAULT_MODEL = os.environ.get("OPENCODE_MODEL", "opencode/nemotron-3.5-lightning-free")
LOG_PATH = os.environ.get("OPENCODE_SEAT_LOG", os.path.join(os.getcwd(), ".openrig-opencode-seat.log"))


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
        "runtime": "opencode",
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


def run_opencode_task(
    task_id: str,
    role: str,
    goal: str,
    raw_envelope: str,
    attempt: int,
    timeout_sec: int,
    model: str,
) -> None:
    emit_bridge_event(
        "task_started",
        task_id,
        role,
        "RUNNING",
        attempt=attempt,
        extra_data={"cwd": os.getcwd(), "model": model},
    )

    prompt = (
        f"You are an interchangeable OpenRig seat ({SEAT_ID}).\n"
        f"Your role for THIS task is '{role}'. It is NOT your permanent identity.\n"
        f"Stay inside the current worktree ({os.getcwd()}).\n"
        f"Do NOT touch files outside this repository worktree.\n\n"
        f"Goal: {goal}\n\n"
        f"TASK_ENVELOPE={raw_envelope}"
    )

    cmd = [
        "opencode",
        "run",
        "-m",
        model,
        "--dir",
        os.getcwd(),
        "--format",
        "json",
        "--auto",
        prompt,
    ]

    with open(LOG_PATH, "a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )

        try:
            exit_code = proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), 15)  # SIGTERM
                proc.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), 9)  # SIGKILL
                    proc.wait(timeout=2)
                except Exception:
                    pass
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

    if exit_code == 0:
        # Agent turn completed. NOTE: Does NOT mean software task is DONE.
        emit_bridge_event(
            "task_result",
            task_id,
            role,
            "SUCCESS",
            attempt=attempt,
            exit_code=0,
        )
    else:
        emit_bridge_event(
            "task_result",
            task_id,
            role,
            "ERROR",
            attempt=attempt,
            exit_code=exit_code,
        )


def main() -> int:
    seq = 0
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
                raise ValueError("Task message must be a JSON object")
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
        # Role must not default to worker; use neutral 'general' if omitted
        role = str(obj.get("role") or "general")
        goal = str(obj.get("goal") or "")
        attempt = int(obj.get("attempt") or 1)
        timeout_sec = int(obj.get("timeout_sec") or DEFAULT_TIMEOUT)
        model = str(obj.get("model") or DEFAULT_MODEL)
        if timeout_sec <= 0:
            timeout_sec = DEFAULT_TIMEOUT

        run_opencode_task(
            task_id=task_id,
            role=role,
            goal=goal,
            raw_envelope=raw,
            attempt=attempt,
            timeout_sec=timeout_sec,
            model=model,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
