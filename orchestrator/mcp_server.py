"""Model Context Protocol (MCP) Stdio Server adapter for Boss Rig Task Service."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from orchestrator.models import TaskSpec
from orchestrator.task_service import TaskService
from plane.adapters.speaker import read_latest_report

MCP_TOOLS = [
    {
        "name": "task_submit",
        "description": "Submit a scoped, approved task to Boss Rig multi-agent orchestrator.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Absolute path to allowlisted repository"},
                "goal": {"type": "string", "description": "Scoped feature or bugfix goal"},
                "verification_commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Deterministic test commands to verify candidate commit",
                },
                "constraints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Execution constraints",
                },
                "allowed_merge_target": {
                    "type": "string",
                    "enum": ["integration"],
                    "default": "integration",
                    "description": "Target merge branch (strictly 'integration')",
                },
                "approved": {
                    "type": "boolean",
                    "description": "Explicit operator authorization flag (must be true)",
                },
                "approved_hash": {
                    "type": "string",
                    "description": "Computed sha256:<hex> hash of canonical task spec",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Unique key to prevent duplicate dispatches",
                },
            },
            "required": ["repo", "goal", "verification_commands", "approved", "approved_hash"],
        },
    },
    {
        "name": "task_status",
        "description": "Query execution status, active engine, and pending questions for a task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_result",
        "description": "Retrieve terminal verification result and commit SHAs for a task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_list",
        "description": "List existing tasks filtered by state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "state": {"type": "string", "description": "Optional TaskState filter"},
            },
        },
    },
    {
        "name": "task_propose",
        "description": "Propose a scoped task specification without executing it. Returns a reviewable proposal ID and hash requiring explicit human approval.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Absolute path to allowlisted repository"},
                "goal": {"type": "string", "description": "Scoped feature or bugfix goal"},
                "verification_commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Deterministic test commands to verify candidate commit",
                },
                "constraints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Execution constraints",
                },
                "allowed_merge_target": {
                    "type": "string",
                    "enum": ["integration"],
                    "default": "integration",
                },
            },
            "required": ["repo", "goal", "verification_commands"],
        },
    },
    {
        "name": "task_events",
        "description": "Fetch ordered execution outbox events and timeline for a task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "after_seq": {"type": "integer", "default": 0, "description": "Fetch events after this sequence number"},
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_cancel",
        "description": "Cancel an active or queued task safely.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to cancel"},
                "reason": {"type": "string", "description": "Cancellation reason"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_questions",
        "description": "List pending interactive questions requiring operator input.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Optional task filter"},
                "status": {"type": "string", "default": "PENDING"},
            },
        },
    },
    {
        "name": "task_answer",
        "description": "Answer a pending question to resume an agent's turn with strict task binding.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question_id": {"type": "string", "description": "Question ID"},
                "answer": {"type": "string", "description": "Operator answer text"},
                "task_id": {"type": "string", "description": "Task ID for cross-task binding verification"},
                "conversation_id": {"type": "string", "description": "Conversation ID for binding verification"},
            },
            "required": ["question_id", "answer"],
        },
    },
    {
        "name": "get_latest_build_report",
        "description": "Retrieve the latest Shared Plane [SHARED PLANE REPORT] build summary and markdown.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]



class McpServer:
    """JSON-RPC 2.0 Stdio Server exposing Boss Rig Task Service via MCP protocol."""

    def __init__(
        self,
        service: Optional[TaskService] = None,
        reports_dir: Optional[Path] = None,
    ):
        self.service = service or TaskService()
        self.reports_dir = (
            Path(reports_dir)
            if reports_dir is not None
            else (self.service.store.db_path.parent / "reports")
        )

    def handle_request(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        msg_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "boss-rig-mcp-server", "version": "1.0.0"},
                    "capabilities": {"tools": {}},
                },
            }
        elif method == "notifications/initialized":
            return None
        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": MCP_TOOLS},
            }
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments", {})
            return self._call_tool(msg_id, name, args)
        else:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method '{method}' not found"},
            }

    def _call_tool(self, msg_id: Any, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if name == "task_submit":
                spec = TaskSpec(
                    repo=args["repo"],
                    goal=args["goal"],
                    verification_commands=args["verification_commands"],
                    constraints=args.get("constraints", []),
                    allowed_merge_target=args.get("allowed_merge_target", "integration"),
                )
                idempotency_key = args.get("idempotency_key") or spec.compute_hash()
                raw_approval = args.get("approval_record")
                res = self.service.task_submit(
                    spec=spec,
                    idempotency_key=idempotency_key,
                    approved=args.get("approved", True),
                    approved_hash=args.get("approved_hash"),
                    approval_record=raw_approval,
                )
                content = json.dumps(res, indent=2)

            elif name == "task_propose":
                spec = TaskSpec(
                    repo=args["repo"],
                    goal=args["goal"],
                    verification_commands=args["verification_commands"],
                    constraints=args.get("constraints", []),
                    allowed_merge_target=args.get("allowed_merge_target", "integration"),
                )
                res = self.service.propose_task(
                    spec=spec,
                    source="mcp",
                )
                content = json.dumps(res, indent=2)

            elif name == "task_events":
                events = self.service.get_task_events(
                    task_id=args["task_id"],
                    after_seq=args.get("after_seq", 0),
                    limit=args.get("limit", 100),
                )
                content = json.dumps({"task_id": args["task_id"], "events": events}, indent=2)

            elif name == "task_status":
                res = self.service.task_status(args["task_id"])
                content = json.dumps(res, indent=2)

            elif name == "task_result":
                res = self.service.task_result(args["task_id"])
                content = json.dumps(res, indent=2)

            elif name == "task_list":
                res = self.service.task_list(
                    limit=args.get("limit", 20), state=args.get("state")
                )
                content = json.dumps(res, indent=2)

            elif name == "task_cancel":
                cancelled = self.service.task_cancel(
                    args["task_id"], reason=args.get("reason", "")
                )
                content = json.dumps({"task_id": args["task_id"], "cancelled": cancelled})

            elif name == "task_questions":
                res = self.service.task_questions(
                    task_id=args.get("task_id"), status=args.get("status", "PENDING")
                )
                content = json.dumps(res, indent=2)

            elif name == "task_answer":
                res = self.service.task_answer(
                    question_id=args["question_id"],
                    answer=args["answer"],
                    task_id=args.get("task_id"),
                    conversation_id=args.get("conversation_id"),
                )
                content = json.dumps(res, indent=2)

            elif name == "get_latest_build_report":
                res = read_latest_report(self.reports_dir)
                content = json.dumps(res, indent=2)

            else:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Unknown tool '{name}'"},
                }


            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": content}]},
            }

        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {exc}"}],
                    "isError": True,
                },
            }

    def run_stdio(self) -> None:
        """Read JSON-RPC lines from stdin, write responses to stdout."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                resp = self.handle_request(req)
                if resp is not None:
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
            except Exception as exc:
                err = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
                sys.stdout.write(json.dumps(err) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    server = McpServer()
    server.run_stdio()
