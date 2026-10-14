"""Goldfish Desktop context priming integration for Boss Rig orchestrator."""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


def resolve_goldfish_db() -> Optional[Path]:
    """Locate the Goldfish Desktop SQLite database across WSL, Windows, and Linux."""
    if "GOLDFISH_DB_PATH" in os.environ:
        p = Path(os.environ["GOLDFISH_DB_PATH"])
        if p.exists():
            return p

    # WSL path resolution: /mnt/c/Users/*/AppData/Roaming/goldfish/goldfish.db
    mnt_users = Path("/mnt/c/Users")
    if mnt_users.exists():
        for user_dir in mnt_users.glob("*"):
            candidate = user_dir / "AppData" / "Roaming" / "goldfish" / "goldfish.db"
            if candidate.exists():
                return candidate

    # Windows native path resolution
    appdata = os.environ.get("APPDATA")
    if appdata:
        p = Path(appdata) / "goldfish" / "goldfish.db"
        if p.exists():
            return p

    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        p = Path(localappdata) / "goldfish" / "goldfish.db"
        if p.exists():
            return p

    # Standard Linux / home fallback
    linux_candidate = Path.home() / ".goldfish" / "goldfish.db"
    if linux_candidate.exists():
        return linux_candidate

    return None


class GoldfishPrimer:
    """Provides safe read-only memory priming from Goldfish Desktop into TaskEnvelope."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or resolve_goldfish_db()

    @property
    def is_available(self) -> bool:
        return self.db_path is not None and self.db_path.exists()

    def search_memory(self, query: str, limit: int = 3) -> Dict[str, Any]:
        """Search Goldfish snapshots for memories relevant to query using safe read-only URI."""
        if not self.is_available or not self.db_path:
            return {
                "enabled": False,
                "source": "goldfish_desktop",
                "query": query,
                "count": 0,
                "items": [],
                "summary": "",
            }

        # Tokenize query into search keywords (words >= 3 chars, skipping stop words)
        stop_words = {"the", "and", "for", "with", "from", "that", "this", "create", "defining", "returning", "dict"}
        raw_words = re.findall(r"\b[A-Za-z0-9_]{3,}\b", query)
        keywords = [w for w in raw_words if w.lower() not in stop_words]
        if not keywords:
            keywords = raw_words[:3]

        items: List[Dict[str, Any]] = []
        try:
            # Use URI read-only immutable connection to prevent disk I/O lock issues on Windows/WSL
            uri = f"file:{self.db_path.as_posix()}?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Search snapshots matching any keyword
            for kw in keywords[:3]:
                if len(items) >= limit:
                    break
                pattern = f"%{kw}%"
                rows = cursor.execute(
                    """
                    SELECT snapshot_id, app, window_title, substr(content, 1, 200) AS snippet, captured_at
                    FROM snapshots
                    WHERE content LIKE ? OR window_title LIKE ?
                    ORDER BY captured_at DESC
                    LIMIT ?
                    """,
                    (pattern, pattern, limit - len(items)),
                ).fetchall()

                for r in rows:
                    snap_id = r["snapshot_id"]
                    if not any(it["snapshot_id"] == snap_id for it in items):
                        # Clean single-line snippet
                        snippet = " ".join((r["snippet"] or "").split())
                        items.append({
                            "snapshot_id": snap_id,
                            "app": r["app"] or "unknown",
                            "window_title": r["window_title"] or "untitled",
                            "snippet": snippet[:150],
                            "captured_at": r["captured_at"],
                        })

            conn.close()
        except Exception:
            # Graceful degradation if Goldfish DB is unreadable or locked
            pass

        # Build concise normalized summary string
        if items:
            bullets = [
                f"- [{it['app']}] \"{it['window_title']}\": {it['snippet']}"
                for it in items
            ]
            summary = "\n".join(bullets)
        else:
            summary = ""

        return {
            "enabled": True,
            "source": "goldfish_desktop",
            "query": query,
            "count": len(items),
            "items": items,
            "summary": summary,
        }
