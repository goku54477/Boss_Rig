# Antigravity Stream Completion Protocol & Semantics

**Author:** Boss Rig Engineering  
**Status:** Authoritative Protocol Specification  
**Version:** v0.1.0  
**Evidence Source:** Real log analysis (`.openrig-antigravity-seat.log`) & Replay suite (`tests/test_antigravity_stream_protocol.py`)

---

## 1. Executive Protocol Finding

In Antigravity (`agy`) version 1.3 stream-json mode:
1. **The `result` event is the AUTHORITATIVE terminal event of a turn.**
2. `step_update(state="DONE", step_type="agent_response")` signals only that the text generation turn has finished. It is frequently followed by tool actions (`step_update(state="ACTIVE", step_type="tool")`) or inter-event latency before the final summary.
3. Arbitrary silence heuristics (e.g. 800ms or 2.0s) risk cutting off legitimate follow-up tool executions or delayed provider responses.
4. In Boss Rig, the normal completion path relies strictly on the `result` event.
5. A configurable fallback drain timeout (`ANTIGRAVITY_DRAIN_TIMEOUT_SEC`, default: 15.0s) exists strictly as safety defense-in-depth in case an external provider drops the stream without emitting `result`.

---

## 2. Real Observed Event Sequences

From analysis of live runs in `.worktrees/antigravity-1/.openrig-antigravity-seat.log`:

### Case A: Simple Response (No Tools)
```
1. {"event": "init", ...}
2. {"event": "step_update", "step_update": {"state": "DONE", "step_type": "user_input"}}
3. {"event": "step_update", "step_update": {"state": "ACTIVE", "step_type": "agent_response", "text_delta": "..."}}
4. {"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response", "duration_seconds": 6.5}}
5. {"event": "result", "result": {"status": "SUCCESS", "response": "...", "duration_seconds": 7.9}}
```
Between step 4 and step 5, observed delay is typically 0.8s - 1.4s.

### Case B: Response with Tool Execution (e.g. `write_to_file` or `run_command`)
```
1. {"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response"}}
2. {"event": "step_update", "step_update": {"state": "ACTIVE", "step_type": "tool", "tool_name": "write_to_file"}}
3. {"event": "step_update", "step_update": {"state": "ERROR" | "DONE", "tool_name": "write_to_file"}}
4. {"event": "result", "result": {"status": "SUCCESS", "response": "...", "duration_seconds": 17.75}}
```
Notice that tool execution occurs **AFTER** `agent_response DONE`! If a runner assumes `agent_response DONE` ends the turn, tool execution will be orphaned or severed.

---

## 3. Delay & Settle Matrix Tests

Verified in `tests/test_antigravity_stream_protocol.py`:

| Delay After `agent_response DONE` | Event Injected | Bridge Behavior | Verdict |
| :---: | :---: | :--- | :---: |
| **0.5s** | `result` | Instant completion | ✅ PASS |
| **1.0s** | `result` | Instant completion | ✅ PASS |
| **1.9s** | `result` | Instant completion | ✅ PASS |
| **2.0s** | `result` | Instant completion | ✅ PASS |
| **2.1s** | `result` | Waited for `result`; no premature cutoff | ✅ PASS |
| **3.0s** | `result` | Waited for `result`; no premature cutoff | ✅ PASS |
| **2.5s** | `tool ACTIVE` -> `tool DONE` -> `result` | Captured all tool events before `result` | ✅ PASS |
| **Omitted** | Silence past `drain_timeout_sec` | Fallback drain gracefully completes | ✅ PASS |

---

## 4. Bridge Implementation Invariant

In `scripts/antigravity_seat_runner.py`:
- `agent_response DONE` sets `agent_response_done = True` and buffers the latest text.
- The reader loop **continues reading** until:
  1. `provider_event == "result"`: Authoritative completion. Emits `task_result` immediately and exits.
  2. `(now - last_event_time) >= DRAIN_TIMEOUT_SEC`: Fallback drain triggers if `agent_response_done` was set.
  3. `timeout_sec` expires: Process-group kill via `os.killpg(SIGTERM)` / `SIGKILL` and emits `task_timeout`.
