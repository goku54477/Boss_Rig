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
from plane.adapters.speaker import read_latest_report

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
    reports_dir: Path

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

        # Simple, standalone local operator inbox dashboard (serves HTML/JS on localhost)
        if path == "/inbox" or path == "":
            html = self._render_inbox_html()
            payload = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
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
            elif path.startswith("/tasks/") and path.endswith("/events"):
                task_id = path.split("/")[2]
                after_seq = int(query.get("after_seq", [0])[0])
                limit = int(query.get("limit", [100])[0])
                events = self.service.get_task_events(task_id, after_seq=after_seq, limit=limit)
                self._send_json(200, {"task_id": task_id, "events": events})
            elif path.startswith("/tasks/"):
                task_id = path.split("/")[2]
                status = self.service.task_status(task_id)
                self._send_json(200, status)
            elif path == "/proposals":
                status = query.get("status", [None])[0]
                proposals = self.service.list_proposals(status=status)
                self._send_json(200, {"proposals": proposals})
            elif path.startswith("/proposals/"):
                proposal_id = path.split("/")[2]
                prop = self.service.get_proposal(proposal_id)
                if not prop:
                    self._send_json(404, {"error": "ProposalNotFound", "message": f"Proposal '{proposal_id}' not found"})
                    return
                self._send_json(200, prop)
            elif path == "/questions":
                task_id = query.get("task_id", [None])[0]
                q_status = query.get("status", ["PENDING"])[0]
                questions = self.service.task_questions(task_id=task_id, status=q_status)
                self._send_json(200, {"questions": questions})
            elif path == "/reports/latest":
                latest = read_latest_report(self.reports_dir)
                self._send_json(200, latest)
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
                raw_approval = body.get("approval_record")

                res = self.service.task_submit(
                    spec=spec,
                    idempotency_key=idempotency_key,
                    approved=approved,
                    approved_hash=approved_hash,
                    conversation_id=conversation_id,
                    approval_record=raw_approval,
                )
                self._send_json(201 if res.get("is_new") else 200, res)

            elif path == "/proposals/propose":
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
                res = self.service.propose_task(
                    spec=spec,
                    source=body.get("source", "api"),
                    conversation_id=body.get("conversation_id"),
                    message_id=body.get("message_id"),
                )
                self._send_json(201, res)

            elif path.startswith("/proposals/") and path.endswith("/approve"):
                proposal_id = path.split("/")[2]
                prop = self.service.get_proposal(proposal_id)
                if not prop:
                    self._send_json(404, {"error": "ProposalNotFound", "message": f"Proposal '{proposal_id}' not found"})
                    return
                spec_dict = prop["spec"]
                spec = TaskSpec(
                    repo=spec_dict.get("repo", ""),
                    goal=spec_dict.get("goal", ""),
                    verification_commands=spec_dict.get("verification_commands", []),
                    constraints=spec_dict.get("constraints", []),
                    acceptance_criteria=spec_dict.get("acceptance_criteria", []),
                    allowed_merge_target=spec_dict.get("allowed_merge_target", "integration"),
                    timeout_sec=int(spec_dict.get("timeout_sec", 1800)),
                    spec_version=int(spec_dict.get("spec_version", 1)),
                    assigned_agents=spec_dict.get("assigned_agents") or {
                        "planner": "antigravity-1",
                        "implementer": "opencode-1",
                        "reviewer": "antigravity-1",
                    },
                )
                # Create cryptographic approval token and nonce
                from orchestrator.security import create_approval_token
                nonce = secrets.token_hex(16)
                token = create_approval_token(
                    spec_id=prop["spec_id"],
                    repo=prop["repo"],
                    conversation_id=prop.get("conversation_id") or "inbox",
                    message_id=prop.get("message_id") or proposal_id,
                    secret_key=get_or_create_auth_token(),
                    nonce=nonce,
                    allowed_actions=["execute_local"],
                )
                approval_rec = {
                    "spec_id": prop["spec_id"],
                    "repo": prop["repo"],
                    "source": "local_web",
                    "conversation_id": prop.get("conversation_id") or "inbox",
                    "message_id": prop.get("message_id") or proposal_id,
                    "approval_token": token,
                    "nonce": nonce,
                    "allowed_actions": ["execute_local"],
                }
                submit_res = self.service.task_submit(
                    spec=spec,
                    idempotency_key=f"prop-{proposal_id}",
                    approved=True,
                    approved_hash=prop["spec_id"],
                    conversation_id=prop.get("conversation_id"),
                    approval_record=approval_rec,
                )
                self.service.store.update_proposal_status(proposal_id, status="APPROVED", approval_token=token)
                self._send_json(200, submit_res)

            elif path.startswith("/proposals/") and path.endswith("/reject"):
                proposal_id = path.split("/")[2]
                reason = body.get("reason", "Rejected by operator")
                self.service.reject_proposal(proposal_id, reason=reason)
                self._send_json(200, {"proposal_id": proposal_id, "status": "REJECTED", "reason": reason})

            elif path.startswith("/tasks/") and path.endswith("/cancel"):
                task_id = path.split("/")[2]
                reason = body.get("reason", "")
                cancelled = self.service.task_cancel(task_id, reason=reason)
                self._send_json(200, {"task_id": task_id, "cancelled": cancelled})

            elif path.startswith("/questions/") and path.endswith("/answer"):
                question_id = path.split("/")[2]
                answer = body.get("answer", "")
                task_id = body.get("task_id")
                conversation_id = body.get("conversation_id")
                if not answer:
                    self._send_json(400, {"error": "MissingAnswer", "message": "'answer' string required"})
                    return
                res = self.service.task_answer(
                    question_id=question_id,
                    answer=answer,
                    task_id=task_id,
                    conversation_id=conversation_id,
                )
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

    def _render_inbox_html(self) -> str:
        """Render self-contained local operator inbox UI."""
        return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Boss Rig — Local Operator Inbox</title>
<style>
  :root { --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #f8fafc; --muted: #94a3b8; --accent: #38bdf8; --green: #22c55e; --red: #ef4444; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 24px; line-height: 1.5; }
  h1, h2, h3 { margin: 0 0 12px; }
  .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; }
  .badge { background: #0369a1; color: #e0f2fe; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 20px; }
  .item { border-left: 3px solid var(--accent); background: rgba(255,255,255,0.02); padding: 12px 16px; margin-bottom: 12px; border-radius: 0 6px 6px 0; }
  .item.pending { border-left-color: #f59e0b; }
  .item.done { border-left-color: var(--green); }
  .item.error { border-left-color: var(--red); }
  .btn { background: #0284c7; color: white; border: none; padding: 6px 14px; border-radius: 4px; cursor: pointer; font-weight: 500; font-size: 13px; }
  .btn:hover { background: #0369a1; }
  .btn-danger { background: #dc2626; }
  .btn-danger:hover { background: #b91c1c; }
  .actions { display: flex; gap: 8px; margin-top: 10px; }
  pre { background: #090d16; padding: 10px; border-radius: 4px; overflow-x: auto; font-size: 12px; color: #a5f3fc; }
  input, textarea { width: 100%; box-sizing: border-box; background: #090d16; border: 1px solid var(--border); color: white; padding: 8px; border-radius: 4px; margin-top: 4px; margin-bottom: 8px; font-family: monospace; }
  .auth-box { background: #090d16; padding: 8px 12px; border-radius: 6px; font-family: monospace; font-size: 12px; }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>Boss Rig — Operator Control Inbox</h1>
    <div style="color: var(--muted); font-size: 13px;">Authenticated Local-Only Bridge Control Boundary</div>
  </div>
  <div class="auth-box">
    <span>Bearer Auth Configured</span>
  </div>
</div>

<div class="grid">
  <div>
    <div class="card">
      <h2>Pending Task Proposals</h2>
      <div id="proposals-list"><div style="color: var(--muted)">Loading proposals...</div></div>
    </div>

    <div class="card">
      <h2>Pending Questions</h2>
      <div id="questions-list"><div style="color: var(--muted)">Loading questions...</div></div>
    </div>
  </div>

  <div>
    <div class="card">
      <h2>Active & Recent Tasks</h2>
      <div id="tasks-list"><div style="color: var(--muted)">Loading tasks...</div></div>
    </div>

    <div class="card">
      <h2>Task Timeline & Evidence</h2>
      <div id="timeline-box"><div style="color: var(--muted)">Select a task to inspect sequenced outbox events and verification evidence.</div></div>
    </div>

    <div class="card">
      <h2>Latest Shared Plane Report</h2>
      <div id="latest-report-box"><div style="color: var(--muted)">Loading latest build report...</div></div>
    </div>
  </div>
</div>

<script>
let authToken = localStorage.getItem("boss_rig_token") || "";
if (!authToken) {
  authToken = prompt("Enter Boss Rig Bearer Token (found in .openrig-bridge/auth.token):") || "";
  if (authToken) localStorage.setItem("boss_rig_token", authToken);
}

function authHeaders() {
  return { "Authorization": "Bearer " + authToken, "Content-Type": "application/json" };
}

async function refreshAll() {
  try {
    const pRes = await fetch("/proposals?status=PENDING_APPROVAL", { headers: authHeaders() });
    if (pRes.ok) {
      const pData = await pRes.json();
      renderProposals(pData.proposals || []);
    }

    const qRes = await fetch("/questions?status=PENDING", { headers: authHeaders() });
    if (qRes.ok) {
      const qData = await qRes.json();
      renderQuestions(qData.questions || []);
    }

    const tRes = await fetch("/tasks?limit=10", { headers: authHeaders() });
    if (tRes.ok) {
      const tData = await tRes.json();
      renderTasks(tData.tasks || []);
    }

    const rRes = await fetch("/reports/latest", { headers: authHeaders() });
    if (rRes.ok) {
      const rData = await rRes.json();
      renderLatestReport(rData);
    }
  } catch (err) {
    console.error("Refresh failed", err);
  }
}

function renderLatestReport(data) {
  const box = document.getElementById("latest-report-box");
  if (!box) return;
  if (!data || !data.available) {
    box.innerHTML = "<div style='color: var(--muted)'>No Shared Plane build report recorded yet.</div>";
    return;
  }
  const md = data.markdown || JSON.stringify(data.report || {}, null, 2);
  box.innerHTML = `<pre>${escapeHtml(md)}</pre>`;
}

function renderProposals(proposals) {
  const container = document.getElementById("proposals-list");
  if (!proposals.length) {
    container.innerHTML = "<div style='color: var(--muted)'>No pending proposals requiring human approval.</div>";
    return;
  }
  container.innerHTML = proposals.map(p => `
    <div class="item pending">
      <div style="display:flex; justify-content:space-between;">
        <strong>${escapeHtml(p.proposal_id)}</strong>
        <span class="badge">${escapeHtml(p.spec_id.substring(0, 16))}...</span>
      </div>
      <div style="margin: 6px 0; font-size: 13px;">${escapeHtml(p.goal)}</div>
      <div style="font-size: 11px; color: var(--muted);">Repo: ${escapeHtml(p.repo)}</div>
      <div class="actions">
        <button class="btn" onclick="approveProposal('${p.proposal_id}')">Approve & Dispatch</button>
        <button class="btn btn-danger" onclick="rejectProposal('${p.proposal_id}')">Reject</button>
      </div>
    </div>
  `).join("");
}

function renderQuestions(questions) {
  const container = document.getElementById("questions-list");
  if (!questions.length) {
    container.innerHTML = "<div style='color: var(--muted)'>No waiting questions from agents.</div>";
    return;
  }
  container.innerHTML = questions.map(q => `
    <div class="item pending">
      <div><strong>Question ${escapeHtml(q.question_id)}</strong> (Task: ${escapeHtml(q.task_id)})</div>
      <div style="margin: 8px 0; font-size: 13px; color: #fed7aa;">${escapeHtml(q.prompt)}</div>
      <input type="text" id="ans-${q.question_id}" placeholder="Type answer...">
      <div class="actions">
        <button class="btn" onclick="answerQuestion('${q.question_id}', '${q.task_id}', '${q.conversation_id || ''}')">Submit Answer</button>
      </div>
    </div>
  `).join("");
}

function renderTasks(tasks) {
  const container = document.getElementById("tasks-list");
  if (!tasks.length) {
    container.innerHTML = "<div style='color: var(--muted)'>No tasks recorded yet.</div>";
    return;
  }
  container.innerHTML = tasks.map(t => `
    <div class="item ${t.state === 'DONE' ? 'done' : t.state === 'REVIEW_FAILED' ? 'error' : ''}">
      <div style="display:flex; justify-content:space-between;">
        <strong>${escapeHtml(t.task_id)}</strong>
        <span class="badge">${escapeHtml(t.state)}</span>
      </div>
      <div style="margin: 6px 0; font-size: 13px;">${escapeHtml(t.goal)}</div>
      <div class="actions">
        <button class="btn" onclick="inspectTask('${t.task_id}')">Inspect Timeline & Evidence</button>
        ${t.state === 'ACTIVE' || t.state === 'QUEUED' ? `<button class="btn btn-danger" onclick="cancelTask('${t.task_id}')">Cancel</button>` : ''}
      </div>
    </div>
  `).join("");
}

async function approveProposal(propId) {
  const res = await fetch(`/proposals/${propId}/approve`, { method: "POST", headers: authHeaders(), body: "{}" });
  if (res.ok) { refreshAll(); } else { alert("Approval failed"); }
}

async function rejectProposal(propId) {
  const res = await fetch(`/proposals/${propId}/reject`, { method: "POST", headers: authHeaders(), body: JSON.stringify({ reason: "Rejected by operator" }) });
  if (res.ok) { refreshAll(); } else { alert("Rejection failed"); }
}

async function answerQuestion(qId, tId, cId) {
  const inp = document.getElementById(`ans-${qId}`);
  const ans = inp ? inp.value.trim() : "";
  if (!ans) return;
  const res = await fetch(`/questions/${qId}/answer`, {
    method: "POST", headers: authHeaders(),
    body: JSON.stringify({ answer: ans, task_id: tId, conversation_id: cId })
  });
  if (res.ok) { refreshAll(); } else { alert("Answer submission failed"); }
}

async function cancelTask(taskId) {
  const res = await fetch(`/tasks/${taskId}/cancel`, { method: "POST", headers: authHeaders(), body: JSON.stringify({ reason: "Cancelled by operator" }) });
  if (res.ok) { refreshAll(); }
}

async function inspectTask(taskId) {
  const box = document.getElementById("timeline-box");
  box.innerHTML = "<div style='color: var(--muted)'>Loading timeline...</div>";
  const rRes = await fetch(`/tasks/${taskId}/result`, { headers: authHeaders() });
  const eRes = await fetch(`/tasks/${taskId}/events?after_seq=0&limit=100`, { headers: authHeaders() });
  if (rRes.ok && eRes.ok) {
    const rData = await rRes.json();
    const eData = await eRes.json();
    box.innerHTML = `
      <h3>Task: ${escapeHtml(taskId)}</h3>
      <div><strong>Status:</strong> ${escapeHtml(rData.state)}</div>
      ${rData.candidate_commit ? `<div><strong>Candidate Commit:</strong> <code>${escapeHtml(rData.candidate_commit)}</code></div>` : ''}
      ${rData.merge_commit ? `<div><strong>Merge Commit:</strong> <code>${escapeHtml(rData.merge_commit)}</code></div>` : ''}
      <h4>Execution Outbox Events (${(eData.events || []).length}):</h4>
      <pre>${escapeHtml(JSON.stringify(eData.events, null, 2))}</pre>
      ${rData.test_evidence ? `<h4>Verification Evidence:</h4><pre>${escapeHtml(JSON.stringify(rData.test_evidence, null, 2))}</pre>` : ''}
    `;
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

refreshAll();
setInterval(refreshAll, 3000);
</script>
</body>
</html>"""



class TaskServer:
    """Manages the background HTTP server lifecycle."""

    def __init__(
        self,
        service: TaskService,
        host: str = "127.0.0.1",
        port: int = 8383,
        token_path: Optional[Path] = None,
        reports_dir: Optional[Path] = None,
    ):
        self.service = service
        self.host = host
        self.port = port
        self.auth_token = get_or_create_auth_token(token_path)
        self.reports_dir = (
            Path(reports_dir)
            if reports_dir is not None
            else (self.service.store.db_path.parent / "reports")
        )

        handler_class = type(
            "ConfiguredHandler",
            (TaskServiceHandler,),
            {
                "service": self.service,
                "auth_token": self.auth_token,
                "reports_dir": self.reports_dir,
            },
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
