#!/usr/bin/env python3
"""Regression tests for Goldfish Context Priming integration."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.engine import OrchestrationEngine
from orchestrator.goldfish import GoldfishPrimer, resolve_goldfish_db
from orchestrator.models import TaskEnvelope, TaskRecord, TaskState
from orchestrator.store import TaskStore


class TestGoldfishContextPriming(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_goldfish.db"

        # Initialize mock Goldfish SQLite schema
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript(
            """
            CREATE TABLE snapshots (
                snapshot_id TEXT PRIMARY KEY,
                app TEXT,
                window_title TEXT,
                content TEXT,
                captured_at REAL
            );

            INSERT INTO snapshots (snapshot_id, app, window_title, content, captured_at) VALUES
            ('s1', 'IDE', 'Boss Rig Architecture', 'Architectural directive: OpenRig remains single control plane', 1700000000),
            ('s2', 'Browser', 'Metrics Collector RFC', 'Design spec for metrics_collector: define collect_repo_metrics', 1700000100),
            ('s3', 'Terminal', 'Worktree Gate Specs', 'All seat worktrees are isolated under .worktrees/', 1700000200);
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_01_search_memory_with_keyword_matching(self) -> None:
        """Requirement: search relevant project memory and produce concise normalized summary."""
        primer = GoldfishPrimer(db_path=self.db_path)
        self.assertTrue(primer.is_available)

        res = primer.search_memory("Create metrics_collector module", limit=3)
        self.assertTrue(res["enabled"])
        self.assertEqual(res["source"], "goldfish_desktop")
        self.assertGreaterEqual(res["count"], 1)

        # Verify items and summary formatting
        items = res["items"]
        titles = [it["window_title"] for it in items]
        self.assertIn("Metrics Collector RFC", titles)
        self.assertIn("collect_repo_metrics", res["summary"])

    def test_02_graceful_fallback_when_db_missing(self) -> None:
        """Requirement: When Goldfish DB is absent or query yields no matches, degrade gracefully."""
        primer = GoldfishPrimer(db_path=Path("/tmp/non_existent_goldfish_db.sqlite"))
        self.assertFalse(primer.is_available)

        res = primer.search_memory("any query")
        self.assertFalse(res["enabled"])
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["items"], [])
        self.assertEqual(res["summary"], "")

    def test_03_engine_automatic_context_priming(self) -> None:
        """Requirement: OrchestrationEngine.create_task attaches context and propagates to TaskEnvelope."""
        store_path = Path(self.temp_dir.name) / "tasks.sqlite"
        store = TaskStore(db_path=store_path)
        engine = OrchestrationEngine(store=store)

        # Inject primer with our temp db
        primer = GoldfishPrimer(db_path=self.db_path)
        primed_data = primer.search_memory("OpenRig architecture and worktree gate")

        task = engine.create_task(
            task_id="T-PRIMING-01",
            goal="Refactor worktree gate",
            context=primed_data,
        )

        self.assertIsNotNone(task.context)
        self.assertEqual(task.context.get("source"), "goldfish_desktop")
        self.assertIn("OpenRig", task.context.get("summary", ""))

        # Verify TaskEnvelope serialization
        envelope = TaskEnvelope(
            task_id="T-PRIMING-01",
            role="planner",
            goal=task.goal,
            context=task.context,
        )
        env_dict = envelope.to_dict()
        self.assertIn("context", env_dict)
        self.assertEqual(env_dict["context"]["source"], "goldfish_desktop")


if __name__ == "__main__":
    unittest.main()
