#!/usr/bin/env python3
"""Codex persistent seat runner for OpenRig."""

from __future__ import annotations

from collections import deque
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Deque, Dict, List, Optional

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
    stderr_tail: Deque[str] = deque(maxlen=50)
    stdout_queue: queue.Queue[Optional[str]] = queue.Queue()

    def _pump_stdout(pipe: Any) -> None:
        try:
            for raw_line in iter(pipe.readline, ""):
                stdout_queue.put(raw_line)
        except Exception:
            pass
        finally:
            stdout_queue.put(None)

    def _drain_stderr(pipe: Any) -> None:
        try:
            for raw_line in iter(pipe.readline, ""):
                stripped = raw_line.rstrip("\r\n")
                if stripped:
                    stderr_tail.append(stripped)
        except Exception:
            pass

    def _terminate_proc_tree(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        if hasattr(os, "killpg"):
            for sig, wait_sec in ((signal.SIGTERM, 3), (signal.SIGKILL, 2)):
                try:
                    os.killpg(os.getpgid(proc.pid), sig)
                    proc.wait(timeout=wait_sec)
                    return
                except Exception:
                    pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass

    with open(LOG_PATH, "a", encoding="utf-8") as log_file:
        popen_kwargs: Dict[str, Any] = {
            "stdin": subprocess.DEVNULL,  # Prevent hanging on stdin
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **popen_kwargs)

        assert proc.stdout is not None
        assert proc.stderr is not None
        stdout_thread = threading.Thread(target=_pump_stdout, args=(proc.stdout,), daemon=True)
        stderr_thread = threading.Thread(target=_drain_stderr, args=(proc.stderr,), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        start_t = time.time()
        deadline = start_t + timeout_sec
        timed_out = False
        eof_seen = False
        last_hb = start_t

        while not eof_seen:
            now = time.time()
            if now - last_hb >= 5.0:
                last_hb = now
                emit_bridge_event(
                    "task_heartbeat",
                    task_id,
                    role,
                    "RUNNING",
                    attempt=attempt,
                    extra_data={"elapsed_sec": int(now - start_t)},
                )

            remaining = deadline - now
            if remaining <= 0:
                timed_out = True
                break

            try:
                line = stdout_queue.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                if proc.poll() is not None and stdout_queue.empty():
                    break
                continue

            if line is None:
                eof_seen = True
                break

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
            _terminate_proc_tree(proc)
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except Exception:
                    pass
            stdout_thread.join(timeout=1.0)
            stderr_thread.join(timeout=1.0)
            if stderr_tail:
                log_file.write("\n[stderr tail on timeout]\n" + "\n".join(stderr_tail) + "\n")
                log_file.flush()
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
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)
        if exit_code != 0 and stderr_tail:
            log_file.write("\n[stderr tail]\n" + "\n".join(stderr_tail) + "\n")
            log_file.flush()

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
