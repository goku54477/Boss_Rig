"""Local loopback HTTP API server for Boss Rig Task Service."""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from orchestrator.models import TaskSpec
from orchestrator.task_service import (
    ApprovalVerificationError,
    BridgeError,
    ScopeExpansionError,
    TaskNotFoundError,
    TaskService,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TOKEN_FILE = REPO_ROOT / ".openrig-bridge" / "auth.token"


def get_or_create_auth_token(token_path: Optional[Path] = None) -> str:
    """Read existing bearer token or generate a new cryptographically secure one."""
    path = token_path or DEFAULT_TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            token = path.read_text(encoding="utf-8").strip()
            if len(token) >= 32:
                return token
        except Exception:
            pass

    token = secrets.token_hex(32)
    path.write_text(token, encoding="utf-8")
    try:
        # Set file permissions to owner only (chmod 600 on POSIX)
        os.chmod(path, 0o600)
    except Exception:
        pass
    return token


class TaskServiceHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler mapping REST endpoints to TaskService methods."""

    service: TaskService
    auth_token: str

    def _send_json(self, status_code: int, data: Dict[str, Any]) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _check_auth(self) -> bool:
        auth_header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.auth_token}"
        if not auth_header or not secrets.compare_digest(auth_header.strip(), expected):
            self._send_json(401, {"error": "Unauthorized", "message": "Valid Bearer token required"})
            return False
        return True

    def _read_json_body(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw)
        except Exception as exc:
            self._send_json(400, {"error": "InvalidJSON", "message": str(exc)})
            return None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)

        # Health endpoint can be public or authenticated
        if path == "/health":
            self._send_json(200, {"status": "ok", "service": "boss-rig-task-service"})
            return

        if not self._check_auth():
            return

        try:
            if path == "/tasks":
                limit = int(query.get("limit", [50])[0])
                state = query.get("state", [None])[0]
                tasks = self.service.task_list(limit=limit, state=state)
                self._send_json(200, {"tasks": tasks})
            elif path.startswith("/tasks/") and path.endswith("/result"):
                task_id = path.split("/")[2]
                res = self.service.task_result(task_id)
                self._send_json(200, res)
            elif path.startswith("/tasks/"):
                task_id = path.split("/")[2]
                status = self.service.task_status(task_id)
                self._send_json(200, status)
            elif path == "/questions":
                task_id = query.get("task_id", [None])[0]
                q_status = query.get("status", ["PENDING"])[0]
                questions = self.service.task_questions(task_id=task_id, status=q_status)
                self._send_json(200, {"questions": questions})
            else:
                self._send_json(404, {"error": "NotFound", "path": path})
        except TaskNotFoundError as exc:
            self._send_json(404, {"error": "TaskNotFound", "message": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": "InternalError", "message": str(exc)})

    def do_POST(self) -> None:
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        body = self._read_json_body()
        if body is None:
            return

        try:
            if path == "/tasks/submit":
                raw_spec = body.get("spec")
                if not raw_spec or not isinstance(raw_spec, dict):
                    self._send_json(400, {"error": "MissingSpec", "message": "'spec' object required"})
                    return

                spec = TaskSpec(
                    repo=raw_spec.get("repo", ""),
                    goal=raw_spec.get("goal", ""),
                    verification_commands=raw_spec.get("verification_commands", []),
                    constraints=raw_spec.get("constraints", []),
                    acceptance_criteria=raw_spec.get("acceptance_criteria", []),
                    allowed_merge_target=raw_spec.get("allowed_merge_target", "integration"),
                    timeout_sec=int(raw_spec.get("timeout_sec", 1800)),
                    spec_version=int(raw_spec.get("spec_version", 1)),
                    assigned_agents=raw_spec.get("assigned_agents") or {
                        "planner": "antigravity-1",
                        "implementer": "opencode-1",
                        "reviewer": "antigravity-1",
                    },
                )
                idempotency_key = body.get("idempotency_key") or spec.compute_hash()
                approved = bool(body.get("approved", True))
                approved_hash = body.get("approved_hash")
                conversation_id = body.get("conversation_id")

                res = self.service.task_submit(
                    spec=spec,
                    idempotency_key=idempotency_key,
                    approved=approved,
                    approved_hash=approved_hash,
                    conversation_id=conversation_id,
                )
                self._send_json(201 if res.get("is_new") else 200, res)

            elif path.startswith("/tasks/") and path.endswith("/cancel"):
                task_id = path.split("/")[2]
                reason = body.get("reason", "")
                cancelled = self.service.task_cancel(task_id, reason=reason)
                self._send_json(200, {"task_id": task_id, "cancelled": cancelled})

            elif path.startswith("/questions/") and path.endswith("/answer"):
                question_id = path.split("/")[2]
                answer = body.get("answer", "")
                if not answer:
                    self._send_json(400, {"error": "MissingAnswer", "message": "'answer' string required"})
                    return
                res = self.service.task_answer(question_id, answer)
                self._send_json(200, res)

            else:
                self._send_json(404, {"error": "NotFound", "path": path})

        except ApprovalVerificationError as exc:
            self._send_json(403, {"error": "ApprovalFailed", "message": str(exc)})
        except ScopeExpansionError as exc:
            self._send_json(400, {"error": "ScopeExpansion", "message": str(exc)})
        except BridgeError as exc:
            self._send_json(400, {"error": "BridgeError", "message": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": "InternalError", "message": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default HTTP stderr noise in normal operation."""
        pass


class TaskServer:
    """Manages the background HTTP server lifecycle."""

    def __init__(
        self,
        service: TaskService,
        host: str = "127.0.0.1",
        port: int = 8383,
        token_path: Optional[Path] = None,
    ):
        self.service = service
        self.host = host
        self.port = port
        self.auth_token = get_or_create_auth_token(token_path)

        handler_class = type(
            "ConfiguredHandler",
            (TaskServiceHandler,),
            {"service": self.service, "auth_token": self.auth_token},
        )
        self.httpd = HTTPServer((self.host, self.port), handler_class)
        self.port = self.httpd.server_port
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start server in background thread."""
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="task-server")
        self.thread.start()

    def stop(self) -> None:
        """Stop server and wait for thread to join."""
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)
