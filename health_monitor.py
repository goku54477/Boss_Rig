HEALTHY = "HEALTHY"


def check_system_health() -> dict:
    return {"status": HEALTHY, "worktrees_count": 0}