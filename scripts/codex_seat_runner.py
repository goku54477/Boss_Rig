#!/usr/bin/env python3
"""Codex persistent seat runner for OpenRig."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

SEAT_ID = os.environ.get("OPENRIG_SEAT_ID", "codex-1")
NOTIFY_SEAT = os.environ.get("OPENRIG_NOTIFY_SEAT", "")
DEFAULT_TIMEOUT = int(os.environ.get("CODEX_TASK_TIMEOUT_SEC", "1800"))
DEFAULT_MODEL = os.environ.get("CODEX_MODEL", "")
LOG_PATH = os.environ.get("CODEX_SEAT_LOG", os.path.join(os.getcwd(), ".openrig-codex-seat.log"))


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
        "runtime": "codex",
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


def build_codex_command(prompt: str, cwd: str, model: str = "") -> List[str]:
    cmd = ["codex", "exec", "--json", "-C", cwd]
    
    # Sandboxing & permissions policy:
    # Default is safe workspace sandboxing with automatic approval.
    # Dangerous bypass requires explicit opt-in.
    danger_opt_in = os.environ.get("CODEX_DANGEROUS_SKIP_PERMISSIONS", "0").lower() in ("1", "true", "yes") or \
                    os.environ.get("AGY_DANGEROUS_SKIP_PERMISSIONS", "0").lower() in ("1", "true", "yes")

    if danger_opt_in:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.append("--approve-for-me")

    if model:
        cmd.extend(["-m", model])

    cmd.append(prompt)
    return cmd


def run_codex_task(
    task_id: str,
    role: str,
    goal: str,
    raw_envelope: str,
    attempt: int,
    timeout_sec: int,
    model: str,
) -> None:
    cwd = os.getcwd()
    emit_bridge_event(
        "task_started",
        task_id,
        role,
        "RUNNING",
        attempt=attempt,
        extra_data={"cwd": cwd, "model": model or "default"},
    )

    prompt = (
        f"You are an interchangeable OpenRig seat ({SEAT_ID}).\n"
        f"Your role for THIS task is '{role}'. It is NOT your permanent identity.\n"
        f"Stay inside the current worktree ({cwd}).\n"
        f"Do NOT touch files outside this repository worktree.\n\n"
        f"Goal: {goal}\n\n"
        f"TASK_ENVELOPE={raw_envelope}"
    )

    cmd = build_codex_command(prompt, cwd, model=model)

    last_agent_message = ""
    with open(LOG_PATH, "a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,  # Prevent hanging on stdin
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

        deadline = time.time() + timeout_sec
        timed_out = False

        # Read streaming json lines from codex exec
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                if time.time() > deadline:
                    timed_out = True
                    break
                time.sleep(0.05)
                continue

            log_file.write(line)
            log_file.flush()

            clean_line = line.strip()
            if clean_line.startswith("{") and clean_line.endswith("}"):
                try:
                    event_data = json.loads(clean_line)
                    evt_type = event_data.get("type")
                    if evt_type == "item.completed":
                        item = event_data.get("item", {})
                        if item.get("type") == "agent_message":
                            last_agent_message = item.get("text", "")
                except Exception:
                    pass

        if timed_out:
            try:
                os.killpg(os.getpgid(proc.pid), 15)
                proc.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), 9)
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

        exit_code = proc.wait()

    if exit_code == 0:
        # Agent turn completed. NOTE: Does NOT mean software task is DONE.
        emit_bridge_event(
            "task_result",
            task_id,
            role,
            "SUCCESS",
            attempt=attempt,
            exit_code=0,
            extra_data={"response": last_agent_message},
        )
    else:
        emit_bridge_event(
            "task_result",
            task_id,
            role,
            "ERROR",
            attempt=attempt,
            exit_code=exit_code,
            extra_data={"response": last_agent_message},
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
        role = str(obj.get("role") or "general")
        goal = str(obj.get("goal") or "")
        attempt = int(obj.get("attempt") or 1)
        timeout_sec = int(obj.get("timeout_sec") or DEFAULT_TIMEOUT)
        model = str(obj.get("model") or DEFAULT_MODEL)
        if timeout_sec <= 0:
            timeout_sec = DEFAULT_TIMEOUT

        run_codex_task(
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
