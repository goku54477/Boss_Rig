"""Multi-channel Speaker adapters for Boss Rig Shared Plane."""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List

from orchestrator.event_emitter import EventEmitter
from plane.schemas import BuildReport

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically via a temporary file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def read_latest_report(reports_dir: Path) -> Dict[str, Any]:
    """Read the latest BuildReport JSON and Markdown artifacts if present."""
    latest_json = reports_dir / "latest_report.json"
    latest_md = reports_dir / "latest_report.md"

    if not latest_json.exists() and not latest_md.exists():
        return {"available": False, "report": None, "markdown": ""}

    report_data: Any = None
    if latest_json.exists():
        try:
            report_data = json.loads(latest_json.read_text(encoding="utf-8"))
        except Exception:
            report_data = None

    markdown = ""
    if latest_md.exists():
        try:
            markdown = latest_md.read_text(encoding="utf-8")
        except Exception:
            markdown = ""
    elif isinstance(report_data, dict):
        markdown = str(report_data.get("markdown", ""))

    return {
        "available": bool(report_data or markdown),
        "report": report_data,
        "markdown": markdown,
    }


class BaseSpeaker(ABC):
    """Base contract for Shared Plane terminal report delivery channels."""

    @abstractmethod
    def speak(self, report: BuildReport) -> Dict[str, Any]:
        """Deliver a BuildReport through this channel."""


class ReportFileSpeaker(BaseSpeaker):
    """Atomically writes latest_report.md, latest_report.json, and <task_id>.md."""

    def __init__(self, reports_dir: Path) -> None:
        self.reports_dir = Path(reports_dir)

    def speak(self, report: BuildReport) -> Dict[str, Any]:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        markdown = report.to_markdown()
        payload = json.dumps(report.to_dict(), indent=2)

        safe_id = _SAFE_ID_RE.sub("_", report.task_id).strip("_") or "task"
        per_task_md = self.reports_dir / f"{safe_id}.md"
        latest_md = self.reports_dir / "latest_report.md"
        latest_json = self.reports_dir / "latest_report.json"

        _atomic_write_text(per_task_md, markdown)
        _atomic_write_text(latest_md, markdown)
        _atomic_write_text(latest_json, payload)

        return {
            "channel": "report_file",
            "task_markdown": str(per_task_md),
            "latest_markdown": str(latest_md),
            "latest_json": str(latest_json),
        }


class ToastNotificationSpeaker(BaseSpeaker):
    """Fires structured event log and Windows desktop toast via EventEmitter."""

    def __init__(self, emitter: EventEmitter) -> None:
        self.emitter = emitter

    def speak(self, report: BuildReport) -> Dict[str, Any]:
        title = f"[Shared Plane] {report.task_id} — {report.status}"
        summary_part = report.test_summary or report.status
        body = f"{report.goal[:80]} ({summary_part})"
        event = self.emitter.emit(
            "SHARED_PLANE_REPORT",
            task_id=report.task_id,
            data=report.to_dict(),
            notification_title=title,
            notification_body=body,
        )
        return {"channel": "toast", "event_type": event["event_type"], "task_id": report.task_id}


class CompositeSpeaker(BaseSpeaker):
    """Dispatches BuildReport across multiple Speaker channels with fault isolation."""

    def __init__(self, speakers: List[BaseSpeaker]) -> None:
        self.speakers = list(speakers)

    def speak(self, report: BuildReport) -> Dict[str, Any]:
        results: List[Dict[str, Any]] = []
        for speaker in self.speakers:
            try:
                results.append({"ok": True, **speaker.speak(report)})
            except Exception as exc:
                results.append({"ok": False, "speaker": type(speaker).__name__, "error": str(exc)})
        return {"results": results}
