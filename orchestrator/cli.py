#!/usr/bin/env python3
"""Boss Rig Operator CLI — Production Multi-Agent Orchestrator Control."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.engine import OrchestrationEngine, run_gate
from orchestrator.models import KNOWN_ENGINES, SeatMode, TaskRecord, TaskState
from orchestrator.policy import PolicyEngine
from orchestrator.store import TaskStore


def get_seat_branch(seat: str) -> str:
    seat_wt = REPO_ROOT / ".worktrees" / seat
    if not seat_wt.exists():
        return "NOT_INITIALIZED"
    try:
        res = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=seat_wt,
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return "UNKNOWN"


def cmd_up(args: argparse.Namespace) -> int:
    print("🚀 Initializing Boss Rig Orchestrator seats...")
    # 1. Run OpenRig preflight
    preflight = subprocess.run(["rig", "preflight"], cwd=REPO_ROOT, capture_output=True, text=True)
    if preflight.returncode == 0:
        print("  ✓ OpenRig preflight passed")
    else:
        print(f"  ⚠ OpenRig preflight notice: {preflight.stderr or preflight.stdout}")

    # 2. Init all 3 seats
    for seat in KNOWN_ENGINES:
        init_res = run_gate("init-seat", seat)
        if init_res.returncode == 0:
            print(f"  ✓ Seat {seat} initialized at .worktrees/{seat}")
        else:
            print(f"  ⚠ Seat {seat} initialization warning: {init_res.stderr.strip()}")

    # 3. Init _integration worktree
    int_wt = REPO_ROOT / ".worktrees" / "_integration"
    if not int_wt.exists():
        subprocess.run(["git", "worktree", "add", str(int_wt), "integration"], cwd=REPO_ROOT, check=False)
        print("  ✓ Integration worktree initialized at .worktrees/_integration")
    else:
        print("  ✓ Integration worktree ready")

    print("\n✅ Boss Rig system ready. Run 'boss-orchestrator status' to inspect seats.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = TaskStore()
    print("═══════════════════════════════════════════════════════════════")
    print("                    BOSS RIG SYSTEM STATUS                     ")
    print("═══════════════════════════════════════════════════════════════")

    # Seats
    print("\n[Active Seats]")
    for seat in KNOWN_ENGINES:
        branch = get_seat_branch(seat)
        mode_info = store.get_seat_mode(seat)
        mode_str = mode_info["mode"].value
        if mode_info["mode"] == SeatMode.MANUAL_HOLD:
            mode_str += f" (HELD: {mode_info['reason']})"
        print(f"  • {seat:15} | Branch: {branch:22} | Mode: {mode_str:25} | Runtime: {seat.split('-')[0]}")

    # Merge Lock
    lock_path = REPO_ROOT / ".git" / "openrig-merge.lock"
    lock_status = "LOCKED" if lock_path.exists() else "FREE"
    print(f"\n[Merge Lock]: {lock_status}")

    # Tasks summary
    all_tasks = store.list_tasks()
    counts: dict[str, int] = {}
    for t in all_tasks:
        st = t.state.value
        counts[st] = counts.get(st, 0) + 1

    print("\n[Task Summary]")
    for st_name in [s.value for s in TaskState]:
        cnt = counts.get(st_name, 0)
        print(f"  • {st_name:15}: {cnt}")

    if all_tasks:
        print("\n[Recent Tasks]")
        for t in all_tasks[:5]:
            print(f"  • {t.task_id[:25]:25} | {t.state.value:12} | Goal: {t.goal[:35]}")
    print("═══════════════════════════════════════════════════════════════")
    return 0


def cmd_task_show(args: argparse.Namespace) -> int:
    store = TaskStore()
    task = store.get_task(args.task_id)
    if not task:
        print(f"Error: Task '{args.task_id}' not found in store", file=sys.stderr)
        return 1

    print("═══════════════════════════════════════════════════════════════")
    print(f"TASK RECORD: {task.task_id}")
    print("═══════════════════════════════════════════════════════════════")
    print(f"State:            {task.state.value}")
    print(f"Goal:             {task.goal}")
    print(f"Assigned Engine:  {task.assigned_engine or 'None'}")
    print(f"Assigned Role:    {task.assigned_role or 'None'}")
    print(f"Planner:          {task.planner or 'None'}")
    print(f"Implementer:      {task.implementer or 'None'}")
    print(f"Reviewer:         {task.reviewer or 'None'}")
    print(f"Candidate Commit: {task.candidate_commit or 'None'}")
    print(f"Merge Commit:     {task.merge_commit or 'None'}")
    print(f"Verification:     {' && '.join(task.verification_commands) if task.verification_commands else 'None'}")

    print("\n[Lifecycle Transition History]")
    for h in task.history:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(h.get("timestamp", time.time())))
        print(f"  • {ts} | {h.get('from', '')} -> {h.get('to', '')} | Reason: {h.get('reason', '')}")

    # Audit events
    with store._get_connection() as conn:
        events = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC", (task.task_id,)
        ).fetchall()
        if events:
            print("\n[Audit Events Provenance]")
            for e in events:
                e_ts = time.strftime("%H:%M:%S", time.gmtime(e["created_at"]))
                print(f"  • {e_ts} | seat={e['seat_id']} | role={e['role']} | event={e['event_name']} | status={e['status']}")

    print("═══════════════════════════════════════════════════════════════")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    engine = OrchestrationEngine()
    store = engine.store
    ts = int(time.time())
    task_id = args.task_id or f"T-RUN-{ts}"
    goal = args.goal
    verify_cmd = args.verify or "true"

    available = [
        e for e in KNOWN_ENGINES
        if store.get_seat_mode(e).get("mode") == SeatMode.AUTOMATED
    ]
    if len(available) < 2:
        print(f"❌ Error: At least 2 active automated engines required, found {available}")
        return 1

    tasks = store.list_tasks()
    usage = {e: 0 for e in available}
    for t in tasks:
        if t.planner in usage:
            usage[t.planner] += 1
        if t.implementer in usage:
            usage[t.implementer] += 1
        if t.reviewer in usage:
            usage[t.reviewer] += 1

    sorted_by_usage = sorted(available, key=lambda e: usage[e])

    planner = args.planner or sorted_by_usage[0]
    cand_impls = [e for e in available if e != planner] if len(available) > 1 else available
    cand_impls_sorted = sorted(cand_impls, key=lambda e: usage[e])
    implementer = args.implementer or cand_impls_sorted[0]

    reviewer = args.reviewer or PolicyEngine.select_reviewer(
        implementer=implementer, available_engines=available, planner=planner
    )

    print(f"🎯 Starting task: {task_id}")
    print(f"   Goal: {goal}")
    print(f"   Dynamic Engine Roles -> Planner: {planner} | Implementer: {implementer} | Reviewer: {reviewer}")
    print(f"   Verification: {verify_cmd}")

    task = engine.create_task(task_id, goal=goal, verification_commands=[verify_cmd])
    if task.context and task.context.get("items"):
        print(f"   🧠 Goldfish Context Priming: {task.context['count']} relevant project memories primed")

    def make_activity_cb(seat_name: str, role_name: str):
        def on_activity(info: Dict[str, Any]) -> None:
            evt_name = info.get("event", "heartbeat")
            elapsed = info.get("elapsed_sec") or info.get("data", {}).get("elapsed_sec") or "?"
            if evt_name == "task_started":
                sys.stdout.write(f"\r  ▶ [{seat_name}:{role_name}] started execution...            ")
            else:
                sys.stdout.write(f"\r  ⏳ [{seat_name}:{role_name}] working... ({elapsed}s elapsed)   ")
            sys.stdout.flush()
        return on_activity

    # 1. Plan
    print(f"\n[1/4] Running planning turn on {planner}...")
    engine.assign_role(task_id, engine=planner, role="planner")
    planner_prompt = f"Formulate a plan in plain text for: {goal}. Do not call tools."
    if task.context and task.context.get("summary"):
        planner_prompt += f"\n\n[Project Context from Goldfish Memory]:\n{task.context['summary']}"

    p_res = engine.execute_turn(
        task_id,
        engine=planner,
        role="planner",
        prompt=planner_prompt,
        timeout_sec=120,
        on_activity=make_activity_cb(planner, "planner"),
    )
    sys.stdout.write("\n")
    if p_res.get("status") != "SUCCESS":
        print(f"❌ Planning failed: {p_res}")
        return 1
    print("  ✓ Planning complete")

    # 2. Implement
    print(f"\n[2/4] Running implementation turn on {implementer}...")
    engine.assign_role(task_id, engine=implementer, role="implementer")
    i_res = engine.execute_turn(
        task_id,
        engine=implementer,
        role="implementer",
        prompt=f"Implement: {goal}. Write the code into the file, run git add and git commit, and confirm committed changes.",
        timeout_sec=120,
        on_activity=make_activity_cb(implementer, "implementer"),
    )
    sys.stdout.write("\n")
    if i_res.get("status") != "SUCCESS":
        print(f"❌ Implementation failed: {i_res}")
        return 1
    print(f"  ✓ Implementation complete. Candidate commit pinned: {task.candidate_commit[:8] if task.candidate_commit else 'None'}")

    # 3. Review
    print(f"\n[3/4] Running independent review on {reviewer}...")
    engine.assign_role(task_id, engine=reviewer, role="reviewer")
    r_res = engine.execute_turn(
        task_id,
        engine=reviewer,
        role="reviewer",
        prompt=f"Review candidate commit {task.candidate_commit} for: {goal}. Reply PASS if verified.",
        timeout_sec=120,
        on_activity=make_activity_cb(reviewer, "reviewer"),
    )
    sys.stdout.write("\n")
    if r_res.get("status") != "SUCCESS":
        print(f"❌ Review failed: {r_res}")
        return 1
    print("  ✓ Independent review passed")

    # 4. Gate
    print("\n[4/4] Executing deterministic merge gate...")
    verified = engine.run_deterministic_verification(task_id, seat_id=implementer)
    if verified:
        print(f"🎉 TASK DONE! Merged into integration branch (Commit: {task.merge_commit[:8] if task.merge_commit else 'None'})")
        return 0
    else:
        print("❌ Deterministic gate failed!")
        return 1


def cmd_pause(args: argparse.Namespace) -> int:
    store = TaskStore()
    task = store.block_task(args.task_id, reason=args.reason or "Paused by operator")
    print(f"Task '{task.task_id}' state transitioned to: {task.state.value}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    store = TaskStore()
    task = store.transition_task(args.task_id, TaskState.ACTIVE, reason="Resumed by operator")
    print(f"Task '{task.task_id}' resumed to: {task.state.value}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    store = TaskStore()
    task = store.cancel_task(args.task_id, reason=args.reason or "Cancelled by operator")
    # Release seat if active
    if task.assigned_engine:
        run_gate("reset-seat", task.assigned_engine)
    print(f"Task '{task.task_id}' CANCELLED and seat released")
    return 0


def cmd_seat_hold(args: argparse.Namespace) -> int:
    store = TaskStore()
    if args.seat_id not in KNOWN_ENGINES:
        print(f"❌ Unknown seat: {args.seat_id}. Must be one of {KNOWN_ENGINES}")
        return 1
    info = store.set_seat_mode(
        args.seat_id,
        SeatMode.MANUAL_HOLD,
        reason=args.reason or "Manual operator hold",
        held_by=os.environ.get("USER", "operator"),
    )
    print(f"🛑 Seat '{args.seat_id}' is now on MANUAL_HOLD.")
    print(f"   Reason: {info['reason']}")
    print(f"   Automated dispatch to this seat is blocked until released.")
    return 0


def cmd_seat_release(args: argparse.Namespace) -> int:
    store = TaskStore()
    if args.seat_id not in KNOWN_ENGINES:
        print(f"❌ Unknown seat: {args.seat_id}. Must be one of {KNOWN_ENGINES}")
        return 1
    info = store.set_seat_mode(
        args.seat_id,
        SeatMode.AUTOMATED,
        reason="Released by operator",
        held_by=os.environ.get("USER", "operator"),
    )
    print(f"✅ Seat '{args.seat_id}' released back to AUTOMATED mode.")
    return 0


def cmd_seat_status(args: argparse.Namespace) -> int:
    store = TaskStore()
    print("═══════════════════════════════════════════════════════════════")
    print("                     SEAT OPERATIONAL MODES                    ")
    print("═══════════════════════════════════════════════════════════════")
    for seat in KNOWN_ENGINES:
        info = store.get_seat_mode(seat)
        branch = get_seat_branch(seat)
        mode = info["mode"].value
        reason = info["reason"]
        held_by = info["held_by"]
        print(f"Seat: {seat}")
        print(f"  Branch:  {branch}")
        print(f"  Mode:    {mode}")
        if info["mode"] == SeatMode.MANUAL_HOLD:
            print(f"  Reason:  {reason}")
            print(f"  Held by: {held_by}")
        print()
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    print("🔧 Running deterministic recovery...")
    res = run_gate("recover", "--force" if args.force else "")
    print(res.stdout or res.stderr)
    return res.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="boss-orchestrator",
        description="Boss Rig Multi-Agent Orchestrator CLI",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # up
    subparsers.add_parser("up", help="Initialize OpenRig environment and seats")

    # status
    subparsers.add_parser("status", help="Show system, seat, and task status")

    # seat
    p_seat = subparsers.add_parser("seat", help="Operator seat controls (hold / release / status)")
    p_seat_sub = p_seat.add_subparsers(dest="seat_subcommand", required=True)
    
    p_seat_hold = p_seat_sub.add_parser("hold", help="Place a seat on manual hold")
    p_seat_hold.add_argument("seat_id", help="Seat ID (e.g. codex-1, opencode-1, antigravity-1)")
    p_seat_hold.add_argument("--reason", help="Reason for hold", default="Manual operator hold")

    p_seat_rel = p_seat_sub.add_parser("release", help="Release a seat back to automated scheduling")
    p_seat_rel.add_argument("seat_id", help="Seat ID to release")

    p_seat_sub.add_parser("status", help="Show seat operational modes")

    # task show
    p_show = subparsers.add_parser("task", help="Task management")
    p_show_sub = p_show.add_subparsers(dest="task_subcommand", required=True)
    p_show_cmd = p_show_sub.add_parser("show", help="Show task record and provenance")
    p_show_cmd.add_argument("task_id", help="Task ID")

    # run
    p_run = subparsers.add_parser("run", help="Run a multi-agent feature workflow")
    p_run.add_argument("goal", help="Feature task goal")
    p_run.add_argument("--task-id", help="Custom task ID")
    p_run.add_argument("--verify", help="Verification command")
    p_run.add_argument("--planner", help="Planner seat ID (default: dynamic least-loaded)", default=None)
    p_run.add_argument("--implementer", help="Implementer seat ID (default: dynamic least-loaded)", default=None)
    p_run.add_argument("--reviewer", help="Reviewer seat ID (default: dynamic 3-engine rotation)", default=None)

    # pause
    p_pause = subparsers.add_parser("pause", help="Pause / block a task")
    p_pause.add_argument("task_id", help="Task ID to pause")
    p_pause.add_argument("--reason", help="Pause reason")

    # resume
    p_resume = subparsers.add_parser("resume", help="Resume a task")
    p_resume.add_argument("task_id", help="Task ID to resume")

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop / cancel a task")
    p_stop.add_argument("task_id", help="Task ID to stop")
    p_stop.add_argument("--reason", help="Cancellation reason")

    # recover
    p_rec = subparsers.add_parser("recover", help="Recover locks and reset stale seats")
    p_rec.add_argument("--force", action="store_true", help="Force lock removal")

    args = parser.parse_args()

    if args.subcommand == "up":
        return cmd_up(args)
    elif args.subcommand == "status":
        return cmd_status(args)
    elif args.subcommand == "seat":
        if args.seat_subcommand == "hold":
            return cmd_seat_hold(args)
        elif args.seat_subcommand == "release":
            return cmd_seat_release(args)
        elif args.seat_subcommand == "status":
            return cmd_seat_status(args)
    elif args.subcommand == "task" and args.task_subcommand == "show":
        return cmd_task_show(args)
    elif args.subcommand == "run":
        return cmd_run(args)
    elif args.subcommand == "pause":
        return cmd_pause(args)
    elif args.subcommand == "resume":
        return cmd_resume(args)
    elif args.subcommand == "stop":
        return cmd_stop(args)
    elif args.subcommand == "recover":
        return cmd_recover(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
