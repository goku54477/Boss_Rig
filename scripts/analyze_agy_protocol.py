#!/usr/bin/env python3
import json
import sys
from pathlib import Path

log_path = Path(__file__).resolve().parent.parent / ".worktrees" / "antigravity-1" / ".openrig-antigravity-seat.log"
if not log_path.exists():
    print("Log not found")
    sys.exit(1)

with open(log_path, "r", encoding="utf-8") as f:
    for line in f:
        try:
            d = json.loads(line)
            ev = d.get("event")
            if ev == "result":
                res = d.get("result", {})
                print(f"EVENT: result | status={res.get('status')} | duration={res.get('duration_seconds')} | turns={res.get('num_turns')}")
            elif ev == "step_update":
                su = d.get("step_update", {})
                if su.get("state") == "DONE":
                    print(f"EVENT: step_update DONE | type={su.get('step_type')} | idx={su.get('step_index')}")
                elif su.get("step_type") == "tool":
                    print(f"EVENT: tool {su.get('state')} | name={su.get('tool_name')}")
            elif ev == "init":
                print(f"EVENT: init | conv={d.get('conversation_id')} | mode={d.get('init', {}).get('permission_mode')}")
        except Exception:
            pass
