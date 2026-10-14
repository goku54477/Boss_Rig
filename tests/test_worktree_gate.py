#!/usr/bin/env python3
"""Automated tests for worktree gate and supervisor invariants."""

import os
import subprocess
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "worktree-gate.sh"


def run_gate(*args: str) -> subprocess.CompletedProcess[str]:
    cmd = ["bash", str(SCRIPT_PATH), *args]
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def git_cmd(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    cmd = ["git", *args]
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


class TestWorktreeGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Ensure integration branch and _integration worktree exist
        git_cmd("show-ref", "--verify", "--quiet", "refs/heads/integration")
        int_wt = REPO_ROOT / ".worktrees" / "_integration"
        if not int_wt.exists():
            git_cmd("worktree", "add", str(int_wt), "integration")

    def setUp(self) -> None:
        # Recover any stale locks before test
        run_gate("recover", "--force")
        seat_wt = REPO_ROOT / ".worktrees" / "gate-test-1"
        if seat_wt.exists():
            subprocess.run(["git", "-C", str(seat_wt), "checkout", "seat/gate-test-1"], capture_output=True)
            subprocess.run(["git", "-C", str(seat_wt), "reset", "--hard", "integration"], capture_output=True)
            subprocess.run(["git", "-C", str(seat_wt), "clean", "-fd"], capture_output=True)

    def test_01_init_seat(self) -> None:
        """Invariant: init-seat provisions persistent seat worktree."""
        res = run_gate("init-seat", "gate-test-1")
        self.assertIn(res.returncode, (0,), f"init-seat failed: {res.stderr}")
        seat_wt = REPO_ROOT / ".worktrees" / "gate-test-1"
        self.assertTrue(seat_wt.exists())
        self.assertTrue((seat_wt / ".git").exists())

    def test_02_assign_task(self) -> None:
        """Invariant: assign switches worktree to transient task branch and marks ACTIVE."""
        task_id = f"T-GATE-02-{int(time.time()*1000)}"
        run_gate("init-seat", "gate-test-1")
        res = run_gate("assign", task_id, "gate-test-1")
        self.assertEqual(res.returncode, 0, f"assign failed: {res.stderr}")

        status = run_gate("status", task_id)
        self.assertEqual(status.stdout.strip(), "ACTIVE")

        # Verify current branch in worktree
        seat_wt = REPO_ROOT / ".worktrees" / "gate-test-1"
        curr_branch = subprocess.run(
            ["git", "-C", str(seat_wt), "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(curr_branch, f"task/{task_id}")

    def test_03_review_and_merge_success(self) -> None:
        """Invariant: passing tests allow merge into integration and mark DONE."""
        task_id = f"T-GATE-03-{int(time.time()*1000)}"
        run_gate("init-seat", "gate-test-1")
        run_gate("assign", task_id, "gate-test-1")
        seat_wt = REPO_ROOT / ".worktrees" / "gate-test-1"

        # Create a fresh unique test commit
        filename = f"success_{int(time.time())}.txt"
        dummy_file = seat_wt / filename
        dummy_file.write_text(f"success content {time.time()}\n")
        git_cmd("add", filename, cwd=seat_wt)
        git_cmd("commit", "-m", f"feat: test success file {filename}", cwd=seat_wt)

        # Review with passing verification command
        res = run_gate("review", task_id, "gate-test-1", f"test -f {filename}")
        self.assertEqual(res.returncode, 0, f"review failed: {res.stderr}\nstdout: {res.stdout}")

        status = run_gate("status", task_id)
        self.assertEqual(status.stdout.strip(), "DONE")

        # Verify commit landed on integration
        int_wt = REPO_ROOT / ".worktrees" / "_integration"
        self.assertTrue((int_wt / filename).exists())

        # Release task
        rel_res = run_gate("release", task_id, "gate-test-1")
        self.assertEqual(rel_res.returncode, 0, f"release failed: {rel_res.stderr}")

    def test_04_failing_tests_prevent_merge(self) -> None:
        """Invariant: failing tests abort merge, set REVIEW_FAILED, and leave worktree inspectable."""
        task_id = f"T-GATE-04-{int(time.time()*1000)}"
        run_gate("init-seat", "gate-test-1")
        run_gate("assign", task_id, "gate-test-1")
        seat_wt = REPO_ROOT / ".worktrees" / "gate-test-1"

        filename = f"bad_{int(time.time())}.txt"
        bad_file = seat_wt / filename
        bad_file.write_text("bad\n")
        git_cmd("add", filename, cwd=seat_wt)
        git_cmd("commit", "-m", f"feat: intentional failure {filename}", cwd=seat_wt)

        # Review with failing verification command
        res = run_gate("review", task_id, "gate-test-1", "exit 1")
        self.assertNotEqual(res.returncode, 0)

        status = run_gate("status", task_id)
        self.assertEqual(status.stdout.strip(), "REVIEW_FAILED")

        # Verify worktree remains intact and inspectable
        self.assertTrue(bad_file.exists())

        # Verify integration branch was NOT polluted
        int_wt = REPO_ROOT / ".worktrees" / "_integration"
        self.assertFalse((int_wt / filename).exists())

        # Invariant: release MUST refuse when task is not DONE
        rel_res = run_gate("release", task_id, "gate-test-1")
        self.assertNotEqual(rel_res.returncode, 0)
        self.assertIn("refusing release: task is not DONE", rel_res.stderr)

        # Clean up failed task branch for subsequent tests
        git_cmd("switch", "seat/gate-test-1", cwd=seat_wt)
        git_cmd("reset", "--hard", "integration", cwd=seat_wt)
        subprocess.run(["git", "-C", str(REPO_ROOT), "branch", "-D", f"task/{task_id}"], capture_output=True)

    def test_05_locking_and_recovery(self) -> None:
        """Invariant: merge lock prevents concurrent merges; recover clears lock."""
        task_id = f"T-LOCK-05-{int(time.time()*1000)}"
        run_gate("init-seat", "gate-test-1")
        run_gate("assign", task_id, "gate-test-1")
        seat_wt = REPO_ROOT / ".worktrees" / "gate-test-1"

        filename = f"lock_{int(time.time())}.txt"
        test_file = seat_wt / filename
        test_file.write_text("lock test\n")
        git_cmd("add", filename, cwd=seat_wt)
        git_cmd("commit", "-m", f"feat: lock test {filename}", cwd=seat_wt)

        common_git = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if not os.path.isabs(common_git):
            common_git = str(REPO_ROOT / common_git)
        lock_dir = Path(common_git) / "openrig-merge.lock"

        # Manually create lock directory to simulate concurrent lock
        lock_dir.mkdir(exist_ok=True)
        (lock_dir / "pid").write_text("9999999\n")
        (lock_dir / "host").write_text("unknown\n")
        (lock_dir / "started").write_text("0\n")

        # Attempt review should report lock conflict (exit 75)
        res = run_gate("review", task_id, "gate-test-1", f"test -f {filename}")
        self.assertEqual(res.returncode, 75, f"Review should hit lock contention: {res.stderr}")

        # Run recover
        rec_res = run_gate("recover", "--force")
        self.assertEqual(rec_res.returncode, 0)
        self.assertFalse(lock_dir.exists(), "Lock should be cleared after recover --force")

        # Now review should succeed after recovery
        res2 = run_gate("review", task_id, "gate-test-1", f"test -f {filename}")
        self.assertEqual(res2.returncode, 0, f"Review should succeed after recovery: {res2.stderr}\nstdout: {res2.stdout}")
        self.assertEqual(run_gate("status", task_id).stdout.strip(), "DONE")
        run_gate("release", task_id, "gate-test-1")

    def test_07_safe_worktree_preflight_sanitization(self) -> None:
        """Requirement: Preflight sanitizes safe generated artifacts, but never deletes user untracked or tracked work."""
        seat_wt = REPO_ROOT / ".worktrees" / "gate-test-1"
        run_gate("init-seat", "gate-test-1")

        # Case A: Safe generated artifact is sanitized automatically
        safe_artifact = seat_wt / "matrix_proof_gate-test-1.txt"
        safe_artifact.write_text("automated proof\n")
        self.assertTrue(safe_artifact.exists())

        task_a = f"T-GATE-SAFE-{int(time.time()*1000)}"
        res_a = run_gate("assign", task_a, "gate-test-1")
        self.assertEqual(res_a.returncode, 0, f"assign should succeed after auto-sanitizing safe artifact: {res_a.stderr}")
        self.assertFalse(safe_artifact.exists(), "Safe generated artifact should have been sanitized")

        # Reset seat back to seat parking branch
        subprocess.run(["git", "-C", str(seat_wt), "checkout", "seat/gate-test-1"], capture_output=True)
        subprocess.run(["git", "-C", str(seat_wt), "reset", "--hard", "integration"], capture_output=True)

        # Case B: Untracked user file is NOT deleted, assignment fails safely
        user_file = seat_wt / "user_notes.md"
        user_file.write_text("# Important user notes\n")
        self.assertTrue(user_file.exists())

        task_b = f"T-GATE-USER-{int(time.time()*1000)}"
        res_b = run_gate("assign", task_b, "gate-test-1")
        self.assertNotEqual(res_b.returncode, 0, "assign must fail when user untracked file is present")
        self.assertTrue(user_file.exists(), "Untracked user file MUST NOT be deleted")
        self.assertIn("refusing deletion", res_b.stderr)

        # Clean user file manually for Case C
        user_file.unlink()

        # Case C: Tracked uncommitted modification is NOT deleted, assignment fails safely
        tracked_file = seat_wt / "README.md"
        if tracked_file.exists():
            original_content = tracked_file.read_text()
            tracked_file.write_text(original_content + "\n# Uncommitted user edit\n")

            task_c = f"T-GATE-TRACKED-{int(time.time()*1000)}"
            res_c = run_gate("assign", task_c, "gate-test-1")
            self.assertNotEqual(res_c.returncode, 0, "assign must fail when tracked modifications exist")
            self.assertIn("tracked changes", res_c.stderr)

            # Revert tracked modification
            subprocess.run(["git", "-C", str(seat_wt), "checkout", "--", "README.md"], capture_output=True)


if __name__ == "__main__":
    unittest.main()

