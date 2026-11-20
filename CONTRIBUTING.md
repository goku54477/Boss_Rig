# Contributing Adapters to Boss Rig Shared Plane

Boss Rig keeps its core orchestration authority (`orchestrator/task_service.py`) separate from pluggable **Shared Plane** adapters in `plane/adapters/` and `plane/router.py`.

## Adapter Contracts

1. **Listener (`plane/adapters/listeners.py`)** — subclass `BaseListener`:
   - `baseline() -> int`: claim pre-existing messages so startup never replays history.
   - `poll() -> list[PlaneMessage]`: return only new `role == "user"` messages; ignore `[SHARED PLANE REPORT` markers and handle transient read/lock errors safely.

2. **Router (`plane/router.py`)** — subclass `BaseRouter`:
   - `classify(msg: PlaneMessage) -> RoutingDecision`: return `action` in `("IGNORE", "PROPOSE_BUILD", "ANSWER_QUESTION", "CHECK_STATUS")` with `target_agent` chosen from `enabled_agents`.

3. **Speaker (`plane/adapters/speaker.py`)** — subclass `BaseSpeaker`:
   - `speak(report: BuildReport) -> dict`: write or deliver `[SHARED PLANE REPORT]` outputs atomically without mutating external chat databases.

## Adapter Template

```python
from plane.adapters.listeners import BaseListener
from plane.schemas import PlaneMessage

class CustomListener(BaseListener):
    def baseline(self) -> int:
        return 0

    def poll(self) -> list[PlaneMessage]:
        return []
```

## Running the Offline Test Suite

Before submitting a PR, verify the Shared Plane and bridge offline suites pass with zero failures:

```bash
python -m unittest tests/test_shared_plane.py tests/test_conversational_bridge_e2e.py tests/test_bridge_e2e.py tests/test_server_api.py tests/test_mcp_server.py tests/test_store_and_idempotency.py tests/test_scheduler_and_policy.py tests/test_operator_intervention.py tests/test_goldfish_context_priming.py -v
```
