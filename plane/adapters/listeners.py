"""Read-only Listener adapters for Boss Rig Shared Plane."""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from orchestrator.chat_watcher import resolve_goldfish_chats_dir
from orchestrator.store import TaskStore
from plane.schemas import PlaneMessage

REPORT_MARKER = "[SHARED PLANE REPORT"
SHARED_PLANE_MARKERS = (REPORT_MARKER, "[SHARED PLANE]")


def _contains_shared_plane_marker(text: str) -> bool:
    return any(marker in text for marker in SHARED_PLANE_MARKERS)


def _extract_text(content: Any) -> str:
    """Safely normalize string or structured-block message content into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def _parse_timestamp(raw_ts: Any, fallback: float) -> float:
    """Normalize ms or sec timestamps to Unix seconds."""
    if raw_ts is None:
        return fallback
    try:
        val = float(raw_ts)
        return val / 1000.0 if val > 1e12 else val
    except (TypeError, ValueError):
        return fallback


class BaseListener(ABC):
    """Base contract for read-only Shared Plane event listeners."""

    @abstractmethod
    def baseline(self) -> int:
        """Record existing messages as seen without emitting them."""

    @abstractmethod
    def poll(self) -> List[PlaneMessage]:
        """Return newly observed user messages since last poll."""


class GoldfishChatListener(BaseListener):
    """Strictly read-only Goldfish transcript listener (%APPDATA%/goldfish/chats/*.json)."""

    def __init__(
        self,
        chats_dir: Optional[Path] = None,
        store: Optional[TaskStore] = None,
        context_window: int = 3,
    ) -> None:
        self.chats_dir = chats_dir or resolve_goldfish_chats_dir()
        self.store = store or TaskStore()
        self.context_window = context_window
        self._file_fingerprints: Dict[Path, Tuple[int, int]] = {}

    def _iter_chat_files(self) -> List[Path]:
        if not self.chats_dir or not self.chats_dir.exists():
            return []
        return sorted(
            p for p in self.chats_dir.glob("*.json") if p.name.lower() != "inbox.json"
        )

    def baseline(self) -> int:
        """Batch-claim all existing message IDs so startup never replays historical chats."""
        files = self._iter_chat_files()
        if not files:
            return 0

        now = time.time()
        rows_to_claim: List[Tuple[str, str, Optional[int], str, str, str, float]] = []
        for chat_file in files:
            try:
                st = chat_file.stat()
                data = json.loads(chat_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, PermissionError, OSError):
                continue

            chat_id = str(data.get("id") or chat_file.stem)
            for msg in data.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("id")
                if not msg_id:
                    continue
                raw_ts = msg.get("createdAt")
                msg_ts = int(raw_ts) if isinstance(raw_ts, (int, float)) else None
                rows_to_claim.append(
                    (
                        str(msg_id),
                        chat_id,
                        msg_ts,
                        "PLANE_BASELINED",
                        "IGNORED",
                        "Message existed prior to Shared Plane startup",
                        now,
                    )
                )
            self._file_fingerprints[chat_file] = (st.st_mtime_ns, st.st_size)

        if not rows_to_claim:
            return 0

        inserted = 0
        with self.store._connect() as conn:
            for row in rows_to_claim:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO bridge_processed_events (
                        event_id, chat_id, message_created_at, action, status, detail, processed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                inserted += cur.rowcount
        return inserted

    def poll(self) -> List[PlaneMessage]:
        """Read changed Goldfish chat JSON files and emit unclaimed user turns."""
        emitted: List[PlaneMessage] = []
        for chat_file in self._iter_chat_files():
            try:
                st = chat_file.stat()
                fingerprint = (st.st_mtime_ns, st.st_size)
                if self._file_fingerprints.get(chat_file) == fingerprint:
                    continue
                data = json.loads(chat_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, PermissionError, OSError):
                # Goldfish may be holding or writing the file mid-poll; retry next tick.
                continue

            self._file_fingerprints[chat_file] = fingerprint
            chat_id = str(data.get("id") or chat_file.stem)
            chat_fallback_ts = _parse_timestamp(
                data.get("updatedAt") or data.get("createdAt"), time.time()
            )
            messages = data.get("messages") or []
            if not isinstance(messages, list):
                continue

            for idx, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("id")
                if not msg_id or msg.get("role") != "user":
                    continue
                msg_id_str = str(msg_id)
                if self.store.is_event_claimed(msg_id_str):
                    continue

                text = _extract_text(msg.get("content", ""))
                if _contains_shared_plane_marker(text):
                    self.store.claim_event_durable(
                        event_id=msg_id_str,
                        chat_id=chat_id,
                        action="PLANE_IGNORED",
                        status="IGNORED",
                        detail="Ignored Shared Plane report/log marker",
                    )
                    continue

                context: List[str] = []
                if self.context_window > 0:
                    start = max(0, idx - self.context_window)
                    for prev in messages[start:idx]:
                        if not isinstance(prev, dict):
                            continue
                        prev_text = _extract_text(prev.get("content", "")).strip()
                        if prev_text and not _contains_shared_plane_marker(prev_text):
                            context.append(f"{prev.get('role', 'unknown')}: {prev_text[:280]}")

                raw_ts = msg.get("createdAt")
                msg_ts_sec = _parse_timestamp(raw_ts, chat_fallback_ts)
                claimed = self.store.claim_event_durable(
                    event_id=msg_id_str,
                    chat_id=chat_id,
                    message_created_at=int(msg_ts_sec * 1000),
                    action="PLANE_INGESTED",
                    status="INGESTED",
                    detail="Ingested by GoldfishChatListener",
                )
                if not claimed:
                    continue

                emitted.append(
                    PlaneMessage(
                        id=msg_id_str,
                        source="goldfish",
                        conversation_id=chat_id,
                        role="user",
                        text=text,
                        created_at=msg_ts_sec,
                        recent_context=context,
                    )
                )
        return emitted


class JsonlBusListener(BaseListener):
    """Minimal read-only JSONL bus adapter (~/.boss-rig/bus.jsonl) for external brains."""

    def __init__(self, bus_path: Optional[Path] = None, store: Optional[TaskStore] = None) -> None:
        self.bus_path = bus_path or (Path.home() / ".boss-rig" / "bus.jsonl")
        self.store = store or TaskStore()

    def _read_entries(self) -> List[Dict[str, Any]]:
        if not self.bus_path.exists():
            return []
        try:
            lines = self.bus_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries: List[Dict[str, Any]] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict) and obj.get("id"):
                    entries.append(obj)
            except json.JSONDecodeError:
                continue
        return entries

    def baseline(self) -> int:
        count = 0
        for obj in self._read_entries():
            if self.store.claim_event_durable(
                event_id=str(obj["id"]),
                chat_id=str(obj.get("conversation_id", "bus")),
                action="PLANE_BASELINED",
                status="IGNORED",
                detail="JSONL bus entry existed prior to startup",
            ):
                count += 1
        return count

    def poll(self) -> List[PlaneMessage]:
        emitted: List[PlaneMessage] = []
        for obj in self._read_entries():
            msg_id = str(obj["id"])
            if obj.get("role", "user") != "user" or self.store.is_event_claimed(msg_id):
                continue
            text = _extract_text(obj.get("text") or obj.get("content") or "")
            if not text.strip() or _contains_shared_plane_marker(text):
                continue
            conv_id = str(obj.get("conversation_id") or "bus")
            if not self.store.claim_event_durable(
                event_id=msg_id, chat_id=conv_id, action="PLANE_INGESTED", status="INGESTED"
            ):
                continue
            emitted.append(
                PlaneMessage(
                    id=msg_id,
                    source=str(obj.get("source") or "jsonl_bus"),
                    conversation_id=conv_id,
                    role="user",
                    text=text,
                    created_at=_parse_timestamp(obj.get("created_at"), time.time()),
                )
            )
        return emitted
