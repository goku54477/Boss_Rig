# Live 3×3 Agent-Role Interchangeability Matrix Report

**Generated:** 2026-10-14T11:16:17Z  
**Architecture:** OpenRig single control plane  
**Principle:** `ENGINE != ROLE` — All engines independently verified in all semantic roles.

## Interchangeability Matrix Results

| Engine Seat | Role | Status | Latency (s) | Model / Runtime | Contract Evidence |
| :--- | :--- | :---: | :---: | :--- | :--- |
| `codex-1` | **planner** | ✅ PASS | 7.61s | `codex` | Clean worktree verified; zero file mutations |
| `codex-1` | **implementer** | ✅ PASS | 29.39s | `codex` | Candidate commit `aa422ecd` pinned |
| `codex-1` | **reviewer** | ✅ PASS | 15.01s | `codex` | Clean worktree; reviewed commit `aa422ecd` |
| `opencode-1` | **planner** | ✅ PASS | 7.54s | `opencode` | Clean worktree verified; zero file mutations |
| `opencode-1` | **implementer** | ✅ PASS | 27.30s | `opencode` | Candidate commit `d51b14c1` pinned |
| `opencode-1` | **reviewer** | ✅ PASS | 18.62s | `opencode` | Clean worktree; reviewed commit `d51b14c1` |
| `antigravity-1` | **planner** | ✅ PASS | 21.10s | `antigravity` | Clean worktree verified; zero file mutations |
| `antigravity-1` | **implementer** | ✅ PASS | 1.31s | `antigravity` | Candidate commit `09d497b2` pinned |
| `antigravity-1` | **reviewer** | ✅ PASS | 6.45s | `antigravity` | Clean worktree; reviewed commit `09d497b2` |

## Contract Verification Details

- **Planner**: Verified clean worktree (`git status --porcelain == 0`) and structured planning response.
- **Implementer**: Verified candidate commit creation on seat worktree branch (`git rev-parse HEAD`).
- **Reviewer**: Verified commit review verdict (`PASS` / `NEEDS_CHANGES`) with zero file mutations.

## Conclusion
All 9 cells (3 engines × 3 roles) passed live model execution contracts successfully.
