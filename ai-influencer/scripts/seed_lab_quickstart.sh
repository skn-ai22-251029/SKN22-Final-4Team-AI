#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/seed_lab_quickstart.sh [samples] [concurrency] [--no-open]

Examples:
  ./scripts/seed_lab_quickstart.sh
  ./scripts/seed_lab_quickstart.sh 100 4
  ./scripts/seed_lab_quickstart.sh 100 4 --no-open

Behavior:
  - Reads TTS_API_URL from (priority):
    1) current shell env TTS_API_URL
    2) ./.env file
  - Ensures scripts/seed_lab_dataset.local.json exists
    (copies from example on first run)
  - Runs Stage-A seed lab (default 100 seeds)
  - Opens generated review HTML automatically (macOS: open)
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SAMPLES="${1:-100}"
CONCURRENCY="${2:-4}"
OPEN_BROWSER=1

if [[ "${3:-}" == "--no-open" ]] || [[ "${1:-}" == "--no-open" ]] || [[ "${2:-}" == "--no-open" ]]; then
  OPEN_BROWSER=0
fi

if ! [[ "$SAMPLES" =~ ^[0-9]+$ ]] || (( SAMPLES <= 0 )); then
  echo "ERROR: samples must be a positive integer" >&2
  exit 1
fi
if ! [[ "$CONCURRENCY" =~ ^[0-9]+$ ]] || (( CONCURRENCY <= 0 )); then
  echo "ERROR: concurrency must be a positive integer" >&2
  exit 1
fi

if [[ -z "${TTS_API_URL:-}" ]] && [[ -f ".env" ]]; then
  # .env 전체 source는 포맷 이슈 가능성이 있어 필요한 키만 안전 추출
  TTS_API_URL="$(grep -E '^TTS_API_URL=' .env | head -n1 | cut -d'=' -f2- || true)"
fi

if [[ -z "${TTS_API_URL:-}" ]]; then
  echo "ERROR: TTS_API_URL is empty. .env에 TTS_API_URL=... 값을 넣어주세요." >&2
  exit 1
fi

DATASET_LOCAL="scripts/seed_lab_dataset.local.json"
DATASET_EXAMPLE="scripts/seed_lab_dataset.example.json"

if [[ ! -f "$DATASET_LOCAL" ]]; then
  if [[ ! -f "$DATASET_EXAMPLE" ]]; then
    echo "ERROR: dataset example not found: $DATASET_EXAMPLE" >&2
    exit 1
  fi
  cp "$DATASET_EXAMPLE" "$DATASET_LOCAL"
  echo "[seed-lab] created $DATASET_LOCAL from example"
fi

echo "[seed-lab] TTS_API_URL=${TTS_API_URL}"
echo "[seed-lab] dataset=${DATASET_LOCAL}"
echo "[seed-lab] stage=a samples=${SAMPLES} concurrency=${CONCURRENCY}"

TMP_LOG="$(mktemp)"

set +e
python3 scripts/seed_lab.py run \
  --dataset "$DATASET_LOCAL" \
  --api-url "$TTS_API_URL" \
  --stage a \
  --samples "$SAMPLES" \
  --concurrency "$CONCURRENCY" | tee "$TMP_LOG"
RUN_EXIT="${PIPESTATUS[0]}"
set -e

if (( RUN_EXIT != 0 )); then
  rm -f "$TMP_LOG"
  exit "$RUN_EXIT"
fi

REVIEW_HTML="$(grep -E '^\[seed-lab\] review_html=' "$TMP_LOG" | tail -n1 | sed 's/^\[seed-lab\] review_html=//')"
rm -f "$TMP_LOG"

if [[ -z "${REVIEW_HTML:-}" ]]; then
  echo "[seed-lab] WARN: review_html 경로를 찾지 못했습니다." >&2
  exit 0
fi

echo "[seed-lab] review_html_path=$REVIEW_HTML"

if (( OPEN_BROWSER == 0 )); then
  echo "[seed-lab] --no-open 지정으로 자동 열기를 건너뜁니다."
  exit 0
fi

if command -v open >/dev/null 2>&1; then
  open "$REVIEW_HTML" || true
  echo "[seed-lab] browser opened: $REVIEW_HTML"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$REVIEW_HTML" >/dev/null 2>&1 || true
  echo "[seed-lab] browser opened via xdg-open: $REVIEW_HTML"
else
  echo "[seed-lab] open/xdg-open 명령이 없어 자동 열기를 건너뜁니다."
fi
