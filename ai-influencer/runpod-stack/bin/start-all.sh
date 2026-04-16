#!/usr/bin/env bash
set -Eeuo pipefail

STACK_ROOT="${RUNPOD_STACK_ROOT:-/workspace/runpod-stack}"
ENV_FILE="${RUNPOD_STACK_ENV_FILE:-$STACK_ROOT/env/runpod-services.env}"
RUN_DIR="$STACK_ROOT/run"
LOG_DIR="$STACK_ROOT/logs"

mkdir -p "$RUN_DIR" "$LOG_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

: "${GPT_SOVITS_ROOT:?GPT_SOVITS_ROOT is required}"
: "${GPT_SOVITS_VENV:?GPT_SOVITS_VENV is required}"
: "${SEEDLAB_REPO_ROOT:?SEEDLAB_REPO_ROOT is required}"
: "${SEEDLAB_ENV_FILE:?SEEDLAB_ENV_FILE is required}"
: "${AWS_TUNNEL_TARGET:?AWS_TUNNEL_TARGET is required}"
: "${AWS_TUNNEL_KEY:?AWS_TUNNEL_KEY is required}"

TTS_API_STARTUP_TIMEOUT_SECONDS="${TTS_API_STARTUP_TIMEOUT_SECONDS:-240}"
TTS_PID_FILE="$RUN_DIR/tts-api.pid"
SEEDLAB_PID_FILE="$RUN_DIR/seedlab-eval.pid"
AWS_TUNNEL_PID_FILE="$RUN_DIR/aws-reverse-tunnel.pid"

is_running() {
  local pid_file="$1"
  if [[ ! -f "$pid_file" ]]; then
    return 1
  fi
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

start_bg() {
  local name="$1"
  local pid_file="$2"
  local log_file="$3"
  shift 3
  if is_running "$pid_file"; then
    echo "[runpod-stack] $name already running (pid=$(cat "$pid_file"))"
    return 0
  fi
  nohup "$@" >"$log_file" 2>&1 &
  echo $! >"$pid_file"
  echo "[runpod-stack] started $name (pid=$!)"
}

wait_http() {
  local url="$1"
  local name="$2"
  for _ in $(seq 1 30); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "[runpod-stack] $name healthy: $url"
      return 0
    fi
    sleep 2
  done
  echo "[runpod-stack] $name failed health check: $url" >&2
  return 1
}

wait_port() {
  local port="$1"
  local name="$2"
  local timeout_seconds="${3:-60}"
  local deadline=$((SECONDS + timeout_seconds))
  while (( SECONDS < deadline )); do
    if python3 - <<PY >/dev/null 2>&1
import socket
s = socket.socket()
s.settimeout(1.0)
try:
    s.connect(("127.0.0.1", int("$port")))
except OSError:
    raise SystemExit(1)
finally:
    s.close()
PY
    then
      echo "[runpod-stack] $name listening on :$port"
      return 0
    fi
    sleep 2
  done
  echo "[runpod-stack] $name failed to listen on :$port" >&2
  return 1
}

wait_url() {
  local url="$1"
  local name="$2"
  for _ in $(seq 1 30); do
    if curl -sS -o /dev/null "$url"; then
      echo "[runpod-stack] $name reachable: $url"
      return 0
    fi
    sleep 2
  done
  echo "[runpod-stack] $name failed reachability check: $url" >&2
  return 1
}

start_bg \
  "tts-api" \
  "$TTS_PID_FILE" \
  "$LOG_DIR/tts-api.log" \
  /bin/bash -lc "cd '$GPT_SOVITS_ROOT' && exec '$GPT_SOVITS_VENV/bin/python' api_v2.py"

wait_port 9880 "tts-api" "$TTS_API_STARTUP_TIMEOUT_SECONDS"

start_bg \
  "seedlab-eval" \
  "$SEEDLAB_PID_FILE" \
  "$LOG_DIR/seedlab-eval.log" \
  /bin/bash -lc "cd '$SEEDLAB_REPO_ROOT' && set -a && source '$SEEDLAB_ENV_FILE' && set +a && exec '$GPT_SOVITS_VENV/bin/python' runpod-seedlab-eval-service/main.py"

wait_http "http://127.0.0.1:8400/health" "seedlab-eval"

start_bg \
  "aws-reverse-tunnel" \
  "$AWS_TUNNEL_PID_FILE" \
  "$LOG_DIR/aws-reverse-tunnel.log" \
  /bin/bash "$STACK_ROOT/bin/aws-reverse-tunnel.sh"

if [[ -n "${TTS_PUBLIC_BASE_URL:-}" ]]; then
  wait_url "${TTS_PUBLIC_BASE_URL%/}" "tts-public"
fi

if [[ -n "${SEEDLAB_EVAL_PUBLIC_BASE_URL:-}" ]]; then
  wait_http "${SEEDLAB_EVAL_PUBLIC_BASE_URL%/}/health" "seedlab-eval-public"
fi

echo "[runpod-stack] all services started"
