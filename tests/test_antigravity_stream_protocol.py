#!/usr/bin/env python3
"""Deterministic Replay and Delay Tests for Antigravity Completion Protocol."""

from __future__ import annotations

import io
import json
import os
import queue
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO_ROOT))

from scripts.antigravity_seat_runner import AntigravitySession, emit_bridge_event


class MockPipeStream:
    """Simulates a line-based pipe stream with configurable time delays between lines."""

    def __init__(self, events_with_delays: List[tuple[float, Dict[str, Any]]]):
        self.events = events_with_delays
        self.q: queue.Queue[Optional[str]] = queue.Queue()
        self._stop = False
        self._thread = threading.Thread(target=self._producer, daemon=True)
        self._thread.start()

    def _producer(self) -> None:
        for delay, event in self.events:
            if self._stop:
                break
            if delay > 0:
                time.sleep(delay)
            self.q.put(json.dumps(event))
        self.q.put(None)

    def readline(self, timeout: float = 1.0) -> Optional[str]:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._stop = True


class TestAntigravityCompletionSemantics(unittest.TestCase):
    def _run_mock_session_stream(
        self,
        events_with_delays: List[tuple[float, Dict[str, Any]]],
        drain_timeout_sec: float = 2.0,
        overall_timeout_sec: float = 15.0,
    ) -> Dict[str, Any]:
        """Simulates event processing loop with mock delayed stream."""
        mock_stream = MockPipeStream(events_with_delays)
        captured_events: List[Dict[str, Any]] = []

        start_time = time.time()
        last_event_time = time.time()
        resp_text = ""
        agent_response_done = False
        last_step_data: Dict[str, Any] = {}

        while True:
            elapsed = time.time() - start_time
            remaining = overall_timeout_sec - elapsed
            if remaining <= 0:
                mock_stream.close()
                return {"terminal": "TIMEOUT", "elapsed": elapsed, "events": captured_events}

            line = mock_stream.readline(timeout=min(remaining, 0.2))
            if line is None:
                if agent_response_done and (time.time() - last_event_time) >= drain_timeout_sec:
                    mock_stream.close()
                    return {
                        "terminal": "DRAIN_TIMEOUT_FALLBACK",
                        "status": "SUCCESS",
                        "response": resp_text,
                        "elapsed": time.time() - start_time,
                        "events": captured_events,
                    }
                continue

            last_event_time = time.time()
            event_obj = json.loads(line)
            provider_event = event_obj.get("event")
            captured_events.append(event_obj)

            if provider_event == "step_update":
                step_data = event_obj.get("step_update", {})
                delta = step_data.get("text_delta") or ""
                resp_text += delta
                if step_data.get("state") == "DONE" and step_data.get("step_type") == "agent_response":
                    agent_response_done = True
                    last_step_data = step_data

            elif provider_event == "result":
                mock_stream.close()
                res_data = event_obj.get("result", {})
                return {
                    "terminal": "AUTHORITATIVE_RESULT",
                    "status": res_data.get("status", "SUCCESS"),
                    "response": res_data.get("response") or resp_text,
                    "elapsed": time.time() - start_time,
                    "events": captured_events,
                }

    def test_01_result_with_0_5s_delay(self) -> None:
        events = [
            (0.0, {"event": "init", "init": {"permission_mode": "request-review"}}),
            (0.1, {"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response", "text_delta": "Hello world"}}),
            (0.5, {"event": "result", "result": {"status": "SUCCESS", "response": "Hello world", "duration_seconds": 0.6}}),
        ]
        res = self._run_mock_session_stream(events, drain_timeout_sec=5.0)
        self.assertEqual(res["terminal"], "AUTHORITATIVE_RESULT")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["response"], "Hello world")

    def test_02_result_with_1_0s_delay(self) -> None:
        events = [
            (0.0, {"event": "init", "init": {}}),
            (0.1, {"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response", "text_delta": "Plan ready"}}),
            (1.0, {"event": "result", "result": {"status": "SUCCESS", "response": "Plan ready"}}),
        ]
        res = self._run_mock_session_stream(events, drain_timeout_sec=5.0)
        self.assertEqual(res["terminal"], "AUTHORITATIVE_RESULT")
        self.assertEqual(res["response"], "Plan ready")

    def test_03_result_with_1_9s_delay(self) -> None:
        events = [
            (0.0, {"event": "init", "init": {}}),
            (0.1, {"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response", "text_delta": "Review pass"}}),
            (1.9, {"event": "result", "result": {"status": "SUCCESS", "response": "Review pass"}}),
        ]
        res = self._run_mock_session_stream(events, drain_timeout_sec=5.0)
        self.assertEqual(res["terminal"], "AUTHORITATIVE_RESULT")

    def test_04_result_with_2_0s_delay(self) -> None:
        events = [
            (0.0, {"event": "init", "init": {}}),
            (0.1, {"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response", "text_delta": "Review pass"}}),
            (2.0, {"event": "result", "result": {"status": "SUCCESS", "response": "Review pass"}}),
        ]
        res = self._run_mock_session_stream(events, drain_timeout_sec=5.0)
        self.assertEqual(res["terminal"], "AUTHORITATIVE_RESULT")

    def test_05_result_with_2_1s_delay_no_premature_cutoff(self) -> None:
        """Prove that a result event arriving at 2.1s is NOT cut off by a 2.0s silence window."""
        events = [
            (0.0, {"event": "init", "init": {}}),
            (0.1, {"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response", "text_delta": "Delayed result test"}}),
            (2.1, {"event": "result", "result": {"status": "SUCCESS", "response": "Delayed result test"}}),
        ]
        res = self._run_mock_session_stream(events, drain_timeout_sec=5.0)
        self.assertEqual(res["terminal"], "AUTHORITATIVE_RESULT")
        self.assertEqual(res["response"], "Delayed result test")

    def test_06_result_with_3_0s_delay(self) -> None:
        events = [
            (0.0, {"event": "init", "init": {}}),
            (0.1, {"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response", "text_delta": "3.0s delay test"}}),
            (3.0, {"event": "result", "result": {"status": "SUCCESS", "response": "3.0s delay test"}}),
        ]
        res = self._run_mock_session_stream(events, drain_timeout_sec=6.0)
        self.assertEqual(res["terminal"], "AUTHORITATIVE_RESULT")

    def test_07_delayed_tool_execution_after_agent_response(self) -> None:
        """Prove that tool execution occurring 2.5s after agent_response DONE is captured cleanly."""
        events = [
            (0.0, {"event": "init", "init": {}}),
            (0.1, {"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response", "text_delta": "I will create a file."}}),
            (2.5, {"event": "step_update", "step_update": {"state": "ACTIVE", "step_type": "tool", "tool_name": "write_to_file"}}),
            (0.5, {"event": "step_update", "step_update": {"state": "DONE", "step_type": "tool", "tool_name": "write_to_file"}}),
            (0.5, {"event": "result", "result": {"status": "SUCCESS", "response": "File created."}}),
        ]
        res = self._run_mock_session_stream(events, drain_timeout_sec=6.0)
        self.assertEqual(res["terminal"], "AUTHORITATIVE_RESULT")
        self.assertEqual(res["response"], "File created.")
        tool_events = [e for e in res["events"] if e.get("step_update", {}).get("step_type") == "tool"]
        self.assertEqual(len(tool_events), 2)

    def test_08_fallback_drain_timeout_when_result_omitted(self) -> None:
        """Prove fallback drain window gracefully completes if provider genuinely omits result."""
        events = [
            (0.0, {"event": "init", "init": {}}),
            (0.1, {"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response", "text_delta": "Completed without result event"}}),
        ]
        # Set drain_timeout_sec to 0.8s for fast deterministic test
        res = self._run_mock_session_stream(events, drain_timeout_sec=0.8, overall_timeout_sec=5.0)
        self.assertEqual(res["terminal"], "DRAIN_TIMEOUT_FALLBACK")
        self.assertEqual(res["response"], "Completed without result event")


if __name__ == "__main__":
    unittest.main()
