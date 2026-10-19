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

                CREATE TABLE IF NOT EXISTS bridge_approval_nonces (
                    nonce TEXT PRIMARY KEY,
                    spec_id TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    conversation_id TEXT,
                    consumed_at REAL NOT NULL,
                    task_id TEXT
                );

                CREATE TABLE IF NOT EXISTS bridge_task_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    spec_id TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING_APPROVAL',
                    source TEXT NOT NULL,
                    conversation_id TEXT,
                    message_id TEXT,
                    approval_token TEXT,
                    rejection_reason TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL
                );

                CREATE TABLE IF NOT EXISTS bridge_task_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_id TEXT UNIQUE NOT NULL,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    lifecycle_status TEXT NOT NULL,
                    role TEXT,
                    seat_id TEXT,
                    summary TEXT NOT NULL DEFAULT '',
                    attempt INTEGER DEFAULT 1,
                    data_json TEXT,
                    candidate_commit TEXT,
                    merge_commit TEXT,
                    review_findings_json TEXT,
                    test_evidence_json TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(task_id, seq)
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

    def cancel_task(self, task_id: str, reason: str = "") -> Optional[TaskRecord]:
        now = time.time()
        task = self.get_task(task_id)
        with self._connect() as conn:
            cur_bridge = conn.execute(
                """
                UPDATE bridge_tasks
                SET state = 'CANCELLED', error = ?, updated_at = ?
                WHERE task_id = ? AND state NOT IN ('DONE', 'CANCELLED')
                """,
                (reason or "Cancelled by operator", now, task_id),
            )
            conn.execute(
                """
                UPDATE bridge_user_questions
                SET status = 'CANCELLED', answered_at = ?
                WHERE task_id = ? AND status = 'PENDING'
                """,
                (now, task_id),
            )
            cur_task = conn.execute(
                """
                UPDATE tasks
                SET state = 'CANCELLED', updated_at = ?
                WHERE task_id = ? AND state NOT IN ('DONE', 'CANCELLED')
                """,
                (now, task_id),
            )
            if task and task.state not in (TaskState.DONE, TaskState.CANCELLED):
                conn.execute(
                    """
                    INSERT INTO task_transitions (
                        task_id, from_state, to_state, reason, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        task.state.value,
                        TaskState.CANCELLED.value,
                        reason,
                        None,
                        now,
                    ),
                )
        if cur_bridge.rowcount > 0 or cur_task.rowcount > 0 or task:
            updated = self.get_task(task_id)
            if updated:
                return updated
            return TaskRecord(task_id=task_id, state=TaskState.CANCELLED, goal="")
        return None

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

    # -------------------------------------------------------------
    # Milestone M1: Nonce management & single-use consumption
    # -------------------------------------------------------------
    def consume_approval_nonce(
        self,
        nonce: str,
        spec_id: str,
        repo: str,
        conversation_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> bool:
        """Atomically insert approval nonce into bridge_approval_nonces. Returns True if fresh, False if already consumed."""
        now = time.time()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO bridge_approval_nonces (
                        nonce, spec_id, repo, conversation_id, consumed_at, task_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (nonce, spec_id, repo, conversation_id, now, task_id),
                )
                return cur.rowcount > 0
        except sqlite3.IntegrityError:
            if task_id:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT task_id FROM bridge_approval_nonces WHERE nonce = ?", (nonce,)
                    ).fetchone()
                    if row and row["task_id"] == task_id:
                        return True
            return False

    def is_nonce_consumed(self, nonce: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT nonce FROM bridge_approval_nonces WHERE nonce = ?", (nonce,)
            ).fetchone()
            return row is not None

    # -------------------------------------------------------------
    # Milestone M3: Task Proposals storage & lifecycle
    # -------------------------------------------------------------
    def save_proposal(
        self,
        proposal_id: str,
        spec_id: str,
        repo: str,
        goal: str,
        spec_dict: Dict[str, Any],
        status: str = "PENDING_APPROVAL",
        source: str = "conversational",
        conversation_id: Optional[str] = None,
        message_id: Optional[str] = None,
        approval_token: Optional[str] = None,
        expires_at: Optional[float] = None,
    ) -> None:
        now = time.time()
        exp = expires_at or (now + 3600.0)
        spec_json = json.dumps(spec_dict)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bridge_task_proposals (
                    proposal_id, spec_id, repo, goal, spec_json, status,
                    source, conversation_id, message_id, approval_token,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    spec_id = excluded.spec_id,
                    repo = excluded.repo,
                    goal = excluded.goal,
                    spec_json = excluded.spec_json,
                    status = excluded.status,
                    approval_token = excluded.approval_token,
                    expires_at = excluded.expires_at
                """,
                (
                    proposal_id, spec_id, repo, goal, spec_json, status,
                    source, conversation_id, message_id, approval_token,
                    now, exp,
                ),
            )

    def get_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bridge_task_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "proposal_id": row["proposal_id"],
                "spec_id": row["spec_id"],
                "repo": row["repo"],
                "goal": row["goal"],
                "spec": json.loads(row["spec_json"]) if row["spec_json"] else {},
                "status": row["status"],
                "source": row["source"],
                "conversation_id": row["conversation_id"],
                "message_id": row["message_id"],
                "approval_token": row["approval_token"],
                "rejection_reason": row["rejection_reason"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "consumed_at": row["consumed_at"],
            }

    def list_proposals(
        self, status: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            query = "SELECT * FROM bridge_task_proposals"
            params: List[Any] = []
            if status:
                query += " WHERE status = ?"
                params.append(status)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            return [
                {
                    "proposal_id": r["proposal_id"],
                    "spec_id": r["spec_id"],
                    "repo": r["repo"],
                    "goal": r["goal"],
                    "spec": json.loads(r["spec_json"]) if r["spec_json"] else {},
                    "status": r["status"],
                    "source": r["source"],
                    "conversation_id": r["conversation_id"],
                    "message_id": r["message_id"],
                    "approval_token": r["approval_token"],
                    "rejection_reason": r["rejection_reason"],
                    "created_at": r["created_at"],
                    "expires_at": r["expires_at"],
                    "consumed_at": r["consumed_at"],
                }
                for r in rows
            ]

    def update_proposal_status(
        self,
        proposal_id: str,
        status: str,
        rejection_reason: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            consumed = now if status == "CONSUMED" else None
            conn.execute(
                """
                UPDATE bridge_task_proposals
                SET status = ?,
                    rejection_reason = COALESCE(?, rejection_reason),
                    approval_token = COALESCE(?, approval_token),
                    consumed_at = COALESCE(?, consumed_at)
                WHERE proposal_id = ?
                """,
                (status, rejection_reason, approval_token, consumed, proposal_id),
            )

    # -------------------------------------------------------------
    # Milestone M2: Ordered Task Event Outbox
    # -------------------------------------------------------------
    def emit_outbox_event(
        self,
        task_id: str,
        event_type: str,
        lifecycle_status: str,
        role: Optional[str] = None,
        seat_id: Optional[str] = None,
        summary: str = "",
        attempt: int = 1,
        data: Optional[Dict[str, Any]] = None,
        candidate_commit: Optional[str] = None,
        merge_commit: Optional[str] = None,
        review_findings: Optional[Dict[str, Any]] = None,
        test_evidence: Optional[Dict[str, Any]] = None,
        event_id: Optional[str] = None,
    ) -> int:
        """Append ordered event to bridge_task_outbox with monotonically increasing sequence number."""
        now = time.time()
        data_json = json.dumps(data or {})
        rf_json = json.dumps(review_findings) if review_findings else None
        te_json = json.dumps(test_evidence) if test_evidence else None

        with self._connect() as conn:
            # Query current max seq for this task
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM bridge_task_outbox WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            seq = int(row["max_seq"]) + 1
            evt_id = event_id or f"evt-{task_id}-{seq}-{int(now * 1000)}"

            try:
                conn.execute(
                    """
                    INSERT INTO bridge_task_outbox (
                        task_id, event_id, seq, event_type, lifecycle_status,
                        role, seat_id, summary, attempt, data_json,
                        candidate_commit, merge_commit, review_findings_json,
                        test_evidence_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id, evt_id, seq, event_type, lifecycle_status,
                        role, seat_id, summary, attempt, data_json,
                        candidate_commit, merge_commit, rf_json,
                        te_json, now,
                    ),
                )
                return seq
            except sqlite3.IntegrityError:
                # Duplicate event_id or sequence: fetch existing
                existing = conn.execute(
                    "SELECT seq FROM bridge_task_outbox WHERE event_id = ?", (evt_id,)
                ).fetchone()
                return int(existing["seq"]) if existing else seq

    def get_task_outbox_events(
        self,
        task_id: str,
        after_seq: int = 0,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Fetch ordered events for task_id starting after after_seq."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM bridge_task_outbox
                WHERE task_id = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (task_id, after_seq, limit),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "task_id": r["task_id"],
                    "event_id": r["event_id"],
                    "seq": r["seq"],
                    "event_type": r["event_type"],
                    "lifecycle_status": r["lifecycle_status"],
                    "role": r["role"],
                    "seat_id": r["seat_id"],
                    "summary": r["summary"],
                    "attempt": r["attempt"],
                    "data": json.loads(r["data_json"]) if r["data_json"] else {},
                    "candidate_commit": r["candidate_commit"],
                    "merge_commit": r["merge_commit"],
                    "review_findings": json.loads(r["review_findings_json"]) if r["review_findings_json"] else None,
                    "test_evidence": json.loads(r["test_evidence_json"]) if r["test_evidence_json"] else None,
                    "created_at": r["created_at"],
                }
                for r in rows
            ]


