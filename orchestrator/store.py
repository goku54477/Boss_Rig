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

                CREATE TABLE IF NOT EXISTS bridge_tasks (
                    task_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    spec_id TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    allowed_merge_target TEXT NOT NULL DEFAULT 'integration',
                    spec_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'QUEUED',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS bridge_processed_events (
                    event_id TEXT PRIMARY KEY,
                    chat_id TEXT,
                    message_created_at INTEGER,
                    spec_id TEXT,
                    repo_path TEXT,
                    task_id TEXT,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT,
                    processed_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bridge_cursors (
                    chat_id TEXT PRIMARY KEY,
                    last_seen_created_at INTEGER NOT NULL,
                    last_seen_message_id TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bridge_user_questions (
                    question_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    conversation_id TEXT,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    answer_text TEXT,
                    created_at REAL NOT NULL,
                    answered_at REAL
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

    # --- Bridge & Service Methods ---

    def submit_bridge_task(
        self,
        spec: Any,
        idempotency_key: str,
        task_id: Optional[str] = None,
    ) -> Tuple[str, bool]:
        """Atomically record task submission with idempotency key.
        
        Returns (task_id, created_new: bool).
        """
        now = time.time()
        tid = task_id or f"T-RUN-{int(now * 1000)}"
        spec_hash = spec.compute_hash() if hasattr(spec, "compute_hash") else spec.get("spec_id", "")
        spec_json = json.dumps(spec.to_canonical_dict() if hasattr(spec, "to_canonical_dict") else spec)
        repo = spec.repo if hasattr(spec, "repo") else spec.get("repo", "")
        goal = spec.goal if hasattr(spec, "goal") else spec.get("goal", "")
        merge_target = getattr(spec, "allowed_merge_target", "integration")

        with self._connect() as conn:
            # Check idempotency first
            row = conn.execute(
                "SELECT task_id, state FROM bridge_tasks WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row:
                return row["task_id"], False

            conn.execute(
                """
                INSERT INTO bridge_tasks (
                    task_id, idempotency_key, spec_id, repo, goal,
                    allowed_merge_target, spec_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?)
                """,
                (tid, idempotency_key, spec_hash, repo, goal, merge_target, spec_json, now, now),
            )
            return tid, True

    def get_bridge_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bridge_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "task_id": row["task_id"],
                "idempotency_key": row["idempotency_key"],
                "spec_id": row["spec_id"],
                "repo": row["repo"],
                "goal": row["goal"],
                "allowed_merge_target": row["allowed_merge_target"],
                "spec": json.loads(row["spec_json"]) if row["spec_json"] else {},
                "state": row["state"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "error": row["error"],
            }

    def list_bridge_tasks(
        self, limit: int = 50, state: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if state:
                rows = conn.execute(
                    "SELECT * FROM bridge_tasks WHERE state = ? ORDER BY created_at DESC LIMIT ?",
                    (state, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bridge_tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [
                {
                    "task_id": r["task_id"],
                    "idempotency_key": r["idempotency_key"],
                    "spec_id": r["spec_id"],
                    "repo": r["repo"],
                    "goal": r["goal"],
                    "allowed_merge_target": r["allowed_merge_target"],
                    "state": r["state"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "error": r["error"],
                }
                for r in rows
            ]

    def update_bridge_task_state(
        self, task_id: str, state: str, error: Optional[str] = None
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "UPDATE bridge_tasks SET state = ?, error = ?, updated_at = ? WHERE task_id = ?",
                (state, error, now, task_id),
            )

    def claim_event_durable(
        self,
        event_id: str,
        action: str,
        status: str,
        chat_id: Optional[str] = None,
        message_created_at: Optional[int] = None,
        spec_id: Optional[str] = None,
        repo_path: Optional[str] = None,
        task_id: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> bool:
        """Atomically claim an event in bridge_processed_events.
        
        Returns True if newly inserted, False if already claimed.
        """
        now = time.time()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO bridge_processed_events (
                        event_id, chat_id, message_created_at, spec_id, repo_path,
                        task_id, action, status, detail, processed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        chat_id,
                        message_created_at,
                        spec_id,
                        repo_path,
                        task_id,
                        action,
                        status,
                        detail,
                        now,
                    ),
                )
                return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def get_processed_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bridge_processed_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "event_id": row["event_id"],
                "chat_id": row["chat_id"],
                "message_created_at": row["message_created_at"],
                "spec_id": row["spec_id"],
                "repo_path": row["repo_path"],
                "task_id": row["task_id"],
                "action": row["action"],
                "status": row["status"],
                "detail": row["detail"],
                "processed_at": row["processed_at"],
            }

    def update_processed_event(
        self,
        event_id: str,
        status: str,
        task_id: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE bridge_processed_events
                SET status = ?, task_id = COALESCE(?, task_id), detail = COALESCE(?, detail), processed_at = ?
                WHERE event_id = ?
                """,
                (status, task_id, detail, now, event_id),
            )

    def get_bridge_task_by_idempotency(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_id FROM bridge_tasks WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if not row:
                return None
            return self.get_bridge_task(row["task_id"])

    def is_event_claimed(self, event_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT event_id FROM bridge_processed_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            return row is not None

    def get_cursor(self, chat_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bridge_cursors WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "chat_id": row["chat_id"],
                "last_seen_created_at": row["last_seen_created_at"],
                "last_seen_message_id": row["last_seen_message_id"],
                "updated_at": row["updated_at"],
            }

    def save_cursor(self, chat_id: str, last_created_at: int, last_msg_id: str) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bridge_cursors (chat_id, last_seen_created_at, last_seen_message_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    last_seen_created_at = excluded.last_seen_created_at,
                    last_seen_message_id = excluded.last_seen_message_id,
                    updated_at = excluded.updated_at
                """,
                (chat_id, last_created_at, last_msg_id, now),
            )

    def create_question(
        self,
        question_id: str,
        task_id: str,
        prompt: str,
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bridge_user_questions (
                    question_id, task_id, conversation_id, prompt, status, created_at
                ) VALUES (?, ?, ?, ?, 'PENDING', ?)
                """,
                (question_id, task_id, conversation_id, prompt, now),
            )
            return {
                "question_id": question_id,
                "task_id": task_id,
                "conversation_id": conversation_id,
                "prompt": prompt,
                "status": "PENDING",
                "answer_text": None,
                "created_at": now,
                "answered_at": None,
            }

    def get_question(self, question_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bridge_user_questions WHERE question_id = ?",
                (question_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "question_id": row["question_id"],
                "task_id": row["task_id"],
                "conversation_id": row["conversation_id"],
                "prompt": row["prompt"],
                "status": row["status"],
                "answer_text": row["answer_text"],
                "created_at": row["created_at"],
                "answered_at": row["answered_at"],
            }

    def list_questions(
        self, task_id: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            query = "SELECT * FROM bridge_user_questions WHERE 1=1"
            params: List[Any] = []
            if task_id:
                query += " AND task_id = ?"
                params.append(task_id)
            if status:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY created_at ASC"
            rows = conn.execute(query, tuple(params)).fetchall()
            return [
                {
                    "question_id": r["question_id"],
                    "task_id": r["task_id"],
                    "conversation_id": r["conversation_id"],
                    "prompt": r["prompt"],
                    "status": r["status"],
                    "answer_text": r["answer_text"],
                    "created_at": r["created_at"],
                    "answered_at": r["answered_at"],
                }
                for r in rows
            ]

    def answer_question(self, question_id: str, answer_text: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._connect() as conn:
            # Check question is PENDING
            row = conn.execute(
                "SELECT * FROM bridge_user_questions WHERE question_id = ?",
                (question_id,),
            ).fetchone()
            if not row:
                return None
            if row["status"] != "PENDING":
                return {
                    "question_id": row["question_id"],
                    "status": row["status"],
                    "error": f"Question {question_id} is already in status {row['status']}",
                }
            conn.execute(
                """
                UPDATE bridge_user_questions
                SET status = 'ANSWERED', answer_text = ?, answered_at = ?
                WHERE question_id = ?
                """,
                (answer_text, now, question_id),
            )
            return {
                "question_id": question_id,
                "task_id": row["task_id"],
                "conversation_id": row["conversation_id"],
                "prompt": row["prompt"],
                "status": "ANSWERED",
                "answer_text": answer_text,
                "created_at": row["created_at"],
                "answered_at": now,
            }

    def cancel_task(self, task_id: str, reason: str = "") -> bool:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE bridge_tasks
                SET state = 'CANCELLED', error = ?, updated_at = ?
                WHERE task_id = ? AND state NOT IN ('DONE', 'CANCELLED')
                """,
                (reason or "Cancelled by operator", now, task_id),
            )
            # Also expire any pending questions
            conn.execute(
                """
                UPDATE bridge_user_questions
                SET status = 'CANCELLED', answered_at = ?
                WHERE task_id = ? AND status = 'PENDING'
                """,
                (now, task_id),
            )
            # Also update tasks table if present
            conn.execute(
                """
                UPDATE tasks
                SET state = 'CANCELLED', updated_at = ?
                WHERE task_id = ? AND state NOT IN ('DONE', 'CANCELLED')
                """,
                (now, task_id),
            )
            return cur.rowcount > 0

