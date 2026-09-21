#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE="docker compose -f $ROOT/docker-compose.yml"

check_paths() {
  # (#22) the compose defaults assume a sibling-checkout layout. Resolve
  # them here — honoring the BTC5M_* overrides — and fail fast with a
  # clear message instead of a cryptic bind-mount error from compose.
  # Also derives BTC5M_ENV_FILE from BTC5M_EXEC_REPO when unset, so the
  # two can't silently disagree.
  local exec_repo="${BTC5M_EXEC_REPO:-$ROOT/../pm-hl-conservative-plus-repo}"
  if [[ ! -d "$exec_repo" ]]; then
    echo "error: execution repo not found: $exec_repo" >&2
    echo "set BTC5M_EXEC_REPO to the pm-hl-conservative-plus-repo checkout" >&2
    exit 2
  fi
  export BTC5M_EXEC_REPO="$exec_repo"
  local env_file="${BTC5M_ENV_FILE:-$exec_repo/.env}"
  if [[ ! -f "$env_file" ]]; then
    if [[ -n "${BTC5M_ENV_FILE:-}" ]]; then
      echo "error: env file not found: $env_file" >&2
      exit 2
    fi
    echo "warn: no .env at $env_file; continuing without it (dry-run only)" >&2
    env_file=/dev/null
  fi
  export BTC5M_ENV_FILE="$env_file"
  local runtime_dir="${BTC5M_RUNTIME_DIR:-$ROOT/runtime}"
  mkdir -p "$runtime_dir"
  export BTC5M_RUNTIME_DIR="$runtime_dir"
}

case "${1:-}" in
  build)
    $COMPOSE build
    ;;
  up)
    # Default CMD is a conservative dry-run session (no --execute: no orders).
    check_paths
    $COMPOSE up -d --build
    ;;
  down)
    $COMPOSE down
    ;;
  status)
    $COMPOSE ps
    ;;
  run)
    # Live example: $0 run -- --profile conservative --execute
    check_paths
    shift
    $COMPOSE run --rm btc5m "$@"
    ;;
  *)
    echo "Usage: $0 {build|up|down|status|run -- <runner args...>}"
    echo "Env overrides: BTC5M_ENV_FILE BTC5M_EXEC_REPO BTC5M_RUNTIME_DIR"
    exit 2
    ;;
esac
