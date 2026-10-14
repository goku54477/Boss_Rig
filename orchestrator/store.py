"""Durable SQLite task and event store for Boss Rig orchestrator."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional
from orchestrator.models import BridgeEvent, SeatMode, TaskRecord, TaskState

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / ".openrig-bridge" / "tasks.sqlite"


class TaskStore:
    """Manages persistent SQLite storage of tasks, events, and lifecycle transitions."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        return conn

    @contextlib.contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager providing a SQLite connection with busy retry and backoff."""
        max_retries = 8
        delay = 0.05
        for attempt in range(max_retries):
            try:
                conn = self._get_connection()
                try:
                    with conn:
                        yield conn
                    return
                finally:
                    conn.close()
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if ("locked" in err_msg or "busy" in err_msg) and attempt < max_retries - 1:
                    time.sleep(delay)
                    delay = min(delay * 2.0, 1.0)
                    continue
                raise

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    assigned_engine TEXT,
                    assigned_role TEXT,
                    planner TEXT,
                    implementer TEXT,
                    reviewer TEXT,
                    candidate_commit TEXT,
                    verification_commands TEXT,
                    merge_commit TEXT,
                    attempt INTEGER DEFAULT 1,
                    max_attempts INTEGER DEFAULT 2,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_id TEXT UNIQUE NOT NULL,
                    event_name TEXT NOT NULL,
                    seat_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER DEFAULT 1,
                    data_json TEXT,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    reason TEXT,
                    metadata_json TEXT,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS seat_controls (
                    seat_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL DEFAULT 'AUTOMATED',
                    reason TEXT,
                    held_by TEXT,
                    updated_at REAL NOT NULL
                );
                """
            )

    def save_task(self, task: TaskRecord) -> None:
        now = time.time()
        v_cmds_json = json.dumps(task.verification_commands)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, state, goal, assigned_engine, assigned_role,
                    planner, implementer, reviewer, candidate_commit,
                    verification_commands, merge_commit, attempt, max_attempts,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    state = excluded.state,
                    goal = excluded.goal,
                    assigned_engine = excluded.assigned_engine,
                    assigned_role = excluded.assigned_role,
                    planner = excluded.planner,
                    implementer = excluded.implementer,
                    reviewer = excluded.reviewer,
                    candidate_commit = excluded.candidate_commit,
                    verification_commands = excluded.verification_commands,
                    merge_commit = excluded.merge_commit,
                    attempt = excluded.attempt,
                    max_attempts = excluded.max_attempts,
                    updated_at = excluded.updated_at;
                """,
                (
                    task.task_id,
                    task.state.value if isinstance(task.state, TaskState) else str(task.state),
                    task.goal,
                    task.assigned_engine,
                    task.assigned_role,
                    task.planner,
                    task.implementer,
                    task.reviewer,
                    task.candidate_commit,
                    v_cmds_json,
                    task.merge_commit,
                    task.attempt,
                    task.max_attempts,
                    now,
                    now,
                ),
            )

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                return None

            # Fetch transition history
            hist_rows = conn.execute(
                "SELECT * FROM task_transitions WHERE task_id = ? ORDER BY id ASC",
                (task_id,),
            ).fetchall()
            history: List[Dict[str, Any]] = []
            for h in hist_rows:
                entry = {
                    "from": h["from_state"],
                    "to": h["to_state"],
                    "reason": h["reason"],
                    "timestamp": h["created_at"],
                }
                if h["metadata_json"]:
                    try:
                        entry.update(json.loads(h["metadata_json"]))
                    except Exception:
                        pass
                history.append(entry)

            v_cmds = []
            if row["verification_commands"]:
                try:
                    v_cmds = json.loads(row["verification_commands"])
                except Exception:
                    pass

            record = TaskRecord(
                task_id=row["task_id"],
                state=TaskState(row["state"]),
                goal=row["goal"],
                assigned_engine=row["assigned_engine"],
                assigned_role=row["assigned_role"],
                planner=row["planner"],
                implementer=row["implementer"],
                reviewer=row["reviewer"],
                candidate_commit=row["candidate_commit"],
                verification_commands=v_cmds,
                merge_commit=row["merge_commit"],
                attempt=row["attempt"],
                max_attempts=row["max_attempts"],
                history=history,
            )
            return record

    def list_tasks(self, state: Optional[TaskState] = None) -> List[TaskRecord]:
        query = "SELECT task_id FROM tasks"
        params: List[Any] = []
        if state:
            query += " WHERE state = ?"
            params.append(state.value if isinstance(state, TaskState) else str(state))
        query += " ORDER BY created_at DESC"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            tasks: List[TaskRecord] = []
            for r in rows:
                t = self.get_task(r["task_id"])
                if t:
                    tasks.append(t)
            return tasks

    def record_event(
        self,
        event: Dict[str, Any] | BridgeEvent,
    ) -> bool:
        """Record bridge event with idempotent deduplication. Returns True if recorded, False if duplicate."""
        if isinstance(event, BridgeEvent):
            task_id = event.task_id
            event_name = event.event
            seat_id = event.seat_id
            role = event.role
            status = event.status
            attempt = event.attempt
            data = event.data
        else:
            task_id = str(event.get("task_id", ""))
            event_name = str(event.get("event", ""))
            seat_id = str(event.get("seat_id", ""))
            role = str(event.get("role", ""))
            status = str(event.get("status", ""))
            attempt = int(event.get("attempt", 1))
            data = event.get("data", {})

        # Compute deterministic event identity key for deduplication
        event_id = f"{task_id}:{seat_id}:{event_name}:{attempt}:{status}"
        now = time.time()
        data_json = json.dumps(data)

        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO task_events (
                        task_id, event_id, event_name, seat_id,
                        role, status, attempt, data_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        event_id,
                        event_name,
                        seat_id,
                        role,
                        status,
                        attempt,
                        data_json,
                        now,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                # Duplicate event ignored idempotently
                return False

    def transition_task(
        self,
        task_id: str,
        new_state: TaskState,
        reason: str = "",
        **metadata: Any,
    ) -> TaskRecord:
        task = self.get_task(task_id)
        if not task:
            raise KeyError(f"Task {task_id} not found")

        old_state = task.state
        task.transition(new_state, reason=reason, **metadata)
        now = time.time()

        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?",
                (new_state.value, now, task_id),
            )
            conn.execute(
                """
                INSERT INTO task_transitions (
                    task_id, from_state, to_state, reason, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    old_state.value,
                    new_state.value,
                    reason,
                    json.dumps(metadata) if metadata else None,
                    now,
                ),
            )
        return task

    def block_task(self, task_id: str, reason: str = "") -> TaskRecord:
        return self.transition_task(task_id, TaskState.BLOCKED, reason=reason)

    def cancel_task(self, task_id: str, reason: str = "") -> TaskRecord:
        return self.transition_task(task_id, TaskState.CANCELLED, reason=reason)

    def set_seat_mode(
        self,
        seat_id: str,
        mode: SeatMode,
        reason: str = "",
        held_by: str = "operator",
    ) -> Dict[str, Any]:
        """Update or insert the operational mode of a seat."""
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO seat_controls (seat_id, mode, reason, held_by, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(seat_id) DO UPDATE SET
                    mode = excluded.mode,
                    reason = excluded.reason,
                    held_by = excluded.held_by,
                    updated_at = excluded.updated_at
                """,
                (seat_id, mode.value, reason, held_by, now),
            )
        return self.get_seat_mode(seat_id)

    def get_seat_mode(self, seat_id: str) -> Dict[str, Any]:
        """Fetch current seat operational mode (defaulting to AUTOMATED)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT seat_id, mode, reason, held_by, updated_at FROM seat_controls WHERE seat_id = ?",
                (seat_id,),
            ).fetchone()
            if row:
                return {
                    "seat_id": row["seat_id"],
                    "mode": SeatMode(row["mode"]),
                    "reason": row["reason"] or "",
                    "held_by": row["held_by"] or "",
                    "updated_at": row["updated_at"],
                }
            return {
                "seat_id": seat_id,
                "mode": SeatMode.AUTOMATED,
                "reason": "",
                "held_by": "",
                "updated_at": 0.0,
            }

    def list_seat_modes(self) -> Dict[str, Dict[str, Any]]:
        """List operational modes for all registered seats."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seat_id, mode, reason, held_by, updated_at FROM seat_controls"
            ).fetchall()
            result = {}
            for row in rows:
                result[row["seat_id"]] = {
                    "seat_id": row["seat_id"],
                    "mode": SeatMode(row["mode"]),
                    "reason": row["reason"] or "",
                    "held_by": row["held_by"] or "",
                    "updated_at": row["updated_at"],
                }
            return result
