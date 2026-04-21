#!/usr/bin/env bash
set -Eeuo pipefail

STACK_ROOT="${RUNPOD_STACK_ROOT:-/workspace/runpod-stack}"
ENV_FILE="${RUNPOD_STACK_ENV_FILE:-$STACK_ROOT/env/runpod-services.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

: "${AWS_TUNNEL_TARGET:?AWS_TUNNEL_TARGET is required}"
: "${AWS_TUNNEL_KEY:?AWS_TUNNEL_KEY is required}"

AWS_TUNNEL_SSH_PORT="${AWS_TUNNEL_SSH_PORT:-22}"
AWS_TUNNEL_REMOTE_BIND_HOST="${AWS_TUNNEL_REMOTE_BIND_HOST:-127.0.0.1}"
AWS_TUNNEL_TTS_REMOTE_PORT="${AWS_TUNNEL_TTS_REMOTE_PORT:-19880}"
AWS_TUNNEL_EVAL_REMOTE_PORT="${AWS_TUNNEL_EVAL_REMOTE_PORT:-18400}"
AWS_TUNNEL_TTS_LOCAL_PORT="${AWS_TUNNEL_TTS_LOCAL_PORT:-9880}"
AWS_TUNNEL_EVAL_LOCAL_PORT="${AWS_TUNNEL_EVAL_LOCAL_PORT:-8400}"
AWS_TUNNEL_RECONNECT_SECONDS="${AWS_TUNNEL_RECONNECT_SECONDS:-5}"
AWS_TUNNEL_KNOWN_HOSTS="${AWS_TUNNEL_KNOWN_HOSTS:-$STACK_ROOT/ssh/known_hosts}"
AWS_TUNNEL_STRICT_HOST_KEY_CHECKING="${AWS_TUNNEL_STRICT_HOST_KEY_CHECKING:-accept-new}"
AWS_TUNNEL_RUNTIME_KEY="${AWS_TUNNEL_RUNTIME_KEY:-/tmp/runpod_to_aws_ed25519}"

mkdir -p "$(dirname "$AWS_TUNNEL_KNOWN_HOSTS")"
chmod 700 "$(dirname "$AWS_TUNNEL_KEY")" 2>/dev/null || true
cp "$AWS_TUNNEL_KEY" "$AWS_TUNNEL_RUNTIME_KEY"
chmod 600 "$AWS_TUNNEL_RUNTIME_KEY"

if ! command -v ssh >/dev/null 2>&1; then
  echo "ssh command not found" >&2
  exit 1
fi

while true; do
  echo "[runpod-stack] opening reverse tunnel to $AWS_TUNNEL_TARGET"
  ssh \
    -i "$AWS_TUNNEL_RUNTIME_KEY" \
    -p "$AWS_TUNNEL_SSH_PORT" \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o StrictHostKeyChecking="$AWS_TUNNEL_STRICT_HOST_KEY_CHECKING" \
    -o UserKnownHostsFile="$AWS_TUNNEL_KNOWN_HOSTS" \
    -N \
    -R "${AWS_TUNNEL_REMOTE_BIND_HOST}:${AWS_TUNNEL_TTS_REMOTE_PORT}:127.0.0.1:${AWS_TUNNEL_TTS_LOCAL_PORT}" \
    -R "${AWS_TUNNEL_REMOTE_BIND_HOST}:${AWS_TUNNEL_EVAL_REMOTE_PORT}:127.0.0.1:${AWS_TUNNEL_EVAL_LOCAL_PORT}" \
    "$AWS_TUNNEL_TARGET"
  echo "[runpod-stack] reverse tunnel disconnected, retrying in ${AWS_TUNNEL_RECONNECT_SECONDS}s"
  sleep "$AWS_TUNNEL_RECONNECT_SECONDS"
done
