"""Deterministic verification script for health_monitor feature."""
import sys
sys.path.insert(0, ".")
import health_monitor

res = health_monitor.check_system_health()
print(f"Health check result: {res}")
assert res.get("status") == "HEALTHY", f"Expected status HEALTHY, got {res.get('status')}"
assert res.get("worktrees_count", -1) >= 0, "worktrees_count must be >= 0"
print("VERIFICATION SUCCESSFUL")
sys.exit(0)
