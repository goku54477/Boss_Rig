# Canonical Test Inventory — Boss Rig v0.1.0

**Author:** Boss Rig Engineering  
**Status:** Authoritative Release Inventory  
**Total Automated Tests:** 58  
**Total Test Files:** 13  

---

## 1. Test Suite Directory & Classification

| Test File | Category | Tests | Real Model? | Real CLI? | OpenRig? | Git Worktrees? | Approx Duration | Primary Purpose |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| `tests/test_codex_bridge.py` | UNIT | 3 | No | No | No | No | 0.01s | Verifies safe `--approve-for-me` workspace sandboxing and explicit opt-in for danger flags. |
| `tests/test_antigravity_completion_semantics.py` | UNIT | 3 | No | No | No | No | 0.01s | Verifies safe default command builder and permission mode flags. |
| `tests/test_antigravity_stream_protocol.py` | UNIT | 8 | No | No | No | No | 15.6s | Deterministic replay proving delay matrix (0.5s - 3s), post-response tool capture, and drain fallback. |
| `tests/test_scheduler_and_policy.py` | UNIT | 4 | No | No | No | No | 0.01s | Enforces `ENGINE != ROLE`, independent review rejection (`reviewer != implementer`), and candidate commit pinning. |
| `tests/test_store_and_idempotency.py` | UNIT | 4 | No | No | No | No | 0.02s | Proves SQLite durability, lifecycle history replay, `BLOCKED`/`CANCELLED` states, and event deduplication. |
| `tests/test_worktree_gate.py` | LOCAL INTEGRATION | 5 | No | Yes | No | Yes | 1.8s | Verifies persistent seat worktrees, branch switching, merge locks, and gate test execution. |
| `tests/test_fault_injection.py` | LOCAL INTEGRATION | 3 | No | Yes | No | Yes | 3.0s | Proves recovery from dirty uncommitted files, orphaned 3-hr stale locks, and killed child processes. |
| `tests/test_concurrency_proof.py` | LIVE CONCURRENCY | 1 | No | Yes | No | Yes | 22.8s | Proves 3-way simultaneous task execution on codex-1, opencode-1, antigravity-1, lock contention, and revalidation. |
| `tests/test_opencode_bridge.py` | LIVE PROVIDER | 5 | Yes | Yes | Yes | Yes | 35.0s | Tests live OpenCode CLI streaming, timeout watchdog, JSON formatting, and directory constraints. |
| `tests/test_antigravity_bridge.py` | LIVE PROVIDER | 8 | Yes | Yes | Yes | Yes | 140.0s | Tests live Antigravity CLI streaming, conversation initialization, turns, errors, and process watchdog. |
| `tests/test_operator_intervention.py` | LOCAL INTEGRATION | 5 | No | Yes | No | Yes | 0.2s | Verifies seat hold mode (MANUAL_HOLD), dispatch blocking, task transition to BLOCKED, and CLI hold/release. |
| `tests/test_interchangeability_matrix.py` | LIVE MATRIX | 9 | Yes | Yes | Yes | Yes | 103.8s | Proves all 9 cells of the 3×3 matrix (Codex, OpenCode, Antigravity across Planner, Implementer, Reviewer) against live models. |
| `tests/test_end_to_end_workflow.py` | LIVE E2E | 4 | Yes | Yes | Yes | Yes | 104.5s | Executes Workflows A, B, and C with full role rotation, candidate commit pinning, independent review, and gate merge. |
| `tests/test_goldfish_agent_workflow.py` | LIVE E2E | 1 | Yes | Yes | Yes | Yes | 40.5s | Contextual Goldfish memory injection -> Codex retrieval -> OpenCode implementation -> Antigravity review -> gate merge. |

---

## 2. Canonical Summary Table

| Category | Suite Count | Test Count | Live Model Dependencies |
| :--- | :---: | :---: | :--- |
| **Unit Tests** | 5 | **22** | None (Runs offline in CI / local dev) |
| **Local Integration Tests** | 3 | **13** | Git CLI, local filesystem worktrees |
| **Live Provider & Bridge Tests** | 2 | **13** | Local `opencode` & `agy` CLIs + inference tiers |
| **Live 3×3 Interchangeability Matrix** | 1 | **9** | Live Codex, OpenCode, and Antigravity models |
| **Live Role-Rotated E2E Workflows** | 2 | **5** | Live models, Goldfish SQLite, Git integration merge |
| **Live Concurrency & Lock Contention** | 1 | **1** | ThreadPoolExecutor (3 concurrent seats), atomic mutex |
| **TOTAL AUTOMATED TESTS** | **14** | **63** | **Zero mocks for production role validation** |

---

## 3. Discrepancy Reconciliation

- **Why 32 tests was previously cited**: In Milestone 2–3, only the preliminary mock/unit and single-engine bridge tests existed (32 assertions).
- **Why 23 tests was cited in the CI run**: The core fast automated suite (`test_antigravity_completion_semantics.py`, `test_codex_bridge.py`, `test_scheduler_and_policy.py`, `test_worktree_gate.py`, `test_store_and_idempotency.py`, `test_fault_injection.py`, and `test_concurrency_proof.py`) contains exactly 23 offline/fast-integration tests designed to run quickly on PR commits.
- **Why 58 tests was cited prior to Stage 4**: Stage 4 introduced `test_operator_intervention.py` (5 tests), bringing the total to **63 automated tests** across 14 test suites.
- **The true canonical total**: Exactly **63 tests** across all 14 suites.
