"""Security, process management, approval verification, and path normalization."""

from __future__ import annotations

import hmac
import hashlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Secret redaction patterns
SECRET_PATTERNS = [
    re.compile(r"Bearer\s+([A-Za-z0-9_\-\.]{12,})", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|secret|token|password)[\"'\s:=]+([A-Za-z0-9_\-\.]{12,})", re.IGNORECASE),
    re.compile(r"(sk-[a-zA-Z0-9]{20,})"),
    re.compile(r"(ghp_[a-zA-Z0-9]{30,})"),
]


def redact_secrets(text: str) -> str:
    """Mask credentials and tokens in event strings."""
    redacted = text
    for pat in SECRET_PATTERNS:
        def _mask(match: re.Match[str]) -> str:
            full = match.group(0)
            target = match.group(1) if match.groups() else full
            masked_target = target[:3] + "..." + target[-3:] if len(target) > 8 else "***"
            return full.replace(target, masked_target)
        redacted = pat.sub(_mask, redacted)
    return redacted


def normalize_repo_path(path: Path | str) -> Path:
    """Resolve and normalize repository path across Windows and POSIX."""
    p = Path(path).resolve()
    # Normalize Windows casing if on Windows
    if sys.platform == "win32":
        try:
            # Get canonical casing on Windows
            return Path(os.path.realpath(str(p)))
        except Exception:
            return p
    return p


def create_approval_token(
    spec_id: str,
    repo: str,
    conversation_id: str,
    message_id: str,
    secret_key: str,
) -> str:
    """Generate cryptographic HMAC signature proving trusted user approval from chat."""
    canon_repo = str(normalize_repo_path(repo)).lower()
    payload = f"{spec_id.lower()}|{canon_repo}|{conversation_id}|{message_id}"
    digest = hmac.new(secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"sig-{digest[:32]}"


def verify_approval_token(
    token: str,
    spec_id: str,
    repo: str,
    conversation_id: str,
    message_id: str,
    secret_key: str,
) -> bool:
    """Verify approval token matches expected HMAC."""
    expected = create_approval_token(spec_id, repo, conversation_id, message_id, secret_key)
    return hmac.compare_digest(token.strip(), expected)


def is_pid_alive(pid: int) -> bool:
    """Check if process with given PID is still active."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Windows process existence check
        cmd = ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            output = res.stdout.strip()
            # If PID not found, output starts with "INFO: No tasks"
            return bool(output and f'"{pid}"' in output)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def kill_process_tree(pid: int, timeout_sec: float = 3.0) -> bool:
    """Forcefully terminate a process and all its child processes."""
    if pid <= 0 or not is_pid_alive(pid):
        return True

    if sys.platform == "win32":
        # On Windows, taskkill /F /T kills entire process tree
        cmd = ["taskkill", "/F", "/T", "/PID", str(pid)]
        try:
            subprocess.run(cmd, capture_output=True, check=False)
        except Exception:
            pass
    else:
        # On POSIX / WSL, kill process group
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                if not is_pid_alive(pid):
                    return True
                time.sleep(0.1)
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.1)

    return not is_pid_alive(pid)
