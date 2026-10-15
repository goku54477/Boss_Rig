"""Durable transport-neutral task service for Boss Rig / OpenRig orchestrator."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from orchestrator.event_emitter import EventEmitter
from orchestrator.models import TaskRecord, TaskSpec, TaskState, UserQuestion
from orchestrator.policy import PolicyEngine, PolicyViolation
from orchestrator.store import TaskStore


class BridgeError(Exception):
    """Base class for bridge service errors."""


class ApprovalVerificationError(BridgeError):
    """Raised when task approval hash does not match canonical spec hash."""


class ScopeExpansionError(BridgeError):
    """Raised when an answer or input attempts to expand the approved task scope."""


class TaskNotFoundError(BridgeError):
    """Raised when task_id does not exist."""


class TaskService:
    """Manages the lifecycle of tasks submitted via bridge, API, or watcher."""

    def __init__(
        self,
        store: Optional[TaskStore] = None,
        event_emitter: Optional[EventEmitter] = None,
        engine_factory: Optional[Callable[[], Any]] = None,
        extra_allowed_repos: Optional[List[Path | str]] = None,
        dry_run: bool = False,
    ):
        self.store = store or TaskStore()
        self.emitter = event_emitter or EventEmitter()
        self.engine_factory = engine_factory
        self.extra_allowed_repos = extra_allowed_repos or []
        self.dry_run = dry_run

        self._active_workers: Dict[str, threading.Thread] = {}
        self._worker_events: Dict[str, threading.Event] = {}  # for question wait/wake
        self._cancellation_flags: Dict[str, bool] = {}
        self._lock = threading.Lock()

    def task_submit(
        self,
        spec: TaskSpec,
        idempotency_key: str,
        approved: bool = True,
        approved_hash: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Submit a task for asynchronous execution with strict approval validation."""
        # 1. Enforce repository allowlist
        PolicyEngine.assert_allowed_repository(spec.repo, extra_allowed=self.extra_allowed_repos)

        # 2. Enforce integration-only merge target
        PolicyEngine.assert_allowed_merge_target(spec.allowed_merge_target)

        # 3. Compute canonical SHA-256 spec hash
        computed_hash = spec.compute_hash()

        # 4. Enforce trusted approval binding
        if not approved:
            raise ApprovalVerificationError(
                "Task submission rejected: 'approved' must be True to authorize execution."
            )

        if approved_hash and approved_hash != computed_hash:
            raise ApprovalVerificationError(
                f"Approval hash mismatch: approved '{approved_hash}' does not match computed spec hash '{computed_hash}'."
            )

        # 5. Check dry-run mode
        if self.dry_run:
            self.emitter.emit(
                "TASK_DRY_RUN",
                task_id="T-DRY-RUN",
                spec_id=computed_hash,
                data={"repo": spec.repo, "goal": spec.goal, "would_submit": True},
            )
            return {
                "task_id": "T-DRY-RUN",
                "spec_id": computed_hash,
                "status": "DRY_RUN",
                "message": "Task validated in dry-run mode. Zero agents launched.",
                "is_new": True,
            }

        # 6. Durable idempotency insertion into SQLite
        task_id, is_new = self.store.submit_bridge_task(spec, idempotency_key=idempotency_key)

        if not is_new:
            # Task already exists from prior submission
            existing = self.store.get_bridge_task(task_id)
            return {
                "task_id": task_id,
                "spec_id": computed_hash,
                "status": existing["state"] if existing else "QUEUED",
                "is_new": False,
            }

        # 7. Record event & fire desktop toast
        self.emitter.emit(
            "TASK_SUBMITTED",
            task_id=task_id,
            spec_id=computed_hash,
            data={"repo": spec.repo, "goal": spec.goal, "idempotency_key": idempotency_key},
            notification_title="[Boss Rig] Task Submitted",
            notification_body=f"Task {task_id} approved for {spec.repo}: {spec.goal[:60]}",
        )

        # 8. Launch asynchronous background worker
        worker = threading.Thread(
            target=self._run_task_worker,
            args=(task_id, spec, computed_hash),
            daemon=True,
            name=f"worker-{task_id}",
        )
        with self._lock:
            self._active_workers[task_id] = worker
            self._cancellation_flags[task_id] = False
        worker.start()

        return {
            "task_id": task_id,
            "spec_id": computed_hash,
            "status": "QUEUED",
            "is_new": True,
        }

    def task_status(self, task_id: str) -> Dict[str, Any]:
        """Query current execution state and metadata."""
        task = self.store.get_bridge_task(task_id)
        if not task:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")

        # Check for any pending questions
        questions = self.store.list_questions(task_id=task_id)
        task["questions"] = questions
        return task

    def task_result(self, task_id: str) -> Dict[str, Any]:
        """Fetch terminal execution result and merge commit."""
        task = self.task_status(task_id)
        state = task.get("state")
        is_finished = state in ("DONE", "REVIEW_FAILED", "CANCELLED", "BLOCKED")
        return {
            "task_id": task_id,
            "state": state,
            "finished": is_finished,
            "error": task.get("error"),
            "allowed_merge_target": task.get("allowed_merge_target"),
            "spec_id": task.get("spec_id"),
        }

    def task_list(self, limit: int = 50, state: Optional[str] = None) -> List[Dict[str, Any]]:
        """List tasks with optional state filtering."""
        return self.store.list_bridge_tasks(limit=limit, state=state)

    def task_cancel(self, task_id: str, reason: str = "") -> bool:
        """Cancel an in-flight or queued task safely."""
        with self._lock:
            self._cancellation_flags[task_id] = True
            # Wake up any thread waiting on a question
            if task_id in self._worker_events:
                self._worker_events[task_id].set()

        cancelled = self.store.cancel_task(task_id, reason=reason)
        if cancelled:
            self.emitter.emit(
                "TASK_CANCELLED",
                task_id=task_id,
                data={"reason": reason},
                notification_title="[Boss Rig] Task Cancelled",
                notification_body=f"Task {task_id} was cancelled: {reason}",
            )
        return cancelled

    def task_questions(
        self, task_id: Optional[str] = None, status: Optional[str] = "PENDING"
    ) -> List[Dict[str, Any]]:
        """List pending or answered questions."""
        return self.store.list_questions(task_id=task_id, status=status)

    def task_answer(self, question_id: str, answer: str) -> Dict[str, Any]:
        """Provide an answer to a pending user question."""
        q = self.store.get_question(question_id)
        if not q:
            raise BridgeError(f"Question '{question_id}' not found.")

        if q["status"] != "PENDING":
            raise BridgeError(f"Question '{question_id}' is already {q['status']}.")

        # Guard against scope expansion attacks in answer text
        lower = answer.lower()
        if "repo=" in lower or "merge to main" in lower or "allowed_merge_target=main" in lower:
            raise ScopeExpansionError(
                "Answers cannot modify repository or merge target. Submit a revised spec and obtain new approval."
            )

        updated = self.store.answer_question(question_id, answer)
        task_id = q["task_id"]

        self.emitter.emit(
            "QUESTION_ANSWERED",
            task_id=task_id,
            data={"question_id": question_id, "answer": answer},
        )

        # Signal the waiting worker thread to wake up
        with self._lock:
            if task_id in self._worker_events:
                self._worker_events[task_id].set()

        return updated or {}

    def ask_user_question(
        self,
        task_id: str,
        prompt: str,
        conversation_id: Optional[str] = None,
        timeout_sec: float = 300.0,
    ) -> str:
        """Called by engine/seats to pause execution and wait for user answer."""
        now = time.time()
        qid = f"Q-{int(now * 1000)}"

        # 1. Transition task state to WAITING_FOR_USER
        self.store.update_bridge_task_state(task_id, TaskState.WAITING_FOR_USER.value)

        # 2. Record question in SQLite
        self.store.create_question(qid, task_id=task_id, prompt=prompt, conversation_id=conversation_id)

        # 3. Emit notification toast & event
        self.emitter.emit(
            "QUESTION_ASKED",
            task_id=task_id,
            data={"question_id": qid, "prompt": prompt, "conversation_id": conversation_id},
            notification_title="[Boss Rig Input Required]",
            notification_body=f"Task {task_id} ({qid}): {prompt[:80]}",
        )

        # 4. Wait for answer or timeout or cancellation
        wait_event = threading.Event()
        with self._lock:
            self._worker_events[task_id] = wait_event

        deadline = time.time() + timeout_sec
        answered = False
        while time.time() < deadline:
            if self._cancellation_flags.get(task_id):
                raise BridgeError(f"Task {task_id} was cancelled while awaiting answer.")

            if wait_event.wait(timeout=1.0):
                # Check if answered
                q_rec = self.store.get_question(qid)
                if q_rec and q_rec["status"] == "ANSWERED":
                    # Transition back to ACTIVE
                    self.store.update_bridge_task_state(task_id, TaskState.ACTIVE.value)
                    return q_rec["answer_text"] or ""

        # Timeout expired
        self.store.update_bridge_task_state(
            task_id, TaskState.BLOCKED.value, error=f"Timed out awaiting user answer to {qid}"
        )
        raise BridgeError(f"Question {qid} timed out after {timeout_sec}s awaiting user answer.")

    def _run_task_worker(self, task_id: str, spec: TaskSpec, spec_hash: str) -> None:
        """Background thread executing the task through the orchestration pipeline."""
        try:
            self.store.update_bridge_task_state(task_id, TaskState.ACTIVE.value)

            # Check if custom engine factory provided
            if self.engine_factory:
                engine = self.engine_factory()
            else:
                from orchestrator.engine import OrchestrationEngine
                engine = OrchestrationEngine()

            # Execute via engine if available
            if hasattr(engine, "execute_bridge_task"):
                result = engine.execute_bridge_task(
                    task_id=task_id,
                    spec=spec,
                    ask_user_fn=lambda p: self.ask_user_question(task_id, p),
                    cancel_check_fn=lambda: self._cancellation_flags.get(task_id, False),
                )
                success = result.get("success", False)
                state = TaskState.DONE.value if success else TaskState.REVIEW_FAILED.value
                error = result.get("error")
            else:
                # Fallback standard OrchestrationEngine flow
                record = engine.create_task(
                    task_id=task_id,
                    goal=spec.goal,
                    verification_commands=spec.verification_commands,
                )
                agents = spec.assigned_agents
                planner = agents.get("planner", "antigravity-1")
                implementer = agents.get("implementer", "opencode-1")
                reviewer = agents.get("reviewer", "antigravity-1")

                # Invariant: Reviewer cannot be implementer
                PolicyEngine.validate_engine_and_role(reviewer, "reviewer")
                if reviewer == implementer:
                    raise PolicyViolation("Reviewer cannot be implementer.")

                # Turn 1: Planning
                engine.assign_role(task_id, engine=planner, role="planner")
                p_res = engine.execute_turn(task_id, engine=planner, role="planner", prompt=spec.goal)
                if p_res.get("status") != "SUCCESS":
                    self.store.update_bridge_task_state(task_id, TaskState.REVIEW_FAILED.value, error="Planning failed")
                    return

                # Turn 2: Implementing
                engine.assign_role(task_id, engine=implementer, role="implementer")
                i_res = engine.execute_turn(task_id, engine=implementer, role="implementer", prompt=spec.goal)
                if i_res.get("status") != "SUCCESS":
                    self.store.update_bridge_task_state(task_id, TaskState.REVIEW_FAILED.value, error="Implementation failed")
                    return

                # Turn 3: Review
                engine.assign_role(task_id, engine=reviewer, role="reviewer")
                r_res = engine.execute_turn(task_id, engine=reviewer, role="reviewer", prompt=spec.goal)
                if r_res.get("status") != "SUCCESS":
                    self.store.update_bridge_task_state(task_id, TaskState.REVIEW_FAILED.value, error="Review failed")
                    return

                # Verification gate
                v_ok = engine.run_deterministic_verification(task_id)
                state = TaskState.DONE.value if v_ok else TaskState.REVIEW_FAILED.value
                error = None if v_ok else "Deterministic verification gate failed."

            if self._cancellation_flags.get(task_id):
                return

            self.store.update_bridge_task_state(task_id, state, error=error)
            self.emitter.emit(
                "TASK_COMPLETED" if state == TaskState.DONE.value else "TASK_FAILED",
                task_id=task_id,
                spec_id=spec_hash,
                data={"state": state, "error": error},
                notification_title=f"[Boss Rig] Task {state}",
                notification_body=f"Task {task_id} finished with state {state}.",
            )

        except Exception as exc:
            if self._cancellation_flags.get(task_id):
                return
            self.store.update_bridge_task_state(task_id, TaskState.REVIEW_FAILED.value, error=str(exc))
            self.emitter.emit(
                "TASK_FAILED",
                task_id=task_id,
                spec_id=spec_hash,
                data={"error": str(exc)},
                notification_title="[Boss Rig] Task Error",
                notification_body=f"Task {task_id} error: {str(exc)[:60]}",
            )
        finally:
            with self._lock:
                self._active_workers.pop(task_id, None)
                self._worker_events.pop(task_id, None)
                self._cancellation_flags.pop(task_id, None)

    def wait_all(self, timeout_sec: float = 5.0) -> None:
        """Wait for all active background workers to finish (useful in tests and shutdown)."""
        deadline = time.time() + timeout_sec
        with self._lock:
            threads = list(self._active_workers.values())
        for th in threads:
            rem = max(0.01, deadline - time.time())
            th.join(timeout=rem)

