#!/usr/bin/env bash
# ==============================================================================
# Boss Rig — Environment Bootstrap & Seat Verification Script
# ==============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Ensure all scripts are executable early
chmod +x "$ROOT"/scripts/*.sh "$ROOT"/scripts/boss-orchestrator 2>/dev/null || true

echo "=================================================================="
echo "          BOSS RIG — PRODUCTION MULTI-AGENT BOOTSTRAP             "
echo "=================================================================="

# 1. Verify Prerequisites
echo -n "Checking git... "
if command -v git >/dev/null 2>&1; then
    echo "✓ $(git --version)"
else
    echo "MISSING" >&2
    exit 1
fi

echo -n "Checking python3... "
if command -v python3 >/dev/null 2>&1; then
    echo "✓ $(python3 --version)"
else
    echo "MISSING" >&2
    exit 1
fi

echo -n "Checking tmux... "
if command -v tmux >/dev/null 2>&1; then
    echo "✓ $(tmux -V)"
else
    echo "MISSING" >&2
    exit 1
fi

echo -n "Checking openrig (rig)... "
if command -v rig >/dev/null 2>&1; then
    echo "✓ $(rig --version 2>/dev/null || echo 'installed')"
else
    echo "MISSING — run 'npm install -g @openrig/cli'" >&2
fi

echo -n "Checking codex-cli... "
if command -v codex >/dev/null 2>&1; then
    echo "✓ $(codex --version 2>/dev/null || echo 'installed')"
else
    echo "MISSING" >&2
fi

echo -n "Checking opencode... "
if command -v opencode >/dev/null 2>&1; then
    echo "✓ $(opencode --version 2>/dev/null || echo 'installed')"
else
    echo "MISSING" >&2
fi

echo -n "Checking antigravity (agy)... "
if command -v agy >/dev/null 2>&1; then
    echo "✓ $(agy --version 2>/dev/null || echo 'installed')"
else
    echo "MISSING" >&2
fi

echo ""
echo "------------------------------------------------------------------"
echo "Configuring repository worktrees and seats..."
echo "------------------------------------------------------------------"

mkdir -p "$ROOT/.worktrees" "$ROOT/.openrig-bridge/tasks"

# Ensure integration branch exists
if ! git rev-parse --verify integration >/dev/null 2>&1; then
    echo "Creating 'integration' branch..."
    git branch integration HEAD
fi

# Ensure _integration worktree exists
INT_WT="$ROOT/.worktrees/_integration"
if [[ ! -d "$INT_WT" ]]; then
    echo "Creating integration worktree at .worktrees/_integration..."
    git worktree add "$INT_WT" integration
fi

# Initialize seats
for seat in codex-1 opencode-1 antigravity-1; do
    "$ROOT/scripts/worktree-gate.sh" init-seat "$seat"
    "$ROOT/scripts/worktree-gate.sh" reset-seat "$seat"
done

# Recover stale locks
"$ROOT/scripts/worktree-gate.sh" recover --force >/dev/null 2>&1 || true

chmod +x "$ROOT/scripts/boss-orchestrator" \
         "$ROOT/scripts/worktree-gate.sh" \
         "$ROOT/scripts/codex-seat-runner.sh" \
         "$ROOT/scripts/opencode-seat-runner.sh" \
         "$ROOT/scripts/antigravity-seat-runner.sh"

echo ""
echo "=================================================================="
echo "✅ Bootstrap complete! System is ready."
echo "Run: ./scripts/boss-orchestrator status"
echo "=================================================================="
