"""Structured event emission and optional OS desktop notification for Boss Rig."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVENTS_FILE = REPO_ROOT / ".openrig-bridge" / "events.jsonl"

# Regex patterns for common secrets to redact
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


class EventEmitter:
    """Manages appending structured, redacted events to events.jsonl and firing desktop alerts."""

    def __init__(self, log_path: Optional[Path] = None, enable_notifications: bool = True):
        self.log_path = log_path or DEFAULT_EVENTS_FILE
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.enable_notifications = enable_notifications

    def emit(
        self,
        event_type: str,
        task_id: str,
        data: Optional[Dict[str, Any]] = None,
        spec_id: Optional[str] = None,
        notification_title: Optional[str] = None,
        notification_body: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Emit a structured event, append to events.jsonl, and optionally send OS alert."""
        now = time.time()
        event = {
            "timestamp": now,
            "iso_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "event_type": event_type,
            "task_id": task_id,
            "spec_id": spec_id,
            "data": data or {},
        }

        # Redact any sensitive content in event JSON
        serialized = json.dumps(event)
        safe_serialized = redact_secrets(serialized)

        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(safe_serialized + "\n")
        except Exception as exc:
            sys.stderr.write(f"[WARN] Failed to write event to {self.log_path}: {exc}\n")

        # Send OS desktop toast if requested
        if self.enable_notifications and notification_title and notification_body:
            self._send_os_notification(notification_title, notification_body)

        return event

    def _send_os_notification(self, title: str, body: str) -> None:
        """Send Windows OS toast notification via PowerShell (non-blocking)."""
        safe_title = redact_secrets(title).replace("'", "''").replace('"', '`"')
        safe_body = redact_secrets(body).replace("'", "''").replace('"', '`"')

        if sys.platform == "win32":
            ps_script = f"""
            [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
            $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
            $textNodes = $template.GetElementsByTagName("text")
            $textNodes.Item(0).AppendChild($template.CreateTextNode("{safe_title}")) > $null
            $textNodes.Item(1).AppendChild($template.CreateTextNode("{safe_body}")) > $null
            $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
            [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Boss Rig").Show($toast)
            """
            try:
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
        else:
            # Fallback for Linux / WSL if notify-send exists
            try:
                subprocess.Popen(
                    ["notify-send", safe_title, safe_body],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
