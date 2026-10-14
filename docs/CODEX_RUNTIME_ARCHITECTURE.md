# Codex Execution Architecture: OpenRig Control Plane & Execution Harness

**Author:** Boss Rig Engineering  
**Status:** Authoritative Architectural Specification  
**Version:** v0.1.0  

---

## 1. Architectural Overview & Boundary of Responsibilities

Boss Rig maintains an explicit, strict division of responsibilities between **OpenRig** (the single orchestration & control plane) and the **Execution Plane**.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        OPENRIG CONTROL PLANE                           │
│  • Team Specification (rig.yaml): declares codex-1 with runtime: codex │
│  • Provider Auth & Preflight: verifies ChatGPT / OpenAI credentials    │
│  • Operator Visibility & TUI: rig tui --shared, tmux session oversight │
│  • Inter-Agent Messaging: rig send codex-1 "..."                       │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      BOSS RIG ORCHESTRATION LAYER                      │
│  • Dynamic Policy Engine: Engine != Role decoupling                    │
│  • SQLite Task Store (.openrig-bridge/tasks.sqlite): WAL state, audit  │
│  • Deterministic Merge Gate: worktree-gate.sh + atomic merge mutex     │
│  • Candidate Commit Pinning & Independent Review Enforcement           │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   CODEX EXECUTION RUNNER HARNESS                       │
│       scripts/codex_seat_runner.py & scripts/codex-seat-runner.sh      │
│  • Ingests structured JSON TaskEnvelope via stdin                      │
│  • Employs non-interactive headless CLI: codex exec --json             │
│  • Enforces safe workspace sandboxing: --approve-for-me                │
│  • Isolates process tree: start_new_session=True, killpg watchdog      │
│  • Translates stream JSONL to normalized BRIDGE_EVENT                  │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   SAFETY BOUNDARY & WORKTREE ISOLATION                 │
│  • Worktree: .worktrees/codex-1                                        │
│  • Parked Branch: seat/codex-1                                         │
│  • Task Branch: task/<task_id>                                         │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Answers to Architectural Questions

### A. Does the live `codex-1` seat use OpenRig's native Codex runtime declaration?
**Yes.** In `rig.yaml` and `config/rig.yaml`, the `codex-1` seat is explicitly defined with `runtime: codex`:
```yaml
pods:
  - id: engineering
    label: Interchangeable Coding Engines
    members:
      - id: codex-1
        agent_ref: local:config/agents/codex-neutral
        runtime: codex
        profile: default
        cwd: .worktrees/codex-1
```
OpenRig's preflight and health subsystem inspects `codex` availability, valid ChatGPT/OpenAI provider auth, and establishes the seat's identity within the team topology.

### B. Does Boss Rig invoke `codex exec` through its own runner?
**Yes.** When executing programmatic tasks (planning, implementation, review), Boss Rig routes task envelopes through `scripts/codex-seat-runner.sh` which executes `codex exec --approve-for-me --json -C <worktree>`.

### C. Why does the custom runner exist alongside OpenRig's runtime?
Interactive TUI terminal seats (as created for human pair-programming in tmux) rely on raw terminal screen scrapers or interactive conversational turns. In an unattended, multi-agent CI/CD orchestration pipeline, interactive conversational scrapers introduce severe failure modes:
1. **Unpredictable TUI prompts**: Interactive mode may pause for terminal keypresses or ansi escape codes that cannot be deterministically correlated with task IDs.
2. **Loss of structured event streaming**: Headless `codex exec --json` outputs structured line-delimited JSON events (`thread.started`, `turn.started`, `item.started`, `item.completed`, `turn.completed`).
3. **Missing Correlation & Timeouts**: `codex_seat_runner.py` provides exact `task_id` correlation, attempt tracking, execution timeouts with process-group kill (`os.killpg`), and exit code capture.
4. **Sandboxed Auto-Approval**: Headless execution requires `--approve-for-me` to execute shell commands within the workspace sandbox without blocking on manual user prompts.

### D. Are there two competing Codex lifecycle implementations?
**No.** There is only **one control authority**: OpenRig.
- OpenRig owns the **macro lifecycle**: seat registration, health checks, team configuration, and manual operator interventions (`rig send`, `rig tui`).
- `codex_seat_runner.py` is the **micro execution harness**: a clean, single-responsibility process adapter that translates a `TaskEnvelope` into a `codex exec` invocation and returns a `BRIDGE_EVENT`.

---

## 3. Future Native-Adapter Strategy

When OpenRig ships an official programmatic task execution API (such as native ACP or JSON-RPC execution adapters), Boss Rig's `TaskDispatcher` will connect directly to that interface without modifying `TaskEnvelope`, `PolicyEngine`, or `worktree-gate.sh`. The current architecture maintains complete boundary isolation so swapping the underlying runner requires changing only a single line in `orchestrator/dispatcher.py`.
