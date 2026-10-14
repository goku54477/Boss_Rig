# Live 3×3 Agent-Role Interchangeability Matrix Report

**Generated:** 2026-10-14T09:59:23Z  
**Architecture:** OpenRig single control plane  
**Principle:** `ENGINE != ROLE` — All engines independently verified in all semantic roles.

## Interchangeability Matrix Results

| Engine Seat | Role | Status | Latency (s) | Model / Runtime | Contract Evidence |
| :--- | :--- | :---: | :---: | :--- | :--- |
| `codex-1` | **planner** | ✅ PASS | 9.45s | `codex` | Clean worktree verified; zero file mutations |
| `codex-1` | **implementer** | ✅ PASS | 14.60s | `codex` | Candidate commit `e4753659` pinned |
| `codex-1` | **reviewer** | ✅ PASS | 17.04s | `codex` | Clean worktree; reviewed commit `e4753659` |
| `opencode-1` | **planner** | ✅ PASS | 6.93s | `opencode` | Clean worktree verified; zero file mutations |
| `opencode-1` | **implementer** | ✅ PASS | 14.62s | `opencode` | Candidate commit `4325c1fc` pinned |
| `opencode-1` | **reviewer** | ✅ PASS | 10.55s | `opencode` | Clean worktree; reviewed commit `4325c1fc` |
| `antigravity-1` | **planner** | ✅ PASS | 20.19s | `antigravity` | Clean worktree verified; zero file mutations |
| `antigravity-1` | **implementer** | ✅ PASS | 1.53s | `antigravity` | Candidate commit `1df54f54` pinned |
| `antigravity-1` | **reviewer** | ✅ PASS | 7.01s | `antigravity` | Clean worktree; reviewed commit `1df54f54` |

## Contract Verification Details

- **Planner**: Verified clean worktree (`git status --porcelain == 0`) and structured planning response.
- **Implementer**: Verified candidate commit creation on seat worktree branch (`git rev-parse HEAD`).
- **Reviewer**: Verified commit review verdict (`PASS` / `NEEDS_CHANGES`) with zero file mutations.

## Conclusion
All 9 cells (3 engines × 3 roles) passed live model execution contracts successfully.
