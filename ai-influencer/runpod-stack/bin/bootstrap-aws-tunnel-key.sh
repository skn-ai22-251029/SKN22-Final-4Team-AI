#!/usr/bin/env bash
set -Eeuo pipefail

STACK_ROOT="${RUNPOD_STACK_ROOT:-/workspace/runpod-stack}"
ENV_FILE="${RUNPOD_STACK_ENV_FILE:-$STACK_ROOT/env/runpod-services.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

AWS_TUNNEL_KEY="${AWS_TUNNEL_KEY:-$STACK_ROOT/ssh/runpod_to_aws_ed25519}"
KEY_DIR="$(dirname "$AWS_TUNNEL_KEY")"
PUB_KEY="${AWS_TUNNEL_KEY}.pub"

mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"

if [[ ! -f "$AWS_TUNNEL_KEY" ]]; then
  ssh-keygen -t ed25519 -N "" -f "$AWS_TUNNEL_KEY" -C "runpod-to-aws"
fi

chmod 600 "$AWS_TUNNEL_KEY"

echo "[runpod-stack] public key path: $PUB_KEY"
cat "$PUB_KEY"
