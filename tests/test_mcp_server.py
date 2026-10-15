"""Unit tests for Boss Rig MCP server adapter."""

from __future__ import annotations

import json
import unittest

from orchestrator.mcp_server import McpServer
from orchestrator.task_service import TaskService


class TestMcpServer(unittest.TestCase):
    def setUp(self) -> None:
        self.service = TaskService(dry_run=True)
        self.server = McpServer(service=self.service)

    def test_initialize(self) -> None:
        req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        resp = self.server.handle_request(req)
        self.assertEqual(resp["id"], 1)
        self.assertEqual(resp["result"]["serverInfo"]["name"], "boss-rig-mcp-server")

    def test_tools_list(self) -> None:
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = self.server.handle_request(req)
        tool_names = [t["name"] for t in resp["result"]["tools"]]
        self.assertIn("task_submit", tool_names)
        self.assertIn("task_status", tool_names)
        self.assertIn("task_cancel", tool_names)
        self.assertIn("task_questions", tool_names)
        self.assertIn("task_answer", tool_names)

    def test_tool_call_list_tasks(self) -> None:
        req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "task_list", "arguments": {"limit": 5}},
        }
        resp = self.server.handle_request(req)
        self.assertEqual(resp["id"], 3)
        self.assertFalse(resp.get("result", {}).get("isError", False))


if __name__ == "__main__":
    unittest.main()
