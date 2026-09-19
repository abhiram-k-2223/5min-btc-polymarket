#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE="docker compose -f $ROOT/docker-compose.yml"

case "${1:-}" in
  build)
    $COMPOSE build
    ;;
  up)
    # Default CMD is a conservative dry-run session (no --execute: no orders).
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
    shift
    $COMPOSE run --rm btc5m "$@"
    ;;
  *)
    echo "Usage: $0 {build|up|down|status|run -- <runner args...>}"
    echo "Env overrides: BTC5M_ENV_FILE BTC5M_EXEC_REPO BTC5M_RUNTIME_DIR"
    exit 2
    ;;
esac
