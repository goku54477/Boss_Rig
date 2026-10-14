#!/usr/bin/env bash
set -euo pipefail

# Worktree supervisor and deterministic merge gate
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "Error: not inside a Git repository" >&2
  exit 2
}
ROOT="$(cd "$ROOT" && pwd)"

INTEGRATION_BRANCH="${INTEGRATION_BRANCH:-integration}"
WORKTREE_ROOT="${OPENRIG_WORKTREE_ROOT:-$ROOT/.worktrees}"
STATE_DIR="${OPENRIG_GATE_STATE_DIR:-$ROOT/.openrig-bridge/tasks}"
COMMON_GIT_DIR="$(git -C "$ROOT" rev-parse --git-common-dir)"

[[ "$COMMON_GIT_DIR" = /* ]] || COMMON_GIT_DIR="$ROOT/$COMMON_GIT_DIR"
COMMON_GIT_DIR="$(cd "$COMMON_GIT_DIR" && pwd)"

LOCK_DIR="$COMMON_GIT_DIR/openrig-merge.lock"
LOCK_STALE_SEC="${OPENRIG_LOCK_STALE_SEC:-7200}"
HOST="$(hostname 2>/dev/null || printf unknown)"

mkdir -p "$WORKTREE_ROOT" "$STATE_DIR"

usage() {
  cat >&2 <<'USAGE'
usage:
  worktree-gate.sh init-seat <seat-id>
  worktree-gate.sh assign <task-id> <seat-id>
  worktree-gate.sh review <task-id> <seat-id> [verify-command]
  worktree-gate.sh release <task-id> <seat-id>
  worktree-gate.sh status <task-id>
  worktree-gate.sh recover [--force]
USAGE
  exit 2
}

safe_id() {
  [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] || {
    echo "unsafe id: $1" >&2
    exit 2
  }
}

state_file() {
  printf '%s/%s.state\n' "$STATE_DIR" "$1"
}

seat_file() {
  printf '%s/%s.seat\n' "$STATE_DIR" "$1"
}

set_state() {
  local task="$1"
  local state="$2"
  local file
  file="$(state_file "$task")"
  printf '%s\n' "$state" > "$file.tmp"
  mv "$file.tmp" "$file"
  printf 'GATE_EVENT task=%s state=%s\n' "$task" "$state"
}

# Normalize .git file in worktree to relative gitdir path
# Ensures cross-platform compatibility between Windows and WSL2 Git
normalize_worktree_gitdir() {
  local wt="$1"
  local seat="$2"
  if [[ -f "$wt/.git" ]]; then
    printf 'gitdir: ../../.git/worktrees/%s\n' "$seat" > "$wt/.git"
  fi
}

branch_worktree() {
  local wanted="refs/heads/$1"
  local wt=""
  local line
  while IFS= read -r line; do
    case "$line" in
      worktree\ *)
        wt="${line#worktree }"
        ;;
      branch\ *)
        if [[ "${line#branch }" == "$wanted" ]]; then
          printf '%s\n' "$wt"
          return 0
        fi
        ;;
    esac
  done < <(git -C "$ROOT" worktree list --porcelain)
  return 1
}

require_clean() {
  local dir="$1"
  if [[ -n "$(git -C "$dir" status --porcelain)" ]]; then
    echo "worktree is dirty: $dir" >&2
    return 1
  fi
}

run_checks() {
  local dir="$1"
  local custom="${2:-}"
  local ran=0

  if [[ -n "$custom" ]]; then
    ( cd "$dir" && bash -lc "$custom" )
    return
  fi

  if [[ -f "$dir/package.json" ]]; then
    ran=1
    (
      cd "$dir"
      npm run lint --if-present
      npm test
    )
  fi

  if [[ -f "$dir/pyproject.toml" || -f "$dir/pytest.ini" || -f "$dir/setup.cfg" ]]; then
    ran=1
    (
      cd "$dir"
      python3 -m compileall -q .
      pytest
    )
  fi

  if [[ "$ran" -ne 1 ]]; then
    echo "no default test suite detected; passing default no-op verifier" >&2
    return 0
  fi
}

acquire_lock() {
  local timeout="${1:-${OPENRIG_LOCK_TIMEOUT_SEC:-30}}"
  local deadline=$(( $(date +%s) + timeout ))

  while true; do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      printf '%s\n' "$$" > "$LOCK_DIR/pid"
      printf '%s\n' "$HOST" > "$LOCK_DIR/host"
      date +%s > "$LOCK_DIR/started"
      return 0
    fi

    if [[ $(date +%s) -ge $deadline ]]; then
      break
    fi
    sleep 0.2
  done

  echo "merge lock already exists (timed out after ${timeout}s): $LOCK_DIR" >&2
  return 75
}

release_lock() {
  rm -rf "$LOCK_DIR" 2>/dev/null || true
}

recover() {
  local force="${1:-}"
  local pid=""
  local lock_host=""
  local started=0
  local now
  local age=0
  local int_wt=""

  if int_wt="$(branch_worktree "$INTEGRATION_BRANCH" 2>/dev/null)"; then
    if git -C "$int_wt" rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1; then
      echo "aborting incomplete merge in $int_wt" >&2
      git -C "$int_wt" merge --abort || true
    fi
  fi

  [[ -d "$LOCK_DIR" ]] || return 0

  pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  lock_host="$(cat "$LOCK_DIR/host" 2>/dev/null || true)"
  started="$(cat "$LOCK_DIR/started" 2>/dev/null || printf 0)"
  now="$(date +%s)"
  [[ "$started" =~ ^[0-9]+$ ]] || started=0
  age=$((now - started))

  if [[ "$force" == "--force" ]]; then
    rm -rf "$LOCK_DIR"
    echo "Forcefully removed lock: $LOCK_DIR"
    return 0
  fi

  if [[ "$lock_host" == "$HOST" && "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "refusing recovery: lock owner pid $pid is still alive" >&2
    return 75
  fi

  if (( age < LOCK_STALE_SEC )); then
    echo "lock is only ${age}s old; use recover --force after manual inspection" >&2
    return 75
  fi

  rm -rf "$LOCK_DIR"
  echo "Cleaned stale lock: $LOCK_DIR"
}

cmd="${1:-}"
shift || true

case "$cmd" in
  init-seat)
    [[ $# -eq 1 ]] || usage
    seat="$1"
    safe_id "$seat"

    git -C "$ROOT" show-ref --verify --quiet "refs/heads/$INTEGRATION_BRANCH" || {
      echo "missing integration branch: $INTEGRATION_BRANCH" >&2
      exit 2
    }

    wt="$WORKTREE_ROOT/$seat"
    parking="seat/$seat"

    if [[ -e "$wt" ]]; then
      git -C "$wt" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
        echo "$wt exists but is not a worktree" >&2
        exit 2
      }
      normalize_worktree_gitdir "$wt" "$seat"
      echo "seat worktree already exists and normalized: $wt"
      exit 0
    fi

    if git -C "$ROOT" show-ref --verify --quiet "refs/heads/$parking"; then
      git -C "$ROOT" worktree add "$wt" "$parking"
    else
      git -C "$ROOT" worktree add -b "$parking" "$wt" "$INTEGRATION_BRANCH"
    fi
    normalize_worktree_gitdir "$wt" "$seat"
    echo "initialized seat worktree: $wt"
    ;;

  assign)
    [[ $# -eq 2 ]] || usage
    task="$1"
    seat="$2"
    safe_id "$task"
    safe_id "$seat"

    wt="$WORKTREE_ROOT/$seat"
    branch="task/$task"

    git -C "$wt" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
      echo "run init-seat $seat before assigning tasks" >&2
      exit 2
    }

    require_clean "$wt"

    if other="$(branch_worktree "$branch" 2>/dev/null)" && [[ "$other" != "$wt" ]]; then
      echo "$branch is already checked out in $other" >&2
      exit 2
    fi

    if git -C "$ROOT" show-ref --verify --quiet "refs/heads/$branch"; then
      git -C "$wt" switch "$branch"
    else
      git -C "$wt" switch -c "$branch" "$INTEGRATION_BRANCH"
    fi

    normalize_worktree_gitdir "$wt" "$seat"
    printf '%s\n' "$seat" > "$(seat_file "$task")"
    set_state "$task" ACTIVE
    ;;

  review)
    [[ $# -ge 2 && $# -le 3 ]] || usage
    task="$1"
    seat="$2"
    verify_cmd="${3:-}"

    safe_id "$task"
    safe_id "$seat"

    wt="$WORKTREE_ROOT/$seat"
    branch="task/$task"

    [[ "$(git -C "$wt" branch --show-current)" == "$branch" ]] || {
      echo "$wt is not on $branch" >&2
      exit 2
    }

    require_clean "$wt" || {
      set_state "$task" REVIEW_FAILED
      exit 1
    }

    set_state "$task" REVIEW

    # Rebase/merge latest integration branch into candidate
    if ! git -C "$wt" merge --no-edit "$INTEGRATION_BRANCH"; then
      git -C "$wt" merge --abort >/dev/null 2>&1 || true
      set_state "$task" REVIEW_CONFLICT
      exit 1
    fi

    # Run checks on candidate worktree
    if ! run_checks "$wt" "$verify_cmd"; then
      set_state "$task" REVIEW_FAILED
      exit 1
    fi

    set_state "$task" MERGE_READY

    int_wt="$(branch_worktree "$INTEGRATION_BRANCH")" || {
      echo "integration branch must be checked out in a non-agent worktree" >&2
      set_state "$task" REVIEW_FAILED
      exit 2
    }

    require_clean "$int_wt" || {
      set_state "$task" REVIEW_FAILED
      exit 2
    }

    acquire_lock || {
      set_state "$task" REVIEW
      exit 75
    }

    trap release_lock EXIT INT TERM HUP

    # Re-check integration after locking
    if ! git -C "$wt" merge --no-edit "$INTEGRATION_BRANCH"; then
      git -C "$wt" merge --abort >/dev/null 2>&1 || true
      set_state "$task" REVIEW_CONFLICT
      exit 1
    fi

    if ! run_checks "$wt" "$verify_cmd"; then
      set_state "$task" REVIEW_FAILED
      exit 1
    fi

    set_state "$task" MERGING

    if ! git -C "$int_wt" merge --no-ff --no-commit "$branch"; then
      git -C "$int_wt" merge --abort >/dev/null 2>&1 || true
      set_state "$task" REVIEW_CONFLICT
      exit 1
    fi

    # Run checks on exact combined integration tree
    if ! run_checks "$int_wt" "$verify_cmd"; then
      git -C "$int_wt" merge --abort >/dev/null 2>&1 || true
      set_state "$task" REVIEW_FAILED
      exit 1
    fi

    git -C "$int_wt" commit -m "Merge $branch after deterministic verification"
    set_state "$task" DONE
    ;;

  release)
    [[ $# -eq 2 ]] || usage
    task="$1"
    seat="$2"
    safe_id "$task"
    safe_id "$seat"

    [[ "$(cat "$(state_file "$task")" 2>/dev/null || true)" == "DONE" ]] || {
      echo "refusing release: task is not DONE" >&2
      exit 2
    }

    wt="$WORKTREE_ROOT/$seat"
    branch="task/$task"
    parking="seat/$seat"

    require_clean "$wt"
    git -C "$wt" switch "$parking"
    git -C "$wt" reset --hard "$INTEGRATION_BRANCH"
    git -C "$ROOT" branch -d "$branch" >/dev/null 2>&1 || true
    normalize_worktree_gitdir "$wt" "$seat"
    echo "released $seat from $task back to $parking"
    ;;

  status)
    [[ $# -eq 1 ]] || usage
    safe_id "$1"
    cat "$(state_file "$1")"
    ;;

  recover)
    [[ $# -le 1 ]] || usage
    recover "${1:-}"
    ;;

  reset-seat)
    [[ $# -eq 1 ]] || usage
    seat="$1"
    safe_id "$seat"
    wt="$WORKTREE_ROOT/$seat"
    parking="seat/$seat"

    git -C "$wt" reset --hard HEAD >/dev/null 2>&1 || true
    git -C "$wt" clean -fd >/dev/null 2>&1 || true
    git -C "$wt" switch "$parking" >/dev/null 2>&1 || git -C "$wt" checkout -B "$parking" "$INTEGRATION_BRANCH" >/dev/null 2>&1
    git -C "$wt" reset --hard "$INTEGRATION_BRANCH" >/dev/null 2>&1
    normalize_worktree_gitdir "$wt" "$seat"
    echo "reset seat $seat back to $parking"
    ;;

  *)
    usage
    ;;
esac
