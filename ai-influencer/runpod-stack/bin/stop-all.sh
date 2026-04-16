#!/usr/bin/env bash
set -Eeuo pipefail

STACK_ROOT="${RUNPOD_STACK_ROOT:-/workspace/runpod-stack}"
RUN_DIR="$STACK_ROOT/run"

stop_pid_file() {
  local pid_file="$1"
  if [[ ! -f "$pid_file" ]]; then
    return 0
  fi
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"
}

stop_pid_file "$RUN_DIR/aws-reverse-tunnel.pid"
stop_pid_file "$RUN_DIR/seedlab-eval.pid"
stop_pid_file "$RUN_DIR/tts-api.pid"

echo "[runpod-stack] stopped"
