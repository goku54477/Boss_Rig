"""Comprehensive 12-scenario acceptance tests for Boss Rig Conversational Bridge (Milestones M1-M4)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from orchestrator.chat_watcher import ChatWatcher
from orchestrator.event_emitter import EventEmitter
from orchestrator.models import ApprovalRecord, TaskProposal, TaskSpec
from orchestrator.security import create_approval_token
from orchestrator.server import get_or_create_auth_token
from orchestrator.store import TaskStore
from orchestrator.task_service import ApprovalVerificationError, BridgeError, TaskService


class MockAcceptanceEngine:
    """Configurable mock engine for offline acceptance testing."""

    def __init__(
        self,
        fail_review: bool = False,
        fail_verification: bool = False,
        trigger_question: Optional[str] = None,
        slow_delay: float = 0.0,
        store: Optional[TaskStore] = None,
    ):
        self.fail_review = fail_review
        self.fail_verification = fail_verification
        self.trigger_question = trigger_question
        self.slow_delay = slow_delay
        self.store = store
        self.invoked_engines: List[str] = []
        self.invoked_roles: List[str] = []
        self.received_answer: Optional[str] = None
        self.candidate_commit: Optional[str] = None
        self.merge_commit: Optional[str] = None

    def execute_bridge_task(
        self,
        task_id: str,
        spec: TaskSpec,
        ask_user_fn: Any,
        cancel_check_fn: Any,
    ) -> Dict[str, Any]:
        repo_dir = Path(spec.repo)
        planner = spec.assigned_agents.get("planner", "antigravity-1")
        implementer = spec.assigned_agents.get("implementer", "opencode-1")
        reviewer = spec.assigned_agents.get("reviewer", "antigravity-1")

        # 1. Planner
        self.invoked_engines.append(planner)
        self.invoked_roles.append("planner")
        if self.store:
            self.store.emit_outbox_event(task_id, "planner.completed", "ACTIVE", role="planner", seat_id=planner)

        # Optional Question Turn
        if self.trigger_question:
            self.received_answer = ask_user_fn(self.trigger_question)

        if self.slow_delay > 0:
            start = time.time()
            while time.time() - start < self.slow_delay:
                if cancel_check_fn and cancel_check_fn():
                    return {"success": False, "error": "Task cancelled"}
                time.sleep(0.05)

        # 2. Implementer
        self.invoked_engines.append(implementer)
        self.invoked_roles.append("implementer")

        feature_file = repo_dir / "app.py"
        feature_file.write_text("def hello(): return 'world'\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"Implement {task_id}"], cwd=repo_dir, check=True, capture_output=True)
        cand_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
        self.candidate_commit = cand_sha
        if self.store:
            self.store.emit_outbox_event(task_id, "implementation.completed", "ACTIVE", role="implementer", seat_id=implementer, candidate_commit=cand_sha)

        # Review turn
        self.invoked_engines.append(reviewer)
        self.invoked_roles.append("reviewer")

        if self.fail_review:
            return {
                "success": False,
                "error": "Independent review rejected candidate changes: security vulnerability detected.",
                "candidate_commit": cand_sha,
                "review_findings": "Rejected: insecure pattern detected by reviewer.",
            }

        # Verification check
        if self.fail_verification:
            return {
                "success": False,
                "error": "Deterministic verification commands failed.",
                "candidate_commit": cand_sha,
                "test_evidence": "Verification failed: assert hello() == 'world' returned False.",
            }

        # Merge gate simulation
        subprocess.run(["git", "checkout", spec.allowed_merge_target], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "merge", "--no-ff", "-m", f"Merge {task_id}", cand_sha],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        merge_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
        self.merge_commit = merge_sha

        return {
            "success": True,
            "candidate_commit": cand_sha,
            "merge_commit": merge_sha,
            "review_findings": "Approved by independent reviewer.",
            "test_evidence": "All 1 verification commands passed.",
        }


class TestConversationalBridgeOfflineAcceptance(unittest.TestCase):
    """The 12 offline acceptance scenarios required for Milestone 1-4 sign-off."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="bridge_acc_")
        self.repo_dir = Path(self.test_dir) / "repo"
        self.chats_dir = Path(self.test_dir) / "chats"
        self.events_file = Path(self.test_dir) / "events.jsonl"
        self.db_path = Path(self.test_dir) / "tasks.sqlite"

        self.repo_dir.mkdir(parents=True)
        self.chats_dir.mkdir(parents=True)

        # Initialize test git repo with main branch
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test Boss"], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "boss@example.com"], cwd=self.repo_dir, check=True)

        readme = self.repo_dir / "README.md"
        readme.write_text("# Test Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "branch", "integration"], cwd=self.repo_dir, check=True, capture_output=True)

        self.store = TaskStore(db_path=self.db_path)
        self.emitter = EventEmitter(log_path=self.events_file, enable_notifications=False)
        self.active_services: List[TaskService] = []
        self.secret_key = get_or_create_auth_token()

    def tearDown(self) -> None:
        for s in self.active_services:
            try:
                s.wait_all(timeout_sec=1.5)
            except Exception:
                pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _sample_spec(self, goal: str = "Implement hello()") -> TaskSpec:
        return TaskSpec(
            repo=str(self.repo_dir),
            goal=goal,
            verification_commands=["python -c \"import app; assert app.hello() == 'world'\""],
            constraints=["Do not modify outside repo", "No network calls in generated app"],
            allowed_merge_target="integration",
        )

    def _create_valid_approval(
        self,
        spec: TaskSpec,
        nonce: Optional[str] = "nonce-acc-1",
        conversation_id: str = "conv-1",
        message_id: str = "msg-1",
    ) -> ApprovalRecord:
        spec_hash = spec.compute_hash()
        token = create_approval_token(
            spec_id=spec_hash,
            repo=spec.repo,
            conversation_id=conversation_id,
            message_id=message_id,
            secret_key=self.secret_key,
            nonce=nonce,
        )
        return ApprovalRecord(
            spec_id=spec_hash,
            repo=spec.repo,
            source="operator_cli",
            approved_by="operator",
            conversation_id=conversation_id,
            message_id=message_id,
            approval_token=token,
            nonce=nonce,
        )

    # =========================================================================
    # SCENARIO 1: Successful approved task
    # Propose -> approve -> dispatch -> plan -> implement -> review -> gate -> DONE -> timeline
    # =========================================================================
    def test_scenario_01_successful_approved_task(self) -> None:
        mock_engine = MockAcceptanceEngine(store=self.store)
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
        )
        self.active_services.append(service)

        spec = self._sample_spec()
        # Step A & B: Propose task
        proposal = service.propose_task(
            spec=spec,
            source="goldfish_chat",
            conversation_id="conv-s1",
            message_id="msg-prop-1",
        )
        self.assertEqual(proposal["status"], "PENDING_APPROVAL")
        prop_id = proposal["proposal_id"]

        # Step C: Approve proposal
        approval = self._create_valid_approval(spec, nonce="nonce-s1", conversation_id="conv-s1", message_id="msg-appr-1")
        submit_res = service.task_submit(
            spec=spec,
            idempotency_key="task-s1",
            approved=True,
            approved_hash=spec.compute_hash(),
            approval_record=approval,
        )
        task_id = submit_res["task_id"]

        # Wait for worker
        service.wait_all(timeout_sec=5.0)

        # Verify task is DONE
        status = service.task_status(task_id)
        self.assertEqual(status["state"], "DONE")

        # Verify evidence readback
        res = service.task_result(task_id)
        self.assertIsNotNone(res["candidate_commit"])
        self.assertIsNotNone(res["merge_commit"])
        self.assertIn("Approved by independent reviewer", str(res["review_findings"]))
        self.assertIn("All 1 verification commands passed", str(res["test_evidence"]))

        # Verify ordered timeline outbox events
        events = service.get_task_outbox_events(task_id)
        self.assertTrue(len(events) >= 6)
        seqs = [ev["seq"] for ev in events]
        self.assertEqual(seqs, sorted(seqs))
        types = [ev["event_type"] for ev in events]
        self.assertIn("task.started", types)
        self.assertIn("planner.completed", types)
        self.assertIn("implementation.completed", types)
        self.assertIn("review.completed", types)
        self.assertIn("verification.passed", types)
        self.assertIn("task.done", types)

    # =========================================================================
    # SCENARIO 2: No approval
    # Propose only -> zero execution, no commits, proposal remains PENDING_APPROVAL
    # =========================================================================
    def test_scenario_02_no_approval_causes_zero_execution(self) -> None:
        mock_engine = MockAcceptanceEngine()
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
        )
        self.active_services.append(service)

        spec = self._sample_spec()
        prop = service.propose_task(
            spec=spec,
            source="goldfish_chat",
            conversation_id="conv-s2",
            message_id="msg-prop-2",
        )
        self.assertEqual(prop["status"], "PENDING_APPROVAL")

        # No approval submitted
        time.sleep(0.3)

        # Verify zero engines invoked
        self.assertEqual(len(mock_engine.invoked_engines), 0)
        # Verify no git branch created or changed
        int_head = subprocess.check_output(["git", "rev-parse", "integration"], cwd=self.repo_dir, text=True).strip()
        main_head = subprocess.check_output(["git", "rev-parse", "main"], cwd=self.repo_dir, text=True).strip()
        self.assertEqual(int_head, main_head)

        # Verify proposal remains PENDING_APPROVAL
        fetched = service.get_proposal(prop["proposal_id"])
        self.assertEqual(fetched["status"], "PENDING_APPROVAL")

    # =========================================================================
    # SCENARIO 3: Forged approval
    # Fake operator_cli / local_web approval without HMAC token rejected
    # =========================================================================
    def test_scenario_03_forged_approval_rejected(self) -> None:
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            extra_allowed_repos=[self.repo_dir],
        )
        spec = self._sample_spec()
        spec_hash = spec.compute_hash()

        # Forged approval 1: Missing token
        forged_record_1 = ApprovalRecord(
            spec_id=spec_hash,
            repo=spec.repo,
            source="operator_cli",
            approved_by="attacker",
        )
        with self.assertRaises(ApprovalVerificationError) as ctx:
            service.task_submit(
                spec=spec,
                idempotency_key="forged-1",
                approved=True,
                approved_hash=spec_hash,
                approval_record=forged_record_1,
            )
        self.assertIn("valid cryptographic approval_token or signature required", str(ctx.exception))

        # Forged approval 2: Invalid HMAC token
        forged_record_2 = ApprovalRecord(
            spec_id=spec_hash,
            repo=spec.repo,
            source="operator_cli",
            approved_by="attacker",
            approval_token="sig-invalid-forged-hmac-hash-value-1234",
        )
        with self.assertRaises(ApprovalVerificationError) as ctx:
            service.task_submit(
                spec=spec,
                idempotency_key="forged-2",
                approved=True,
                approved_hash=spec_hash,
                approval_record=forged_record_2,
            )
        self.assertIn("Cryptographic approval signature verification failed", str(ctx.exception))

    # =========================================================================
    # SCENARIO 4: Changed specification
    # Altered goal/hash/repo rejected
    # =========================================================================
    def test_scenario_04_changed_specification_rejected(self) -> None:
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            extra_allowed_repos=[self.repo_dir],
        )
        spec1 = self._sample_spec(goal="Original Goal")
        spec2 = self._sample_spec(goal="Altered Goal with Malicious Payload")

        # Operator approved spec1
        approval = self._create_valid_approval(spec1, nonce="nonce-s4")

        # Attacker submits spec2 claiming approved_hash is spec1's hash
        with self.assertRaises(ApprovalVerificationError) as ctx:
            service.task_submit(
                spec=spec2,
                idempotency_key="tampered-spec",
                approved=True,
                approved_hash=spec1.compute_hash(),
                approval_record=approval,
            )
        self.assertIn("Approval hash mismatch", str(ctx.exception))

    # =========================================================================
    # SCENARIO 5: Duplicate approval (Replay attack)
    # Same nonce replayed -> rejected
    # =========================================================================
    def test_scenario_05_duplicate_approval_nonce_rejected(self) -> None:
        mock_engine = MockAcceptanceEngine()
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
        )
        self.active_services.append(service)
        spec = self._sample_spec()
        approval = self._create_valid_approval(spec, nonce="nonce-replay-unique-123")

        # First submission with nonce succeeds
        res1 = service.task_submit(
            spec=spec,
            idempotency_key="task-replay-1",
            approved=True,
            approved_hash=spec.compute_hash(),
            approval_record=approval,
        )
        self.assertEqual(res1["status"], "QUEUED")

        # Second submission reusing the exact same nonce for a new task fails
        with self.assertRaises(ApprovalVerificationError) as ctx:
            service.task_submit(
                spec=spec,
                idempotency_key="task-replay-2",
                approved=True,
                approved_hash=spec.compute_hash(),
                approval_record=approval,
            )
        self.assertIn("has already been consumed (replay attack prevented)", str(ctx.exception))

    # =========================================================================
    # SCENARIO 6: Agent question
    # Waits -> appears in inbox -> answered -> resumes
    # =========================================================================
    def test_scenario_06_agent_question_and_resume(self) -> None:
        mock_engine = MockAcceptanceEngine(trigger_question="Should we use JSON or YAML?")
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
        )
        self.active_services.append(service)
        spec = self._sample_spec()
        approval = self._create_valid_approval(spec, nonce="nonce-s6", conversation_id="conv-s6")

        res = service.task_submit(
            spec=spec,
            idempotency_key="task-s6",
            approved=True,
            approved_hash=spec.compute_hash(),
            approval_record=approval,
        )
        task_id = res["task_id"]

        # Wait until task transitions to WAITING_FOR_USER
        for _ in range(20):
            time.sleep(0.1)
            st = service.task_status(task_id)
            if st["state"] == "WAITING_FOR_USER":
                break

        self.assertEqual(service.task_status(task_id)["state"], "WAITING_FOR_USER")

        # Question appears in store
        questions = service.task_questions(task_id=task_id, status="PENDING")
        self.assertEqual(len(questions), 1)
        qid = questions[0]["question_id"]
        self.assertEqual(questions[0]["prompt"], "Should we use JSON or YAML?")

        # Provide valid answer
        ans_res = service.task_answer(qid, "Use JSON please", task_id=task_id)
        self.assertEqual(ans_res["status"], "ANSWERED")

        # Wait for task to resume and finish
        service.wait_all(timeout_sec=5.0)
        st_after = service.task_status(task_id)
        self.assertEqual(st_after["state"], "DONE")
        self.assertEqual(mock_engine.received_answer, "Use JSON please")

    # =========================================================================
    # SCENARIO 7: Unauthorized question
    # Wrong conversation_id/task_id -> rejected
    # =========================================================================
    def test_scenario_07_unauthorized_question_answer_rejected(self) -> None:
        mock_engine = MockAcceptanceEngine(trigger_question="Target port?")
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
        )
        self.active_services.append(service)
        spec = self._sample_spec()
        approval = self._create_valid_approval(spec, nonce="nonce-s7", conversation_id="conv-s7")

        res = service.task_submit(
            spec=spec,
            idempotency_key="task-s7",
            approved=True,
            approved_hash=spec.compute_hash(),
            approval_record=approval,
        )
        task_id = res["task_id"]

        for _ in range(20):
            time.sleep(0.1)
            if service.task_status(task_id)["state"] == "WAITING_FOR_USER":
                break

        questions = service.task_questions(task_id=task_id, status="PENDING")
        qid = questions[0]["question_id"]

        # Wrong task_id attempt
        with self.assertRaises((ValueError, BridgeError)) as ctx1:
            service.task_answer(qid, "Port 8080", task_id="wrong-task-id")
        self.assertIn("cross-task interference rejected", str(ctx1.exception))

        # Stale/answered or nonexistent question
        with self.assertRaises((KeyError, BridgeError)):
            service.task_answer("nonexistent-qid", "Port 8080")

        # Clean up by providing legitimate answer
        service.task_answer(qid, "Port 8080", task_id=task_id)
        service.wait_all(timeout_sec=2.0)

    # =========================================================================
    # SCENARIO 8: Review failure
    # Reviewer rejects -> does not merge into integration
    # =========================================================================
    def test_scenario_08_review_failure_blocks_merge(self) -> None:
        mock_engine = MockAcceptanceEngine(fail_review=True)
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
        )
        self.active_services.append(service)

        before_head = subprocess.check_output(["git", "rev-parse", "integration"], cwd=self.repo_dir, text=True).strip()

        spec = self._sample_spec()
        approval = self._create_valid_approval(spec, nonce="nonce-s8")
        res = service.task_submit(
            spec=spec,
            idempotency_key="task-s8",
            approved=True,
            approved_hash=spec.compute_hash(),
            approval_record=approval,
        )
        service.wait_all(timeout_sec=5.0)

        status = service.task_status(res["task_id"])
        self.assertEqual(status["state"], "REVIEW_FAILED")

        after_head = subprocess.check_output(["git", "rev-parse", "integration"], cwd=self.repo_dir, text=True).strip()
        self.assertEqual(before_head, after_head)

    # =========================================================================
    # SCENARIO 9: Verification failure
    # Failing deterministic test commands -> does not merge
    # =========================================================================
    def test_scenario_09_verification_failure_blocks_merge(self) -> None:
        mock_engine = MockAcceptanceEngine(fail_verification=True)
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
        )
        self.active_services.append(service)

        before_head = subprocess.check_output(["git", "rev-parse", "integration"], cwd=self.repo_dir, text=True).strip()

        spec = self._sample_spec()
        approval = self._create_valid_approval(spec, nonce="nonce-s9")
        res = service.task_submit(
            spec=spec,
            idempotency_key="task-s9",
            approved=True,
            approved_hash=spec.compute_hash(),
            approval_record=approval,
        )
        service.wait_all(timeout_sec=5.0)

        status = service.task_status(res["task_id"])
        self.assertEqual(status["state"], "REVIEW_FAILED")

        after_head = subprocess.check_output(["git", "rev-parse", "integration"], cwd=self.repo_dir, text=True).strip()
        self.assertEqual(before_head, after_head)

    # =========================================================================
    # SCENARIO 10: Restart & reconnection
    # Survives restart without double-merge or lost tasks
    # =========================================================================
    def test_scenario_10_restart_reconnection_idempotency(self) -> None:
        mock_engine = MockAcceptanceEngine()
        service1 = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
        )
        self.active_services.append(service1)

        spec = self._sample_spec()
        approval = self._create_valid_approval(spec, nonce="nonce-s10")
        res1 = service1.task_submit(
            spec=spec,
            idempotency_key="task-s10",
            approved=True,
            approved_hash=spec.compute_hash(),
            approval_record=approval,
        )
        service1.wait_all(timeout_sec=5.0)
        self.assertEqual(service1.task_status(res1["task_id"])["state"], "DONE")

        # Simulate restart of Boss Rig service with fresh instance sharing same store
        service2 = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
        )
        self.active_services.append(service2)

        # Query task status from new service
        st = service2.task_status(res1["task_id"])
        self.assertEqual(st["state"], "DONE")

        # Submitting with same idempotency key and nonce is an idempotent retry
        res2 = service2.task_submit(
            spec=spec,
            idempotency_key="task-s10",
            approved=True,
            approved_hash=spec.compute_hash(),
            approval_record=approval,
        )
        self.assertFalse(res2["is_new"])
        self.assertEqual(res2["task_id"], res1["task_id"])

    # =========================================================================
    # SCENARIO 11: Concurrent tasks
    # Two tasks execute independently without cross-talk
    # =========================================================================
    def test_scenario_11_concurrent_tasks_no_cross_talk(self) -> None:
        mock_engine = MockAcceptanceEngine()

        # Set up isolated git repos for each task to avoid worktree lock collisions
        repo_a = Path(self.test_dir) / "repo_a"
        repo_b = Path(self.test_dir) / "repo_b"
        for r in (repo_a, repo_b):
            r.mkdir(parents=True)
            subprocess.run(["git", "init"], cwd=r, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test Boss"], cwd=r, check=True)
            subprocess.run(["git", "config", "user.email", "boss@example.com"], cwd=r, check=True)
            (r / "README.md").write_text("# Repo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=r, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Init"], cwd=r, check=True, capture_output=True)
            subprocess.run(["git", "branch", "integration"], cwd=r, check=True, capture_output=True)

        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[repo_a, repo_b],
        )
        self.active_services.append(service)

        spec_a = TaskSpec(
            repo=str(repo_a),
            goal="Task A: Endpoint 1",
            verification_commands=["python -c \"import app; assert app.hello() == 'world'\""],
            allowed_merge_target="integration",
        )
        spec_b = TaskSpec(
            repo=str(repo_b),
            goal="Task B: Endpoint 2",
            verification_commands=["python -c \"import app; assert app.hello() == 'world'\""],
            allowed_merge_target="integration",
        )

        approval_a = self._create_valid_approval(spec_a, nonce="nonce-11a", conversation_id="conv-a")
        approval_b = self._create_valid_approval(spec_b, nonce="nonce-11b", conversation_id="conv-b")

        res_a = service.task_submit(
            spec=spec_a,
            idempotency_key="task-11a",
            approved=True,
            approved_hash=spec_a.compute_hash(),
            approval_record=approval_a,
        )
        res_b = service.task_submit(
            spec=spec_b,
            idempotency_key="task-11b",
            approved=True,
            approved_hash=spec_b.compute_hash(),
            approval_record=approval_b,
        )

        service.wait_all(timeout_sec=10.0)

        # Both should complete cleanly
        st_a = service.task_status(res_a["task_id"])
        st_b = service.task_status(res_b["task_id"])
        self.assertEqual(st_a["state"], "DONE")
        self.assertEqual(st_b["state"], "DONE")

        # Verify event isolation
        events_a = service.get_task_outbox_events(res_a["task_id"])
        events_b = service.get_task_outbox_events(res_b["task_id"])
        self.assertTrue(all(e["task_id"] == res_a["task_id"] for e in events_a))
        self.assertTrue(all(e["task_id"] == res_b["task_id"] for e in events_b))

    # =========================================================================
    # SCENARIO 12: Cancellation
    # Active task cancelled -> process tree stopped, state CANCELLED
    # =========================================================================
    def test_scenario_12_cancellation_terminates_and_marks_cancelled(self) -> None:
        mock_engine = MockAcceptanceEngine(slow_delay=2.0)
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
        )
        self.active_services.append(service)

        spec = self._sample_spec()
        approval = self._create_valid_approval(spec, nonce="nonce-s12")

        res = service.task_submit(
            spec=spec,
            idempotency_key="task-s12",
            approved=True,
            approved_hash=spec.compute_hash(),
            approval_record=approval,
        )
        task_id = res["task_id"]

        time.sleep(0.2)
        cancel_res = service.task_cancel(task_id)
        self.assertTrue(cancel_res)

        service.wait_all(timeout_sec=5.0)
        st = service.task_status(task_id)
        self.assertEqual(st["state"], "CANCELLED")

        # Timeline has task.cancelled event
        events = service.get_task_outbox_events(task_id)
        types = [e["event_type"] for e in events]
        self.assertIn("task.cancelled", types)


if __name__ == "__main__":
    unittest.main()
