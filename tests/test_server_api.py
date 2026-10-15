"""Automated unit tests for Boss Rig local HTTP API server."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

from orchestrator.event_emitter import EventEmitter
from orchestrator.models import TaskSpec
from orchestrator.server import TaskServer
from orchestrator.store import TaskStore
from orchestrator.task_service import TaskService


class TestTaskServerAPI(unittest.TestCase):
    """Test REST API endpoints and authentication."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="server_api_test_")
        self.db_path = Path(self.test_dir) / "tasks.sqlite"
        self.events_file = Path(self.test_dir) / "events.jsonl"
        self.token_file = Path(self.test_dir) / "auth.token"
        self.dummy_repo = Path(self.test_dir) / "dummy_repo"
        self.dummy_repo.mkdir(parents=True)

        self.store = TaskStore(db_path=self.db_path)
        self.emitter = EventEmitter(log_path=self.events_file, enable_notifications=False)
        self.service = TaskService(
            store=self.store,
            event_emitter=self.emitter,
            extra_allowed_repos=[self.dummy_repo],
            dry_run=True,  # Test API safely in dry run
        )
        self.server = TaskServer(
            service=self.service,
            host="127.0.0.1",
            port=0,  # Ephemeral port
            token_path=self.token_file,
        )
        self.server.start()
        self.base_url = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _request(
        self,
        method: str,
        path: str,
        data: Any = None,
        auth: bool = True,
        token: str = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        if auth:
            tok = token or self.server.auth_token
            req.add_header("Authorization", f"Bearer {tok}")

        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_health_endpoint(self) -> None:
        """Health endpoint responds without requiring auth."""
        res = self._request("GET", "/health", auth=False)
        self.assertEqual(res["status"], "ok")

    def test_auth_rejection(self) -> None:
        """Requests without or with invalid token receive HTTP 401."""
        # No auth
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("GET", "/tasks", auth=False)
        self.assertEqual(ctx.exception.code, 401)

        # Invalid token
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("GET", "/tasks", auth=True, token="invalid-token-1234")
        self.assertEqual(ctx.exception.code, 401)

    def test_task_submit_and_query_flow(self) -> None:
        """Submit a task via POST /tasks/submit and query its status."""
        spec = TaskSpec(
            repo=str(self.dummy_repo),
            goal="Test API submission",
            verification_commands=["true"],
            allowed_merge_target="integration",
        )
        spec_hash = spec.compute_hash()

        # 1. Self-asserted approval without approval_record must be rejected with HTTP 403
        unauthorized_payload = {
            "spec": spec.to_canonical_dict(),
            "idempotency_key": "api-idem-unauth",
            "approved": True,
            "approved_hash": spec_hash,
        }
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("POST", "/tasks/submit", data=unauthorized_payload)
        self.assertEqual(ctx.exception.code, 403)

        # 2. Submission with valid operator approval record succeeds
        valid_payload = {
            "spec": spec.to_canonical_dict(),
            "idempotency_key": "api-idem-1",
            "approved": True,
            "approved_hash": spec_hash,
            "approval_record": {
                "spec_id": spec_hash,
                "repo": str(self.dummy_repo),
                "source": "operator_cli",
                "approved_by": "operator",
            },
        }
        res = self._request("POST", "/tasks/submit", data=valid_payload)
        self.assertEqual(res["status"], "DRY_RUN")
        self.assertEqual(res["spec_id"], spec_hash)

        # 3. Query status via GET /tasks/<task_id> for an enrolled task
        stored_tid, _ = self.store.submit_bridge_task(spec, idempotency_key="api-store-query")
        status = self._request("GET", f"/tasks/{stored_tid}")
        self.assertEqual(status["task_id"], stored_tid)
        self.assertEqual(status["spec_id"], spec_hash)


if __name__ == "__main__":
    unittest.main()
