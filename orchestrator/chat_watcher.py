"""Read-only Goldfish chat transcript watcher and bridge adapter."""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from orchestrator.models import ApprovalRecord, TaskSpec, compute_spec_hash
from orchestrator.policy import PolicyEngine, PolicyViolation
from orchestrator.task_service import TaskService

APPROVE_PATTERN = re.compile(
    r"@boss-rig\s+approve\s+(?:spec_id=)?(sha256:[a-fA-F0-9]{64})(?:\s+repo=[\"']?([^\"'\r\n]+)[\"']?)?",
    re.IGNORECASE,
)
ANSWER_PATTERN = re.compile(
    r"@boss-rig\s+answer\s+(?:task_id=)?([A-Za-z0-9_\-]+)\s+(?:question_id=)?([A-Za-z0-9_\-]+)\s+(?:answer=)?[\"']?(.+?)[\"']?$",
    re.IGNORECASE | re.DOTALL,
)
SPEC_BLOCK_PATTERN = re.compile(
    r"```(?:boss-rig-spec|json)\s*\n(\{.*?\})\n```",
    re.DOTALL | re.IGNORECASE,
)


def resolve_goldfish_chats_dir() -> Optional[Path]:
    """Find the directory containing Goldfish chat transcripts."""
    if "GOLDFISH_CHATS_DIR" in os.environ:
        p = Path(os.environ["GOLDFISH_CHATS_DIR"])
        if p.exists() and p.is_dir():
            return p

    appdata = os.environ.get("APPDATA")
    if appdata:
        p = Path(appdata) / "goldfish" / "chats"
        if p.exists() and p.is_dir():
            return p

    # WSL fallback
    mnt_users = Path("/mnt/c/Users")
    if mnt_users.exists():
        for user_dir in mnt_users.glob("*"):
            candidate = user_dir / "AppData" / "Roaming" / "goldfish" / "chats"
            if candidate.exists() and candidate.is_dir():
                return candidate

    return None


def strip_quotes_and_codeblocks(text: str) -> str:
    """Strip markdown blockquotes and codeblocks to prevent accidental trigger on examples."""
    lines = []
    in_codeblock = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_codeblock = not in_codeblock
            continue
        if in_codeblock:
            continue
        if stripped.startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines)


class ChatWatcher:
    """Scans Goldfish chat JSON files in read-only mode and dispatches approved tasks."""

    def __init__(
        self,
        service: TaskService,
        chats_dir: Optional[Path] = None,
        dry_run: bool = True,
        poll_interval_sec: float = 0.5,
    ):
        self.service = service
        self.chats_dir = chats_dir or resolve_goldfish_chats_dir()
        self.dry_run = dry_run
        self.poll_interval_sec = poll_interval_sec
        self._running = False
        self._baselined = False

    def baseline_existing_messages(self) -> int:
        """Record all existing chat messages as baseline so they are never executed."""
        if not self.chats_dir or not self.chats_dir.exists():
            return 0

        count = 0
        chat_files = list(self.chats_dir.glob("*.json"))
        for chat_file in chat_files:
            if chat_file.name == "inbox.json":
                continue  # Skip private inbox
            try:
                data = json.loads(chat_file.read_text(encoding="utf-8"))
                chat_id = data.get("id") or chat_file.stem
                messages = data.get("messages", [])
                for msg in messages:
                    msg_id = msg.get("id")
                    if msg_id:
                        claimed = self.service.store.claim_event_durable(
                            event_id=msg_id,
                            chat_id=chat_id,
                            action="BASELINED",
                            status="IGNORED",
                            detail="Message existed prior to watcher startup",
                        )
                        if claimed:
                            count += 1
                if messages:
                    last_msg = messages[-1]
                    last_id = last_msg.get("id", "")
                    last_time = last_msg.get("createdAt") or int(time.time() * 1000)
                    self.service.store.save_cursor(chat_id, last_time, last_id)
            except Exception:
                continue

        self._baselined = True
        return count

    def scan_once(self) -> List[Dict[str, Any]]:
        """Perform one scan of all chat files and process new user messages."""
        if not self.chats_dir or not self.chats_dir.exists():
            return []

        results = []
        chat_files = list(self.chats_dir.glob("*.json"))
        for chat_file in chat_files:
            if chat_file.name == "inbox.json":
                continue

            try:
                raw_text = chat_file.read_text(encoding="utf-8")
                data = json.loads(raw_text)
            except (json.JSONDecodeError, OSError):
                # File is being written concurrently; safely skip this tick
                continue

            chat_id = data.get("id") or chat_file.stem
            messages = data.get("messages", [])
            file_results = self._process_messages_in_chat(chat_id, messages)
            results.extend(file_results)

        return results

    def _process_messages_in_chat(
        self, chat_id: str, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        results = []
        for idx, msg in enumerate(messages):
            msg_id = msg.get("id")
            if not msg_id:
                continue

            # Invariant: Only 'user' messages may trigger actions!
            # Assistant messages are strictly ignored.
            role = msg.get("role")
            if role != "user":
                continue

            # Check if message already claimed
            if self.service.store.is_event_claimed(msg_id):
                evt = self.service.store.get_processed_event(msg_id)
                if evt and evt.get("status") == "CLAIMED":
                    idempotency_key = f"chat-{chat_id}-{msg_id}"
                    if not self.service.store.get_bridge_task_by_idempotency(idempotency_key):
                        # Interrupted crash recovery: proceed to handle and dispatch
                        pass
                    else:
                        continue
                else:
                    continue

            content = msg.get("content", "")
            sanitized = strip_quotes_and_codeblocks(content)

            # Check for @boss-rig approve command
            approve_match = APPROVE_PATTERN.search(sanitized)
            if approve_match:
                res = self._handle_approval(chat_id, msg_id, msg, approve_match, messages[:idx])
                results.append(res)
                continue

            # Check for @boss-rig answer command
            answer_match = ANSWER_PATTERN.search(sanitized)
            if answer_match:
                res = self._handle_answer(chat_id, msg_id, answer_match)
                results.append(res)
                continue

        return results

    def _handle_approval(
        self,
        chat_id: str,
        msg_id: str,
        msg: Dict[str, Any],
        match: re.Match[str],
        preceding_messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        expected_hash = match.group(1).lower()
        explicit_repo = match.group(2)
        created_at = msg.get("createdAt")

        # 1. Scan backwards in preceding messages for a matching spec proposal block
        proposed_spec = None
        for prev in reversed(preceding_messages):
            prev_content = prev.get("content", "")
            for block_match in SPEC_BLOCK_PATTERN.finditer(prev_content):
                try:
                    spec_dict = json.loads(block_match.group(1))
                    candidate_hash = compute_spec_hash(spec_dict)
                    if candidate_hash.lower() == expected_hash:
                        proposed_spec = spec_dict
                        break
                except Exception:
                    continue
            if proposed_spec:
                break

        if not proposed_spec:
            # Proposal block not found or hash mismatch
            self.service.store.claim_event_durable(
                event_id=msg_id,
                chat_id=chat_id,
                message_created_at=created_at,
                spec_id=expected_hash,
                action="APPROVE",
                status="REJECTED_SPEC_NOT_FOUND",
                detail=f"No matching spec proposal block found for hash {expected_hash}",
            )
            return {
                "message_id": msg_id,
                "status": "REJECTED",
                "error": f"No proposal matching spec {expected_hash} found in chat transcript.",
            }

        repo = explicit_repo or proposed_spec.get("repo", "")
        merge_target = proposed_spec.get("allowed_merge_target", "integration")

        # 2. Build TaskSpec
        spec = TaskSpec(
            repo=repo,
            goal=proposed_spec.get("goal", ""),
            verification_commands=proposed_spec.get("verification_commands", []),
            constraints=proposed_spec.get("constraints", []),
            acceptance_criteria=proposed_spec.get("acceptance_criteria", []),
            allowed_merge_target=merge_target,
            timeout_sec=int(proposed_spec.get("timeout_sec", 1800)),
            spec_version=int(proposed_spec.get("spec_version", 1)),
            assigned_agents=proposed_spec.get("assigned_agents") or {
                "planner": "antigravity-1",
                "implementer": "opencode-1",
                "reviewer": "antigravity-1",
            },
        )

        # 3. Validate repository and merge target
        try:
            PolicyEngine.assert_allowed_repository(spec.repo, extra_allowed=self.service.extra_allowed_repos)
            PolicyEngine.assert_allowed_merge_target(spec.allowed_merge_target)
        except PolicyViolation as pv:
            self.service.store.claim_event_durable(
                event_id=msg_id,
                chat_id=chat_id,
                message_created_at=created_at,
                spec_id=expected_hash,
                repo_path=spec.repo,
                action="APPROVE",
                status="REJECTED_POLICY_VIOLATION",
                detail=str(pv),
            )
            return {"message_id": msg_id, "status": "REJECTED", "error": str(pv)}

        # 4. Check dry-run mode
        if self.dry_run:
            self.service.store.claim_event_durable(
                event_id=msg_id,
                chat_id=chat_id,
                message_created_at=created_at,
                spec_id=expected_hash,
                repo_path=spec.repo,
                action="APPROVE",
                status="WOULD_SUBMIT",
                detail="Dry-run mode: task validated but not submitted to orchestrator",
            )
            return {
                "message_id": msg_id,
                "spec_id": expected_hash,
                "status": "WOULD_SUBMIT",
                "goal": spec.goal,
                "repo": spec.repo,
            }

        # 5. Live submission
        idempotency_key = f"chat-{chat_id}-{msg_id}"
        self.service.store.claim_event_durable(
            event_id=msg_id,
            chat_id=chat_id,
            message_created_at=created_at,
            spec_id=expected_hash,
            repo_path=spec.repo,
            action="APPROVE",
            status="CLAIMED",
        )
        approval_rec = ApprovalRecord(
            spec_id=expected_hash,
            repo=spec.repo,
            source="goldfish_chat",
            conversation_id=chat_id,
            message_id=msg_id,
            timestamp=created_at / 1000.0 if created_at else time.time(),
        )
        submit_res = self.service.task_submit(
            spec=spec,
            idempotency_key=idempotency_key,
            approved=True,
            approved_hash=expected_hash,
            conversation_id=chat_id,
            approval_record=approval_rec,
        )
        task_id = submit_res.get("task_id")
        self.service.store.update_processed_event(msg_id, status="SUBMITTED", task_id=task_id)
        return {"message_id": msg_id, "status": "SUBMITTED", "result": submit_res}

    def _handle_answer(
        self, chat_id: str, msg_id: str, match: re.Match[str]
    ) -> Dict[str, Any]:
        task_id = match.group(1).strip()
        question_id = match.group(2).strip()
        answer_text = match.group(3).strip()

        self.service.store.claim_event_durable(
            event_id=msg_id,
            chat_id=chat_id,
            task_id=task_id,
            action="ANSWER",
            status="ANSWERED",
            detail=f"Answer provided for question {question_id}",
        )

        try:
            res = self.service.task_answer(question_id, answer_text)
            return {"message_id": msg_id, "status": "ANSWER_ACCEPTED", "result": res}
        except Exception as exc:
            return {"message_id": msg_id, "status": "ANSWER_REJECTED", "error": str(exc)}
