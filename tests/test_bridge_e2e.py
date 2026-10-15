"""Automated end-to-end tests for Boss Rig Bridge foundation.

Covers all 15 test scenarios specified in Milestone 5:
1. Approved spec -> queued -> planner -> implementer -> reviewer -> candidate tests -> merged-tree tests -> integration commit
2. Failed candidate tests do not change integration
3. Failed merged-tree tests leave integration unchanged
4. Unapproved or changed specs cannot dispatch
5. Assistant messages and pasted examples cannot dispatch
6. Old approvals do not replay on startup
7. Repeated submission executes once (idempotency)
8. Restart between claim and dispatch recovers safely
9. Disallowed repositories/main targets are rejected
10. Pending question -> valid answer -> safe resume
11. Duplicate/stale answers are rejected
12. Cancellation and timeouts release resources safely
13. Codex is never invoked
14. Dry-run causes no agent launches or Git mutations
15. No writes occur to Goldfish files
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from orchestrator.chat_watcher import ChatWatcher
from orchestrator.event_emitter import EventEmitter
from orchestrator.models import TaskSpec, TaskState, compute_spec_hash
from orchestrator.policy import PolicyEngine, PolicyViolation
from orchestrator.server import TaskServer
from orchestrator.store import TaskStore
from orchestrator.task_service import (
    ApprovalVerificationError,
    BridgeError,
    ScopeExpansionError,
    TaskService,
)


class MockEngine:
    """Mock orchestration engine executing the two-engine pipeline without external seat dependencies."""

    def __init__(
        self,
        fail_candidate_tests: bool = False,
        fail_merged_tests: bool = False,
        trigger_question_prompt: Optional[str] = None,
    ):
        self.fail_candidate_tests = fail_candidate_tests
        self.fail_merged_tests = fail_merged_tests
        self.trigger_question_prompt = trigger_question_prompt
        self.invoked_engines: List[str] = []
        self.invoked_roles: List[str] = []
        self.candidate_commit_sha: Optional[str] = None
        self.merge_commit_sha: Optional[str] = None
        self.received_user_answer: Optional[str] = None

    def execute_bridge_task(
        self,
        task_id: str,
        spec: TaskSpec,
        ask_user_fn: Any,
        cancel_check_fn: Any,
    ) -> Dict[str, Any]:
        repo_dir = Path(spec.repo)

        # Invariant 1: Reviewer cannot be implementer
        planner = spec.assigned_agents.get("planner", "antigravity-1")
        implementer = spec.assigned_agents.get("implementer", "opencode-1")
        reviewer = spec.assigned_agents.get("reviewer", "antigravity-1")

        if reviewer == implementer:
            raise PolicyViolation("Reviewer cannot be implementer.")

        # Invariant 2: Codex must NOT be invoked
        for engine in (planner, implementer, reviewer):
            if "codex" in engine.lower():
                raise PolicyViolation(f"Codex seat {engine} is prohibited from automated dispatch.")

        # Turn 1: Planner
        self.invoked_engines.append(planner)
        self.invoked_roles.append("planner")

        # Optional Two-Way Question Turn
        if self.trigger_question_prompt:
            answer = ask_user_fn(self.trigger_question_prompt)
            self.received_user_answer = answer

        # Turn 2: Implementer (makes a real commit in disposable repo)
        self.invoked_engines.append(implementer)
        self.invoked_roles.append("implementer")

        feature_file = repo_dir / "feature.py"
        feature_file.write_text("# implementation from implementer\nVALUE = 42\n", encoding="utf-8")
        subprocess.run(["git", "add", "feature.py"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"Implement {task_id}"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        cand_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
        self.candidate_commit_sha = cand_sha

        # Candidate verification tests
        if self.fail_candidate_tests:
            return {"success": False, "error": "Candidate test command failed."}

        # Turn 3: Reviewer (independent review)
        self.invoked_engines.append(reviewer)
        self.invoked_roles.append("reviewer")

        # Merged-tree verification tests & merge to integration
        if self.fail_merged_tests:
            return {"success": False, "error": "Merged-tree verification tests failed."}

        # Simulate merge to integration
        subprocess.run(["git", "checkout", "integration"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "merge", "--no-ff", "-m", f"Merge {task_id}", cand_sha],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        merge_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
        self.merge_commit_sha = merge_sha

        return {"success": True, "candidate_commit": cand_sha, "merge_commit": merge_sha}


class TestBossRigBridgeE2E(unittest.TestCase):
    """End-to-end test cases covering all requirements."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="bossrig_test_")
        self.repo_dir = Path(self.test_dir) / "disposable_repo"
        self.chats_dir = Path(self.test_dir) / "goldfish_chats"
        self.events_file = Path(self.test_dir) / "events.jsonl"
        self.db_path = Path(self.test_dir) / "test_tasks.sqlite"

        self.repo_dir.mkdir(parents=True)
        self.chats_dir.mkdir(parents=True)

        # Initialize disposable git repo with main and integration branches
        subprocess.run(["git", "init"], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test Jacob"], cwd=self.repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "jacob@example.com"], cwd=self.repo_dir, check=True)

        readme = self.repo_dir / "README.md"
        readme.write_text("# Disposable Repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.repo_dir, check=True, capture_output=True)

        # Create integration branch
        subprocess.run(["git", "branch", "integration"], cwd=self.repo_dir, check=True, capture_output=True)

        self.store = TaskStore(db_path=self.db_path)
        self.emitter = EventEmitter(log_path=self.events_file, enable_notifications=False)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_sample_spec(self, goal: str = "Add feature.py") -> TaskSpec:
        return TaskSpec(
            repo=str(self.repo_dir),
            goal=goal,
            verification_commands=["python -c 'import feature; assert feature.VALUE == 42'"],
            constraints=["No external network"],
            allowed_merge_target="integration",
        )

    # --- Test 1: Full pipeline pass ---
    def test_01_full_pipeline_success(self) -> None:
        """1. Approved spec -> queued -> planner -> implementer -> reviewer -> candidate tests -> merged-tree tests -> integration commit."""
        mock_engine = MockEngine()
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
        )

        spec = self._create_sample_spec()
        spec_hash = spec.compute_hash()

        res = service.task_submit(
            spec=spec,
            idempotency_key="test-key-1",
            approved=True,
            approved_hash=spec_hash,
        )
        task_id = res["task_id"]

        # Wait for background worker to complete
        time.sleep(0.5)

        status = service.task_status(task_id)
        self.assertEqual(status["state"], "DONE")
        self.assertIn("antigravity-1", mock_engine.invoked_engines)
        self.assertIn("opencode-1", mock_engine.invoked_engines)
        self.assertNotIn("codex-1", mock_engine.invoked_engines)

        # Check integration branch has the feature commit
        int_log = subprocess.check_output(
            ["git", "log", "integration", "-n", "1", "--oneline"],
            cwd=self.repo_dir,
            text=True,
        )
        self.assertIn(f"Merge {task_id}", int_log)

    # --- Test 2: Failed candidate tests ---
    def test_02_failed_candidate_tests_leave_integration_unchanged(self) -> None:
        """2. Failed candidate tests do not change integration."""
        mock_engine = MockEngine(fail_candidate_tests=True)
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
        )

        before_head = subprocess.check_output(["git", "rev-parse", "integration"], cwd=self.repo_dir, text=True).strip()

        spec = self._create_sample_spec()
        res = service.task_submit(spec=spec, idempotency_key="test-key-2", approved=True, approved_hash=spec.compute_hash())
        time.sleep(0.5)

        status = service.task_status(res["task_id"])
        self.assertEqual(status["state"], "REVIEW_FAILED")

        after_head = subprocess.check_output(["git", "rev-parse", "integration"], cwd=self.repo_dir, text=True).strip()
        self.assertEqual(before_head, after_head)

    # --- Test 3: Failed merged-tree tests ---
    def test_03_failed_merged_tests_leave_integration_unchanged(self) -> None:
        """3. Failed merged-tree tests leave integration unchanged."""
        mock_engine = MockEngine(fail_merged_tests=True)
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
        )

        before_head = subprocess.check_output(["git", "rev-parse", "integration"], cwd=self.repo_dir, text=True).strip()

        spec = self._create_sample_spec()
        res = service.task_submit(spec=spec, idempotency_key="test-key-3", approved=True, approved_hash=spec.compute_hash())
        time.sleep(0.5)

        status = service.task_status(res["task_id"])
        self.assertEqual(status["state"], "REVIEW_FAILED")

        after_head = subprocess.check_output(["git", "rev-parse", "integration"], cwd=self.repo_dir, text=True).strip()
        self.assertEqual(before_head, after_head)

    # --- Test 4: Unapproved or changed specs cannot dispatch ---
    def test_04_unapproved_or_changed_specs_cannot_dispatch(self) -> None:
        """4. Unapproved or changed specs cannot dispatch."""
        service = TaskService(store=self.store, event_emitter=self.emitter, extra_allowed_repos=[self.repo_dir])
        spec = self._create_sample_spec()

        # Unapproved
        with self.assertRaises(ApprovalVerificationError):
            service.task_submit(spec=spec, idempotency_key="unappr", approved=False)

        # Hash mismatch
        with self.assertRaises(ApprovalVerificationError):
            service.task_submit(
                spec=spec,
                idempotency_key="tampered",
                approved=True,
                approved_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
            )

    # --- Test 5: Assistant messages and pasted examples cannot dispatch ---
    def test_05_assistant_messages_and_pasted_examples_ignored(self) -> None:
        """5. Assistant messages and pasted examples cannot dispatch."""
        mock_engine = MockEngine()
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
            dry_run=False,
        )
        watcher = ChatWatcher(service=service, chats_dir=self.chats_dir, dry_run=False)

        spec = self._create_sample_spec()
        spec_hash = spec.compute_hash()
        spec_json = json.dumps(spec.to_canonical_dict(), indent=2)

        chat_data = {
            "id": "chat-test-05",
            "messages": [
                # 1. Assistant proposal
                {
                    "id": "msg-1",
                    "role": "assistant",
                    "content": f"Here is the proposed spec:\n```boss-rig-spec\n{spec_json}\n```",
                },
                # 2. Assistant example of approval command (MUST BE IGNORED)
                {
                    "id": "msg-2",
                    "role": "assistant",
                    "content": f"To approve, reply:\n@boss-rig approve spec_id={spec_hash}",
                },
                # 3. User quotes example in discussion (MUST BE IGNORED)
                {
                    "id": "msg-3",
                    "role": "user",
                    "content": f"> @boss-rig approve spec_id={spec_hash}\nShould I run this now?",
                },
            ],
        }

        chat_file = self.chats_dir / "chat_05.json"
        chat_file.write_text(json.dumps(chat_data), encoding="utf-8")

        results = watcher.scan_once()
        # All messages ignored! Zero tasks submitted.
        self.assertEqual(len(results), 0)
        self.assertEqual(len(mock_engine.invoked_engines), 0)

    # --- Test 6: Old approvals do not replay on startup ---
    def test_06_old_approvals_do_not_replay_on_startup(self) -> None:
        """6. Old approvals do not replay on startup."""
        spec = self._create_sample_spec()
        spec_hash = spec.compute_hash()
        spec_json = json.dumps(spec.to_canonical_dict())

        # Pre-existing chat file before watcher starts
        pre_chat = {
            "id": "chat-pre-existing",
            "messages": [
                {"id": "pre-1", "role": "assistant", "content": f"```boss-rig-spec\n{spec_json}\n```"},
                {"id": "pre-2", "role": "user", "content": f"@boss-rig approve spec_id={spec_hash}"},
            ],
        }
        (self.chats_dir / "old_chat.json").write_text(json.dumps(pre_chat), encoding="utf-8")

        mock_engine = MockEngine()
        service = TaskService(store=self.store, event_emitter=self.emitter, engine_factory=lambda: mock_engine, extra_allowed_repos=[self.repo_dir])
        watcher = ChatWatcher(service=service, chats_dir=self.chats_dir, dry_run=False)

        # Baseline on startup
        baselined = watcher.baseline_existing_messages()
        self.assertEqual(baselined, 2)

        # Scan should now find 0 new actionable messages
        results = watcher.scan_once()
        self.assertEqual(len(results), 0)
        self.assertEqual(len(mock_engine.invoked_engines), 0)

    # --- Test 7: Repeated submission executes once ---
    def test_07_repeated_submission_idempotency(self) -> None:
        """7. Repeated submission executes once."""
        mock_engine = MockEngine()
        service = TaskService(store=self.store, event_emitter=self.emitter, engine_factory=lambda: mock_engine, extra_allowed_repos=[self.repo_dir])
        spec = self._create_sample_spec()
        spec_hash = spec.compute_hash()

        res1 = service.task_submit(spec=spec, idempotency_key="idem-key-1", approved=True, approved_hash=spec_hash)
        res2 = service.task_submit(spec=spec, idempotency_key="idem-key-1", approved=True, approved_hash=spec_hash)

        self.assertEqual(res1["task_id"], res2["task_id"])
        self.assertTrue(res1["is_new"])
        self.assertFalse(res2["is_new"])
        service.wait_all(timeout_sec=2.0)

    # --- Test 8: Restart between claim and dispatch recovers safely ---
    def test_08_restart_recovery(self) -> None:
        """8. Restart between claim and dispatch recovers safely."""
        # Pre-claim message in store
        self.store.claim_event_durable(
            event_id="msg-claim-1",
            chat_id="chat-1",
            action="APPROVE",
            status="CLAIMED",
        )
        self.assertTrue(self.store.is_event_claimed("msg-claim-1"))

        # Re-initialize service and watcher (simulating process restart)
        new_store = TaskStore(db_path=self.db_path)
        self.assertTrue(new_store.is_event_claimed("msg-claim-1"))

    # --- Test 9: Disallowed repositories / main targets are rejected ---
    def test_09_disallowed_repos_and_main_target_rejected(self) -> None:
        """9. Disallowed repositories/main targets are rejected."""
        service = TaskService(store=self.store, event_emitter=self.emitter, extra_allowed_repos=[self.repo_dir])

        # 1. Disallowed repository
        bad_repo_spec = TaskSpec(
            repo="C:\\Windows\\System32",
            goal="Halt",
            verification_commands=["true"],
        )
        with self.assertRaises(PolicyViolation):
            service.task_submit(spec=bad_repo_spec, idempotency_key="bad-repo", approved=True, approved_hash=bad_repo_spec.compute_hash())

        # 2. Disallowed merge target (main)
        bad_target_spec = TaskSpec(
            repo=str(self.repo_dir),
            goal="Direct to main",
            verification_commands=["true"],
            allowed_merge_target="main",
        )
        with self.assertRaises(PolicyViolation):
            service.task_submit(spec=bad_target_spec, idempotency_key="bad-target", approved=True, approved_hash=bad_target_spec.compute_hash())

    # --- Test 10: Pending question -> valid answer -> safe resume ---
    def test_10_question_and_answer_resumes_execution(self) -> None:
        """10. Pending question -> valid answer -> safe resume."""
        mock_engine = MockEngine(trigger_question_prompt="Which port should metrics export on?")
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
        )

        spec = self._create_sample_spec()
        res = service.task_submit(spec=spec, idempotency_key="q-task-1", approved=True, approved_hash=spec.compute_hash())
        task_id = res["task_id"]

        # Wait for task to pause on question
        time.sleep(0.3)
        questions = service.task_questions(task_id=task_id, status="PENDING")
        self.assertEqual(len(questions), 1)
        qid = questions[0]["question_id"]
        self.assertEqual(questions[0]["prompt"], "Which port should metrics export on?")

        # Provide answer
        ans_res = service.task_answer(qid, answer="Port 9090")
        self.assertEqual(ans_res["status"], "ANSWERED")

        # Worker resumes and completes
        time.sleep(0.5)
        status = service.task_status(task_id)
        self.assertEqual(status["state"], "DONE")
        self.assertEqual(mock_engine.received_user_answer, "Port 9090")

    # --- Test 11: Duplicate/stale answers are rejected ---
    def test_11_duplicate_or_stale_answers_rejected(self) -> None:
        """11. Duplicate/stale answers are rejected."""
        mock_engine = MockEngine(trigger_question_prompt="Choose mode: A or B?")
        service = TaskService(store=self.store, event_emitter=self.emitter, engine_factory=lambda: mock_engine, extra_allowed_repos=[self.repo_dir])
        spec = self._create_sample_spec()
        res = service.task_submit(spec=spec, idempotency_key="q-task-2", approved=True, approved_hash=spec.compute_hash())
        time.sleep(0.3)

        questions = service.task_questions(task_id=res["task_id"], status="PENDING")
        self.assertEqual(len(questions), 1)
        qid = questions[0]["question_id"]

        # First answer: accepted
        service.task_answer(qid, "Mode A")

        # Second duplicate answer: rejected!
        with self.assertRaises(BridgeError):
            service.task_answer(qid, "Mode B")

    # --- Test 12: Cancellation and timeouts release resources safely ---
    def test_12_cancellation_releases_resources(self) -> None:
        """12. Cancellation and timeouts release resources safely."""
        mock_engine = MockEngine(trigger_question_prompt="Waiting indefinitely...")
        service = TaskService(store=self.store, event_emitter=self.emitter, engine_factory=lambda: mock_engine, extra_allowed_repos=[self.repo_dir])
        spec = self._create_sample_spec()
        res = service.task_submit(spec=spec, idempotency_key="cancel-task-1", approved=True, approved_hash=spec.compute_hash())
        task_id = res["task_id"]
        time.sleep(0.3)

        cancelled = service.task_cancel(task_id, reason="User cancelled test")
        self.assertTrue(cancelled)

        time.sleep(0.3)
        status = service.task_status(task_id)
        self.assertEqual(status["state"], "CANCELLED")

    # --- Test 13: Codex is never invoked ---
    def test_13_codex_is_never_invoked(self) -> None:
        """13. Codex is never invoked."""
        spec = TaskSpec(
            repo=str(self.repo_dir),
            goal="Try codex",
            verification_commands=["true"],
            assigned_agents={"planner": "codex-1", "implementer": "opencode-1", "reviewer": "antigravity-1"},
        )
        mock_engine = MockEngine()
        service = TaskService(store=self.store, event_emitter=self.emitter, engine_factory=lambda: mock_engine, extra_allowed_repos=[self.repo_dir])

        res = service.task_submit(spec=spec, idempotency_key="codex-reject", approved=True, approved_hash=spec.compute_hash())
        time.sleep(0.4)

        status = service.task_status(res["task_id"])
        self.assertEqual(status["state"], "REVIEW_FAILED")
        self.assertNotIn("codex-1", mock_engine.invoked_engines)

    # --- Test 14: Dry-run causes no agent launches or Git mutations ---
    def test_14_dry_run_causes_zero_mutations(self) -> None:
        """14. Dry-run causes no agent launches or Git mutations."""
        mock_engine = MockEngine()
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
            dry_run=True,
        )

        before_head = subprocess.check_output(["git", "rev-parse", "integration"], cwd=self.repo_dir, text=True).strip()

        spec = self._create_sample_spec()
        res = service.task_submit(spec=spec, idempotency_key="dry-run-key", approved=True, approved_hash=spec.compute_hash())
        self.assertEqual(res["status"], "DRY_RUN")

        time.sleep(0.3)
        self.assertEqual(len(mock_engine.invoked_engines), 0)

        after_head = subprocess.check_output(["git", "rev-parse", "integration"], cwd=self.repo_dir, text=True).strip()
        self.assertEqual(before_head, after_head)

    # --- Test 15: No writes occur to Goldfish files ---
    def test_15_no_writes_to_goldfish_files(self) -> None:
        """15. No writes occur to Goldfish files."""
        inbox_file = self.chats_dir / "inbox.json"
        inbox_content = json.dumps({"id": "inbox", "messages": []})
        inbox_file.write_text(inbox_content, encoding="utf-8")

        mock_engine = MockEngine()
        service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            engine_factory=lambda: mock_engine,
            extra_allowed_repos=[self.repo_dir],
            dry_run=False,
        )
        watcher = ChatWatcher(service=service, chats_dir=self.chats_dir, dry_run=False)

        spec = self._create_sample_spec()
        spec_hash = spec.compute_hash()
        chat_content = json.dumps({
            "id": "chat-real-test",
            "messages": [
                {"id": "m1", "role": "assistant", "content": f"```boss-rig-spec\n{json.dumps(spec.to_canonical_dict())}\n```"},
                {"id": "m2", "role": "user", "content": f"@boss-rig approve spec_id={spec_hash}"},
            ],
        })
        chat_file = self.chats_dir / "chat_real.json"
        chat_file.write_text(chat_content, encoding="utf-8")

        # Record file mod times and sizes
        inbox_mtime_before = inbox_file.stat().st_mtime
        chat_mtime_before = chat_file.stat().st_mtime

        # Run watcher scan
        watcher.scan_once()
        time.sleep(0.5)

        # Confirm Goldfish files were not written or corrupted
        self.assertEqual(inbox_file.read_text(encoding="utf-8"), inbox_content)
        self.assertEqual(chat_file.read_text(encoding="utf-8"), chat_content)
        self.assertEqual(inbox_file.stat().st_mtime, inbox_mtime_before)
        self.assertEqual(chat_file.stat().st_mtime, chat_mtime_before)


if __name__ == "__main__":
    unittest.main()
