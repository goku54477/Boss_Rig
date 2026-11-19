"""Durable transport-neutral task service for Boss Rig / OpenRig orchestrator."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from orchestrator.event_emitter import EventEmitter
from orchestrator.models import ApprovalRecord, TaskRecord, TaskSpec, TaskState, UserQuestion
from orchestrator.policy import PolicyEngine, PolicyViolation
from orchestrator.security import is_pid_alive, kill_process_tree, normalize_repo_path
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
        self._task_pids: Dict[str, List[int]] = {}
        self._lock = threading.Lock()

    def task_submit(
        self,
        spec: TaskSpec,
        idempotency_key: str,
        approved: bool = True,
        approved_hash: Optional[str] = None,
        conversation_id: Optional[str] = None,
        approval_record: Optional[ApprovalRecord | Dict[str, Any]] = None,
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

        if approval_record is None:
            raise ApprovalVerificationError(
                "Self-asserted approval rejected: explicit trusted approval record (from verified user chat event or operator signature) is required."
            )

        if isinstance(approval_record, dict):
            approval_record = ApprovalRecord(**approval_record)

        if approval_record.spec_id.lower() != computed_hash.lower():
            raise ApprovalVerificationError(
                f"Approval record hash '{approval_record.spec_id}' does not match computed spec hash '{computed_hash}'."
            )

        if normalize_repo_path(approval_record.repo) != normalize_repo_path(spec.repo):
            raise ApprovalVerificationError(
                f"Approval record repo '{approval_record.repo}' does not match spec repo '{spec.repo}'."
            )

        # Enforce expiration check if expires_at provided
        if approval_record.expires_at is not None and time.time() > approval_record.expires_at:
            raise ApprovalVerificationError(
                f"Approval record has expired (expired at {approval_record.expires_at})."
            )

        # Enforce permissions: execute_local only; forbid remote push / deployment
        allowed_actions = set(approval_record.allowed_actions or ["execute_local"])
        if "remote_push" in allowed_actions or "deploy_production" in allowed_actions:
            raise ApprovalVerificationError(
                "Approval grants forbidden permissions: remote push and production deployment are strictly disallowed."
            )

        # Enforce source validation & cryptographic token checks
        valid_sources = ("goldfish_chat", "operator_cli", "local_web")
        if approval_record.source not in valid_sources:
            raise ApprovalVerificationError(
                f"Unknown approval source '{approval_record.source}'. Must be one of {valid_sources}."
            )

        if approval_record.source == "goldfish_chat":
            if not approval_record.message_id or not self.store.is_event_claimed(approval_record.message_id):
                raise ApprovalVerificationError(
                    f"Approval message '{approval_record.message_id}' was not verified in chat transcripts."
                )
        elif approval_record.source in ("operator_cli", "local_web"):
            # Enforce cryptographic token verification using secret key
            from orchestrator.server import get_or_create_auth_token
            secret_key = get_or_create_auth_token()
            token = approval_record.approval_token or approval_record.signature
            if not token:
                raise ApprovalVerificationError(
                    f"Untrusted {approval_record.source} approval rejected: valid cryptographic approval_token or signature required."
                )
            from orchestrator.security import verify_approval_token
            is_valid = verify_approval_token(
                token=token,
                spec_id=computed_hash,
                repo=spec.repo,
                conversation_id=approval_record.conversation_id or "cli",
                message_id=approval_record.message_id or "cli",
                secret_key=secret_key,
                nonce=approval_record.nonce,
                expires_at=approval_record.expires_at,
                allowed_actions=approval_record.allowed_actions,
            )
            if not is_valid:
                raise ApprovalVerificationError(
                    "Cryptographic approval signature verification failed. Token does not match expected HMAC."
                )

        # Enforce single-use nonce consumption if nonce provided
        if approval_record.nonce:
            fresh = self.store.consume_approval_nonce(
                nonce=approval_record.nonce,
                spec_id=computed_hash,
                repo=spec.repo,
                conversation_id=approval_record.conversation_id,
                task_id=idempotency_key,
            )
            if not fresh:
                raise ApprovalVerificationError(
                    f"Approval nonce '{approval_record.nonce}' has already been consumed (replay attack prevented)."
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

        # 7. Record event & fire desktop toast & append to durable outbox
        self.store.emit_outbox_event(
            task_id=task_id,
            event_type="task.approved",
            lifecycle_status="QUEUED",
            summary=f"Task approved for {spec.repo}: {spec.goal[:60]}",
            data={"repo": spec.repo, "goal": spec.goal, "spec_id": computed_hash},
        )
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
        """Fetch terminal execution result, commits, and verification evidence."""
        task = self.task_status(task_id)
        state = task.get("state")
        is_finished = state in ("DONE", "REVIEW_FAILED", "FAILED", "CANCELLED", "BLOCKED")

        # Query core task record from tasks table for commits if available
        rec = self.store.get_task(task_id)
        candidate_commit = rec.candidate_commit if rec else None
        merge_commit = rec.merge_commit if rec else None

        # Fetch outbox events for evidence and reviewer findings
        outbox_events = self.store.get_task_outbox_events(task_id, limit=200)
        review_findings = None
        test_evidence = None
        failed_stage = None
        for ev in reversed(outbox_events):
            if not review_findings and ev.get("review_findings"):
                review_findings = ev.get("review_findings")
            if not test_evidence and ev.get("test_evidence"):
                test_evidence = ev.get("test_evidence")
            if not candidate_commit and ev.get("candidate_commit"):
                candidate_commit = ev.get("candidate_commit")
            if not merge_commit and ev.get("merge_commit"):
                merge_commit = ev.get("merge_commit")
            if not failed_stage and isinstance(ev.get("data"), dict):
                failed_stage = ev["data"].get("failed_stage")

        spec = task.get("spec", {})
        return {
            "task_id": task_id,
            "state": state,
            "finished": is_finished,
            "error": task.get("error"),
            "failed_stage": failed_stage,
            "repo": task.get("repo"),
            "allowed_merge_target": task.get("allowed_merge_target"),
            "spec_id": task.get("spec_id"),
            "goal": task.get("goal"),
            "candidate_commit": candidate_commit,
            "merge_commit": merge_commit,
            "verification_commands": spec.get("verification_commands", []),
            "assigned_agents": spec.get("assigned_agents", {}),
            "review_findings": review_findings,
            "test_evidence": test_evidence,
        }


    def get_task_outbox_events(
        self, task_id: str, after_seq: int = 0, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Fetch ordered timeline outbox events for task_id."""
        return self.store.get_task_outbox_events(task_id, after_seq=after_seq, limit=limit)

    def task_list(self, limit: int = 50, state: Optional[str] = None) -> List[Dict[str, Any]]:
        """List tasks with optional state filtering."""
        return self.store.list_bridge_tasks(limit=limit, state=state)

    def register_task_pid(self, task_id: str, pid: int) -> None:
        """Register child process PID associated with task for clean process-tree cancellation."""
        with self._lock:
            if task_id not in self._task_pids:
                self._task_pids[task_id] = []
            if pid not in self._task_pids[task_id]:
                self._task_pids[task_id].append(pid)

    def get_task_pids(self, task_id: str) -> List[int]:
        with self._lock:
            return list(self._task_pids.get(task_id, []))

    def task_cancel(self, task_id: str, reason: str = "") -> bool:
        """Cancel an in-flight or queued task safely and forcefully terminate its process tree."""
        with self._lock:
            self._cancellation_flags[task_id] = True
            pids = list(self._task_pids.get(task_id, []))
            # Wake up any thread waiting on a question
            if task_id in self._worker_events:
                self._worker_events[task_id].set()

        # Forcefully terminate any active child process trees
        for pid in pids:
            kill_process_tree(pid)

        cancelled = self.store.cancel_task(task_id, reason=reason)
        if cancelled:
            self.store.emit_outbox_event(
                task_id=task_id,
                event_type="task.cancelled",
                lifecycle_status="CANCELLED",
                summary=f"Task {task_id} was cancelled: {reason}",
                data={"reason": reason, "killed_pids": pids},
            )
            self.emitter.emit(
                "TASK_CANCELLED",
                task_id=task_id,
                data={"reason": reason, "killed_pids": pids},
                notification_title="[Boss Rig] Task Cancelled",
                notification_body=f"Task {task_id} was cancelled: {reason}",
            )
        return cancelled

    def get_task_events(
        self, task_id: str, after_seq: int = 0, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Retrieve ordered outbox events for a task since after_seq."""
        return self.store.get_task_outbox_events(task_id, after_seq=after_seq, limit=limit)

    def propose_task(
        self,
        spec: TaskSpec,
        source: str = "conversational",
        conversation_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a pending, unapproved TaskProposal without executing it."""
        # Validate repository & merge target against policy
        PolicyEngine.assert_allowed_repository(spec.repo, extra_allowed=self.extra_allowed_repos)
        PolicyEngine.assert_allowed_merge_target(spec.allowed_merge_target)

        spec_hash = spec.compute_hash()
        now = time.time()
        proposal_id = f"PROP-{int(now * 1000)}"

        self.store.save_proposal(
            proposal_id=proposal_id,
            spec_id=spec_hash,
            repo=spec.repo,
            goal=spec.goal,
            spec_dict=spec.to_canonical_dict(),
            status="PENDING_APPROVAL",
            source=source,
            conversation_id=conversation_id,
            message_id=message_id,
        )

        return {
            "proposal_id": proposal_id,
            "spec_id": spec_hash,
            "repo": spec.repo,
            "goal": spec.goal,
            "status": "PENDING_APPROVAL",
            "spec": spec.to_canonical_dict(),
            "message": "Task proposal created. Requires explicit authenticated human approval before dispatch.",
        }

    def get_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        """Fetch pending or processed task proposal."""
        return self.store.get_proposal(proposal_id)

    def list_proposals(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List proposals filtered by status."""
        return self.store.list_proposals(status=status)

    def reject_proposal(self, proposal_id: str, reason: str = "") -> bool:
        """Reject a pending proposal."""
        prop = self.store.get_proposal(proposal_id)
        if not prop:
            raise TaskNotFoundError(f"Proposal '{proposal_id}' not found.")
        self.store.update_proposal_status(proposal_id, status="REJECTED", rejection_reason=reason)
        return True


    def task_questions(
        self, task_id: Optional[str] = None, status: Optional[str] = "PENDING"
    ) -> List[Dict[str, Any]]:
        """List pending or answered questions."""
        return self.store.list_questions(task_id=task_id, status=status)

    def task_answer(
        self,
        question_id: str,
        answer: str,
        task_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Provide an answer to a pending user question with strict task and conversation binding."""
        q = self.store.get_question(question_id)
        if not q:
            raise BridgeError(f"Question '{question_id}' not found.")

        if q["status"] != "PENDING":
            raise BridgeError(f"Question '{question_id}' is already {q['status']}.")

        # Invariant: Verify task_id matches question record if provided
        if task_id and str(task_id).strip() != str(q.get("task_id", "")).strip():
            raise BridgeError(
                f"Question '{question_id}' belongs to task '{q.get('task_id')}', but was answered for task '{task_id}' (cross-task interference rejected)."
            )

        # Invariant: Verify conversation_id matches question record if provided
        if conversation_id and q.get("conversation_id"):
            if str(conversation_id).strip() != str(q.get("conversation_id", "")).strip():
                raise BridgeError(
                    f"Question '{question_id}' originated in conversation '{q.get('conversation_id')}', but answer was submitted from conversation '{conversation_id}' (cross-conversation interference rejected)."
                )

        # Guard against scope expansion attacks in answer text
        lower = answer.lower()
        if "repo=" in lower or "merge to main" in lower or "allowed_merge_target=main" in lower:
            raise ScopeExpansionError(
                "Answers cannot modify repository or merge target. Submit a revised spec and obtain new approval."
            )

        updated = self.store.answer_question(question_id, answer)
        q_task_id = q["task_id"]

        self.emitter.emit(
            "QUESTION_ANSWERED",
            task_id=q_task_id,
            data={"question_id": question_id, "answer": answer},
        )

        # Signal the waiting worker thread to wake up
        with self._lock:
            if q_task_id in self._worker_events:
                self._worker_events[q_task_id].set()

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
        while time.time() < deadline:
            if self._cancellation_flags.get(task_id):
                raise BridgeError(f"Task {task_id} was cancelled while awaiting answer.")

            q_rec = self.store.get_question(qid)
            if q_rec and q_rec["status"] == "ANSWERED":
                self.store.update_bridge_task_state(task_id, TaskState.ACTIVE.value)
                return q_rec["answer_text"] or ""
            elif q_rec and q_rec["status"] == "CANCELLED":
                raise BridgeError(f"Question {qid} was cancelled.")

            wait_event.wait(timeout=0.5)

        # Timeout expired
        self.store.update_bridge_task_state(
            task_id, TaskState.BLOCKED.value, error=f"Timed out awaiting user answer to {qid}"
        )
        raise BridgeError(f"Question {qid} timed out after {timeout_sec}s awaiting user answer.")

    def _record_stage_failure(
        self,
        task_id: str,
        spec_hash: str,
        stage: str,
        fail_state: str,
        event_type: str,
        summary: str,
        error_msg: str,
        role: Optional[str] = None,
        seat_id: Optional[str] = None,
        candidate_commit: Optional[str] = None,
        review_findings: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist failure state, outbox event with failed_stage, and OS notification."""
        self.store.update_bridge_task_state(task_id, fail_state, error=error_msg)
        self.store.emit_outbox_event(
            task_id=task_id,
            event_type=event_type,
            lifecycle_status=fail_state,
            role=role,
            seat_id=seat_id,
            summary=summary,
            candidate_commit=candidate_commit,
            review_findings=review_findings,
            data={"state": fail_state, "failed_stage": stage, "error": error_msg},
        )
        self.emitter.emit(
            "TASK_FAILED",
            task_id=task_id,
            spec_id=spec_hash,
            data={"state": fail_state, "failed_stage": stage, "error": error_msg},
            notification_title=f"[Boss Rig] Task {fail_state} ({stage})",
            notification_body=f"Task {task_id} failed at {stage}: {error_msg[:60]}",
        )

    def _run_task_worker(self, task_id: str, spec: TaskSpec, spec_hash: str) -> None:
        """Background thread executing the task through the orchestration pipeline."""
        stage = "dispatch"
        try:
            self.store.update_bridge_task_state(task_id, TaskState.ACTIVE.value)
            self.store.emit_outbox_event(
                task_id=task_id,
                event_type="task.started",
                lifecycle_status="ACTIVE",
                summary=f"Task {task_id} execution started",
                data={"spec_id": spec_hash, "repo": spec.repo},
            )

            # Check if custom engine factory provided
            if self.engine_factory:
                engine = self.engine_factory()
            else:
                from orchestrator.engine import OrchestrationEngine
                engine = OrchestrationEngine(target_repo=spec.repo)

            # Execute via engine if available
            if hasattr(engine, "execute_bridge_task"):
                stage = "bridge_execute"
                result = engine.execute_bridge_task(
                    task_id=task_id,
                    spec=spec,
                    ask_user_fn=lambda p: self.ask_user_question(task_id, p),
                    cancel_check_fn=lambda: self._cancellation_flags.get(task_id, False),
                )
                success = result.get("success", False)
                state = TaskState.DONE.value if success else TaskState.REVIEW_FAILED.value
                error = result.get("error")
                cand_commit = result.get("candidate_commit")
                mrg_commit = result.get("merge_commit")
                if result.get("review_findings"):
                    self.store.emit_outbox_event(
                        task_id=task_id,
                        event_type="review.completed" if success else "review.failed",
                        lifecycle_status="MERGE_READY" if success else "REVIEW_FAILED",
                        role="reviewer",
                        summary="Review completed",
                        candidate_commit=cand_commit,
                        review_findings={"verdict": "PASS" if success else "FAIL", "findings": result.get("review_findings")},
                    )
                if result.get("test_evidence"):
                    self.store.emit_outbox_event(
                        task_id=task_id,
                        event_type="verification.passed" if success else "verification.failed",
                        lifecycle_status="DONE" if success else "REVIEW_FAILED",
                        summary="Verification test execution completed",
                        candidate_commit=cand_commit,
                        merge_commit=mrg_commit,
                        test_evidence={"status": "PASSED" if success else "FAILED", "evidence": result.get("test_evidence")},
                    )
            else:
                # Fallback standard OrchestrationEngine flow
                stage = "dispatch"
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
                stage = "planner"
                self.store.emit_outbox_event(
                    task_id=task_id,
                    event_type="planner.started",
                    lifecycle_status="ACTIVE",
                    role="planner",
                    seat_id=planner,
                    summary=f"Planner turn started by {planner}",
                )
                engine.assign_role(task_id, engine=planner, role="planner")
                p_res = engine.execute_turn(
                    task_id,
                    engine=planner,
                    role="planner",
                    prompt=spec.goal,
                    timeout_sec=spec.timeout_sec,
                )
                if p_res.get("status") != "SUCCESS":
                    err_msg = str(p_res.get("error") or "Planning failed")
                    self._record_stage_failure(
                        task_id=task_id,
                        spec_hash=spec_hash,
                        stage="planner",
                        fail_state=TaskState.FAILED.value,
                        event_type="planner.failed",
                        summary=f"Planning turn failed on {planner}: {err_msg}",
                        error_msg=err_msg,
                        role="planner",
                        seat_id=planner,
                    )
                    return
                self.store.emit_outbox_event(
                    task_id=task_id,
                    event_type="planner.completed",
                    lifecycle_status="ACTIVE",
                    role="planner",
                    seat_id=planner,
                    summary=f"Planner turn completed by {planner}",
                )

                # Turn 2: Implementing
                stage = "implementer"
                self.store.emit_outbox_event(
                    task_id=task_id,
                    event_type="implementation.started",
                    lifecycle_status="ACTIVE",
                    role="implementer",
                    seat_id=implementer,
                    summary=f"Implementation turn started by {implementer}",
                )
                engine.assign_role(task_id, engine=implementer, role="implementer")
                i_res = engine.execute_turn(
                    task_id,
                    engine=implementer,
                    role="implementer",
                    prompt=spec.goal,
                    timeout_sec=spec.timeout_sec,
                )
                if i_res.get("status") != "SUCCESS":
                    err_msg = str(i_res.get("error") or "Implementation failed")
                    self._record_stage_failure(
                        task_id=task_id,
                        spec_hash=spec_hash,
                        stage="implementer",
                        fail_state=TaskState.FAILED.value,
                        event_type="implementation.failed",
                        summary=f"Implementation turn failed on {implementer}: {err_msg}",
                        error_msg=err_msg,
                        role="implementer",
                        seat_id=implementer,
                    )
                    return

                core_rec = engine.tasks.get(task_id)
                cand_commit = core_rec.candidate_commit if core_rec else None
                self.store.emit_outbox_event(
                    task_id=task_id,
                    event_type="implementation.completed",
                    lifecycle_status="REVIEW",
                    role="implementer",
                    seat_id=implementer,
                    summary=f"Implementation completed by {implementer}; candidate commit pinned: {cand_commit}",
                    candidate_commit=cand_commit,
                )

                # Turn 3: Review
                stage = "reviewer"
                self.store.emit_outbox_event(
                    task_id=task_id,
                    event_type="review.started",
                    lifecycle_status="REVIEW",
                    role="reviewer",
                    seat_id=reviewer,
                    summary=f"Review started by {reviewer} on candidate {cand_commit}",
                    candidate_commit=cand_commit,
                )
                engine.assign_role(task_id, engine=reviewer, role="reviewer")
                r_res = engine.execute_turn(
                    task_id,
                    engine=reviewer,
                    role="reviewer",
                    prompt=spec.goal,
                    timeout_sec=spec.timeout_sec,
                )
                if r_res.get("status") != "SUCCESS":
                    err_msg = str(r_res.get("error") or "Review rejected")
                    self._record_stage_failure(
                        task_id=task_id,
                        spec_hash=spec_hash,
                        stage="reviewer",
                        fail_state=TaskState.REVIEW_FAILED.value,
                        event_type="review.failed",
                        summary="Review turn rejected candidate commit",
                        error_msg=err_msg,
                        role="reviewer",
                        seat_id=reviewer,
                        candidate_commit=cand_commit,
                        review_findings={"verdict": "FAIL", "error": err_msg},
                    )
                    return
                self.store.emit_outbox_event(
                    task_id=task_id,
                    event_type="review.completed",
                    lifecycle_status="MERGE_READY",
                    role="reviewer",
                    seat_id=reviewer,
                    summary=f"Review verified by {reviewer}",
                    candidate_commit=cand_commit,
                    review_findings={"verdict": "PASS"},
                )

                # Verification gate
                stage = "merge_gate"
                self.store.emit_outbox_event(
                    task_id=task_id,
                    event_type="verification.started",
                    lifecycle_status="MERGING",
                    summary="Deterministic verification gate running",
                    candidate_commit=cand_commit,
                )
                v_ok = engine.run_deterministic_verification(task_id)
                state = TaskState.DONE.value if v_ok else TaskState.REVIEW_FAILED.value
                error = None if v_ok else "Deterministic verification gate failed."
                mrg_commit = core_rec.merge_commit if core_rec else None

                if v_ok:
                    self.store.emit_outbox_event(
                        task_id=task_id,
                        event_type="verification.passed",
                        lifecycle_status="DONE",
                        summary=f"Verification passed; merged to integration: {mrg_commit}",
                        candidate_commit=cand_commit,
                        merge_commit=mrg_commit,
                        test_evidence={"exit_code": 0, "status": "PASSED"},
                    )
                else:
                    self.store.emit_outbox_event(
                        task_id=task_id,
                        event_type="verification.failed",
                        lifecycle_status="REVIEW_FAILED",
                        summary="Deterministic verification gate failed",
                        candidate_commit=cand_commit,
                        data={"failed_stage": "merge_gate"},
                        test_evidence={"exit_code": 1, "status": "FAILED"},
                    )

            if self._cancellation_flags.get(task_id):
                return

            self.store.update_bridge_task_state(task_id, state, error=error)
            extra_data: Dict[str, Any] = {"state": state, "error": error}
            if state != TaskState.DONE.value:
                extra_data["failed_stage"] = stage
            self.store.emit_outbox_event(
                task_id=task_id,
                event_type="task.done" if state == TaskState.DONE.value else "task.failed",
                lifecycle_status=state,
                summary=f"Task {task_id} completed with status {state}",
                candidate_commit=cand_commit,
                merge_commit=mrg_commit,
                data=extra_data,
            )
            self.emitter.emit(
                "TASK_COMPLETED" if state == TaskState.DONE.value else "TASK_FAILED",
                task_id=task_id,
                spec_id=spec_hash,
                data=extra_data,
                notification_title=f"[Boss Rig] Task {state}",
                notification_body=f"Task {task_id} finished with state {state}.",
            )

        except Exception as exc:
            if self._cancellation_flags.get(task_id):
                return
            fail_state = (
                TaskState.REVIEW_FAILED.value
                if stage == "bridge_execute"
                else TaskState.FAILED.value
            )
            self._record_stage_failure(
                task_id=task_id,
                spec_hash=spec_hash,
                stage=stage,
                fail_state=fail_state,
                event_type="task.failed",
                summary=f"Task {task_id} failed at stage '{stage}': {exc}",
                error_msg=str(exc),
            )
        finally:
            with self._lock:
                self._active_workers.pop(task_id, None)
                self._worker_events.pop(task_id, None)
                self._cancellation_flags.pop(task_id, None)
                self._task_pids.pop(task_id, None)

    def recover_inflight_tasks(self) -> List[str]:
        """On orchestrator startup, safely mark orphaned in-flight tasks from previous run as BLOCKED."""
        orphaned = []
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, state FROM bridge_tasks WHERE state IN ('ACTIVE', 'QUEUED')"
            ).fetchall()
            for r in rows:
                tid = r["task_id"]
                if tid not in self._active_workers:
                    self.store.update_bridge_task_state(
                        tid,
                        TaskState.BLOCKED.value,
                        error="Orchestrator restarted while task was in-flight; requiring operator verification",
                    )
                    orphaned.append(tid)
        return orphaned

    def wait_all(self, timeout_sec: float = 5.0) -> None:
        """Wait for all active background workers to finish (useful in tests and shutdown)."""
        deadline = time.time() + timeout_sec
        with self._lock:
            threads = list(self._active_workers.values())
        for th in threads:
            rem = max(0.01, deadline - time.time())
            th.join(timeout=rem)

