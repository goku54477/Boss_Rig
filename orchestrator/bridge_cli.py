"""Operator CLI for Boss Rig Bridge Service and Chat Watcher."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from orchestrator.chat_watcher import ChatWatcher, resolve_goldfish_chats_dir
from orchestrator.event_emitter import EventEmitter
from orchestrator.models import TaskSpec
from orchestrator.server import TaskServer, get_or_create_auth_token
from orchestrator.task_service import TaskService


def cmd_serve(args: argparse.Namespace) -> int:
    service = TaskService(dry_run=args.dry_run)
    server = TaskServer(service=service, host=args.host, port=args.port)
    print(f"🚀 Starting Boss Rig Task Server on http://{args.host}:{server.port}")
    print(f"🔑 Auth Token: {server.auth_token}")
    print("Press Ctrl+C to terminate.")
    server.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down Task Server...")
        server.stop()
        print("✓ Server stopped cleanly.")
        return 0


def cmd_watch(args: argparse.Namespace) -> int:
    dry_run = not args.live
    chats_dir = Path(args.chats_dir) if args.chats_dir else resolve_goldfish_chats_dir()

    print("🔍 Initializing Goldfish Chat Watcher...")
    print(f"   Directory: {chats_dir}")
    print(f"   Mode: {'[DRY RUN] (No agents launched)' if dry_run else '[LIVE EXECUTION]'}")

    if not chats_dir or not chats_dir.exists():
        print(f"❌ Error: Goldfish chats directory not found: {chats_dir}")
        return 1

    service = TaskService(dry_run=dry_run)
    watcher = ChatWatcher(service=service, chats_dir=chats_dir, dry_run=dry_run, poll_interval_sec=args.interval)

    baselined = watcher.baseline_existing_messages()
    print(f"   ✓ Baselined {baselined} historical messages (will not be executed).")
    print("📡 Watching for @boss-rig commands in active transcripts... (Press Ctrl+C to stop)")

    try:
        while True:
            results = watcher.scan_once()
            for r in results:
                status = r.get("status")
                msg_id = r.get("message_id")
                if status == "WOULD_SUBMIT":
                    print(f"\n[DRY RUN] Would submit task for spec {r.get('spec_id')}:")
                    print(f"   Goal: {r.get('goal')}")
                    print(f"   Repo: {r.get('repo')}")
                elif status == "SUBMITTED":
                    print(f"\n[LIVE] Dispatched task {r.get('result', {}).get('task_id')} for message {msg_id}")
                elif status == "REJECTED":
                    print(f"\n[REJECTED] Message {msg_id} failed verification: {r.get('error')}")
                elif status == "ANSWER_ACCEPTED":
                    print(f"\n[ANSWER] Accepted answer for message {msg_id}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n🛑 Stopping Chat Watcher...")
        return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    service = TaskService()
    tasks = service.task_list(limit=args.limit, state=args.state)
    if not tasks:
        print("No tasks found.")
        return 0
    print(f"{'TASK ID':<24} {'STATE':<14} {'MERGE TARGET':<14} {'SPEC ID':<20} GOAL")
    print("-" * 90)
    for t in tasks:
        print(f"{t['task_id']:<24} {t['state']:<14} {t.get('allowed_merge_target',''):<14} {t.get('spec_id','')[:16]:<20} {t.get('goal','')[:40]}")
    return 0


def cmd_questions(args: argparse.Namespace) -> int:
    service = TaskService()
    questions = service.task_questions(task_id=args.task_id, status=args.status)
    if not questions:
        print("No questions found matching criteria.")
        return 0
    print(f"{'QUESTION ID':<18} {'TASK ID':<22} {'STATUS':<12} PROMPT")
    print("-" * 80)
    for q in questions:
        print(f"{q['question_id']:<18} {q['task_id']:<22} {q['status']:<12} {q['prompt'][:50]}")
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    service = TaskService()
    try:
        res = service.task_answer(args.question_id, args.answer)
        print(f"✓ Answer recorded for {args.question_id}: status={res.get('status')}")
        return 0
    except Exception as exc:
        print(f"❌ Error: {exc}")
        return 1


def cmd_token(args: argparse.Namespace) -> int:
    token = get_or_create_auth_token()
    print(f"Boss Rig Bearer Token: {token}")
    return 0


def cmd_ask_question(args: argparse.Namespace) -> int:
    service = TaskService()
    task_id = args.task_id
    if not task_id:
        active_tasks = service.task_list(state="ACTIVE")
        if active_tasks:
            task_id = active_tasks[0]["task_id"]
        else:
            print("Error: No active task found in store. Pass --task-id.", file=sys.stderr)
            return 1
    try:
        ans = service.ask_user_question(task_id, args.prompt, timeout_sec=args.timeout)
        print(ans)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Boss Rig Bridge & Watcher CLI")
    subparsers = parser.add_subparsers(dest="subcommand", help="Bridge commands")

    # serve
    p_serve = subparsers.add_parser("serve", help="Run local HTTP task service")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8383, help="Bind port (default: 8383)")
    p_serve.add_argument("--dry-run", action="store_true", help="Run in dry-run mode")

    # watch
    p_watch = subparsers.add_parser("watch", help="Watch Goldfish chat transcripts")
    p_watch.add_argument("--chats-dir", help="Path to Goldfish chats directory")
    p_watch.add_argument("--live", action="store_true", help="Enable live execution (default is dry-run)")
    p_watch.add_argument("--interval", type=float, default=0.5, help="Poll interval in seconds (default: 0.5)")

    # tasks
    p_tasks = subparsers.add_parser("tasks", help="List tasks")
    p_tasks.add_argument("--limit", type=int, default=50, help="Max tasks to show")
    p_tasks.add_argument("--state", help="Filter by state")

    # questions
    p_q = subparsers.add_parser("questions", help="List questions")
    p_q.add_argument("--task-id", help="Filter by task ID")
    p_q.add_argument("--status", default="PENDING", help="Filter by status (default: PENDING)")

    # ask-question
    p_ask = subparsers.add_parser("ask-question", help="Ask a question from agent to operator and wait for answer")
    p_ask.add_argument("prompt", help="Question text for operator")
    p_ask.add_argument("--task-id", help="Associated Task ID (defaults to active task)")
    p_ask.add_argument("--timeout", type=float, default=300.0, help="Wait timeout in seconds (default: 300)")

    # answer
    p_ans = subparsers.add_parser("answer", help="Answer a pending question")
    p_ans.add_argument("question_id", help="Question ID to answer")
    p_ans.add_argument("answer", help="Answer text")

    # token
    subparsers.add_parser("token", help="Show local bearer auth token")

    args = parser.parse_args(argv)
    if args.subcommand == "serve":
        return cmd_serve(args)
    elif args.subcommand == "watch":
        return cmd_watch(args)
    elif args.subcommand == "tasks":
        return cmd_tasks(args)
    elif args.subcommand == "questions":
        return cmd_questions(args)
    elif args.subcommand == "ask-question":
        return cmd_ask_question(args)
    elif args.subcommand == "answer":
        return cmd_answer(args)
    elif args.subcommand == "token":
        return cmd_token(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
