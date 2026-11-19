"""Cross-platform Windows/WSL dispatcher, path conversion, and failure-stage regression tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.dispatcher import SeatBridgeProcess, TaskDispatcher
from orchestrator.engine import OrchestrationEngine
from orchestrator.event_emitter import EventEmitter
from orchestrator.models import TaskSpec, TaskState
from orchestrator.security import create_approval_token, to_wsl_path
from orchestrator.server import get_or_create_auth_token
from orchestrator.store import TaskStore
from orchestrator.task_service import TaskService
from plane.adapters.speaker import ReportFileSpeaker, ToastNotificationSpeaker
from plane.schemas import BuildReport


def _init_temp_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "integration"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
    (repo_dir / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)


class TestWindowsDispatcherAndStages(unittest.TestCase):
    def test_to_wsl_path_conversion(self) -> None:
        self.assertEqual(
            to_wsl_path(r"C:\Users\jacob\Boss Agent\scripts\worktree-gate.sh", platform="win32"),
            "/mnt/c/Users/jacob/Boss Agent/scripts/worktree-gate.sh",
        )
        self.assertEqual(
            to_wsl_path("D:/repos/my app/file.py", platform="win32"),
            "/mnt/d/repos/my app/file.py",
        )
        # No double conversion if already POSIX / /mnt/...
        self.assertEqual(
            to_wsl_path("/mnt/c/Users/jacob/Boss Agent", platform="win32"),
            "/mnt/c/Users/jacob/Boss Agent",
        )
        # No-op on Linux
        self.assertEqual(
            to_wsl_path(r"C:\Users\jacob\Boss Agent", platform="linux"),
            r"C:\Users\jacob\Boss Agent",
        )

    def test_seat_bridge_process_reads_events_and_handles_eof_without_set_blocking(self) -> None:
        script = (
            "import sys, json, time\n"
            "line = sys.stdin.readline().strip()\n"
            "req = json.loads(line)\n"
            "print('BRIDGE_EVENT ' + json.dumps({'event': 'task_started', 'task_id': req['task_id'], 'status': 'RUNNING'}), flush=True)\n"
            "time.sleep(0.08)\n"
            "print('BRIDGE_EVENT ' + json.dumps({'event': 'task_result', 'task_id': req['task_id'], 'status': 'SUCCESS'}), flush=True)\n"
            "sys.stdout.flush()\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            bridge = SeatBridgeProcess(
                seat_id="codex-1",
                script_name="codex-seat-runner.sh",
                cwd=Path(tmp),
                command=[sys.executable, "-c", script],
            )
            try:
                # Timeout before sending anything returns None cleanly
                self.assertIsNone(bridge.read_event(timeout_sec=0.03))
                bridge.send_line(json.dumps({"task_id": "T-WIN-1"}))
                ev1 = bridge.read_until_event("task_started", timeout_sec=3.0)
                self.assertIsNotNone(ev1)
                self.assertEqual(ev1["event"], "task_started")
                ev2 = bridge.read_until_event("task_result", timeout_sec=3.0)
                self.assertIsNotNone(ev2)
                self.assertEqual(ev2["event"], "task_result")
                self.assertEqual(ev2["status"], "SUCCESS")
                # EOF reached cleanly -> returns None immediately without hanging
                self.assertIsNone(bridge.read_event(timeout_sec=1.0))
            finally:
                bridge.close()
                self.assertFalse(bridge.is_alive())

    def test_run_gate_converts_paths_for_wsl_and_init_seat_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_dir = tmp_path / "Boss Agent"
            _init_temp_git_repo(repo_dir)
            store = TaskStore(db_path=tmp_path / "tasks.sqlite")
            engine = OrchestrationEngine(target_repo=repo_dir, store=store)

            captured = {}

            def fake_run(cmd, cwd=None, capture_output=True, text=True, **kwargs):
                captured["cmd"] = cmd
                captured["cwd"] = cwd
                return subprocess.CompletedProcess(
                    cmd, returncode=1, stdout="gate stdout info", stderr="missing integration branch"
                )

            with patch("orchestrator.engine.sys.platform", "win32"), patch(
                "orchestrator.security.sys.platform", "win32"
            ), patch("orchestrator.engine.subprocess.run", side_effect=fake_run):
                engine.create_task("T-INIT-FAIL", "Test init fail")
                engine.assign_role("T-INIT-FAIL", "antigravity-1", "planner")
                with self.assertRaises(RuntimeError) as ctx:
                    engine.execute_turn("T-INIT-FAIL", "antigravity-1", "planner")

            self.assertEqual(captured["cmd"][0], "bash")
            self.assertTrue(captured["cmd"][1].startswith("/mnt/"))
            self.assertIn("worktree-gate.sh", captured["cmd"][1])
            self.assertIn("init-seat", str(ctx.exception))
            self.assertIn("missing integration branch", str(ctx.exception))
            # Crucial: no stale empty .worktrees/antigravity-1 directory left behind
            self.assertFalse((repo_dir / ".worktrees" / "antigravity-1").exists())

            # TaskDispatcher also refuses missing/non-git seat worktree without mkdir fallback
            dispatcher = TaskDispatcher(store=store, worktrees_dir=repo_dir / ".worktrees")
            with self.assertRaises(RuntimeError):
                dispatcher.get_or_start_bridge("codex-1")
            self.assertFalse((repo_dir / ".worktrees" / "codex-1").exists())

    def test_task_service_reports_actual_failed_stage_not_review_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_dir = tmp_path / ".disposable-live-repo"
            _init_temp_git_repo(repo_dir)
            store = TaskStore(db_path=tmp_path / "tasks.sqlite")
            events_path = tmp_path / "events.jsonl"
            emitter = EventEmitter(log_path=events_path, enable_notifications=False)
            reports_dir = tmp_path / "reports"

            service = TaskService(
                store=store,
                event_emitter=emitter,
                extra_allowed_repos=[repo_dir],
                dry_run=False,
            )
            spec = TaskSpec(
                repo=str(repo_dir),
                goal="Build Pomodoro app",
                verification_commands=["python -m unittest"],
                assigned_agents={
                    "planner": "antigravity-1",
                    "implementer": "codex-1",
                    "reviewer": "opencode-1",
                },
            )
            spec_hash = spec.compute_hash()
            tok = create_approval_token(
                spec_id=spec_hash,
                repo=str(repo_dir),
                conversation_id="c1",
                message_id="m1",
                secret_key=get_or_create_auth_token(),
                nonce="n-stage-1",
            )
            approval = {
                "spec_id": spec_hash,
                "repo": str(repo_dir),
                "source": "local_web",
                "conversation_id": "c1",
                "message_id": "m1",
                "approval_token": tok,
                "nonce": "n-stage-1",
            }

            with patch.object(
                OrchestrationEngine,
                "execute_turn",
                side_effect=RuntimeError("init-seat failed in planner bridge"),
            ):
                sub = service.task_submit(
                    spec=spec,
                    idempotency_key="stage-fail-1",
                    approved=True,
                    approved_hash=spec_hash,
                    approval_record=approval,
                )
                service.wait_all(timeout_sec=5.0)

            tid = sub["task_id"]
            res = service.task_result(tid)
            self.assertEqual(res["state"], TaskState.FAILED.value)
            self.assertNotEqual(res["state"], TaskState.REVIEW_FAILED.value)
            self.assertTrue(res["finished"])
            self.assertEqual(res["failed_stage"], "planner")
            self.assertIn("init-seat failed", res["error"])

            # Speaker report & toast reflect FAILED and failed_stage
            report = BuildReport(
                task_id=tid,
                status=res["state"],
                repo=res["repo"],
                goal=res["goal"],
                test_summary=res.get("error") or res["state"],
                failed_stage=res.get("failed_stage"),
            )
            ReportFileSpeaker(reports_dir=reports_dir).speak(report)
            ToastNotificationSpeaker(emitter=emitter).speak(report)

            md = (reports_dir / "latest_report.md").read_text(encoding="utf-8")
            self.assertIn("FAILED", md)
            self.assertIn("planner", md)
            self.assertNotIn("REVIEW_FAILED", md)

    def test_task_service_dispatch_failure_vs_reviewer_rejection_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_dir = tmp_path / ".disposable-live-repo"
            _init_temp_git_repo(repo_dir)
            store = TaskStore(db_path=tmp_path / "tasks.sqlite")
            emitter = EventEmitter(log_path=tmp_path / "events.jsonl", enable_notifications=False)
            service = TaskService(
                store=store,
                event_emitter=emitter,
                extra_allowed_repos=[repo_dir],
                dry_run=False,
            )
            spec = TaskSpec(
                repo=str(repo_dir),
                goal="Verify dispatch vs reviewer states",
                verification_commands=["python -m unittest"],
                assigned_agents={
                    "planner": "antigravity-1",
                    "implementer": "codex-1",
                    "reviewer": "opencode-1",
                },
            )
            spec_hash = spec.compute_hash()
            secret = get_or_create_auth_token()

            # 1. Dispatch stage exception -> FAILED with failed_stage="dispatch"
            tok1 = create_approval_token(
                spec_id=spec_hash,
                repo=str(repo_dir),
                conversation_id="c1",
                message_id="m-disp",
                secret_key=secret,
                nonce="n-disp",
            )
            with patch.object(
                OrchestrationEngine,
                "create_task",
                side_effect=RuntimeError("dispatch init boom"),
            ):
                sub1 = service.task_submit(
                    spec=spec,
                    idempotency_key="disp-fail",
                    approved=True,
                    approved_hash=spec_hash,
                    approval_record={
                        "spec_id": spec_hash,
                        "repo": str(repo_dir),
                        "source": "local_web",
                        "conversation_id": "c1",
                        "message_id": "m-disp",
                        "approval_token": tok1,
                        "nonce": "n-disp",
                    },
                )
                service.wait_all(timeout_sec=5.0)

            res1 = service.task_result(sub1["task_id"])
            self.assertEqual(res1["state"], TaskState.FAILED.value)
            self.assertEqual(res1["failed_stage"], "dispatch")

            # 2. Actual reviewer rejection -> REVIEW_FAILED with failed_stage="reviewer"
            tok2 = create_approval_token(
                spec_id=spec_hash,
                repo=str(repo_dir),
                conversation_id="c1",
                message_id="m-rev",
                secret_key=secret,
                nonce="n-rev",
            )

            def fake_turn(self_eng, task_id, engine, role, prompt=None, **kwargs):
                if role == "reviewer":
                    return {"status": "ERROR", "error": "Tests missing"}
                return {"status": "SUCCESS"}

            with patch.object(OrchestrationEngine, "execute_turn", fake_turn):
                sub2 = service.task_submit(
                    spec=spec,
                    idempotency_key="rev-fail",
                    approved=True,
                    approved_hash=spec_hash,
                    approval_record={
                        "spec_id": spec_hash,
                        "repo": str(repo_dir),
                        "source": "local_web",
                        "conversation_id": "c1",
                        "message_id": "m-rev",
                        "approval_token": tok2,
                        "nonce": "n-rev",
                    },
                )
                service.wait_all(timeout_sec=5.0)

            res2 = service.task_result(sub2["task_id"])
            self.assertEqual(res2["state"], TaskState.REVIEW_FAILED.value)
            self.assertEqual(res2["failed_stage"], "reviewer")


if __name__ == "__main__":
    unittest.main()
