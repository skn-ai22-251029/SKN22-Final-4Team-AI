#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/seed_lab_quickstart.sh [-dup|--dup] ["seed1,seed2,..."]

Examples:
  ./scripts/seed_lab_quickstart.sh
  ./scripts/seed_lab_quickstart.sh ""
  ./scripts/seed_lab_quickstart.sh "111,222"
  ./scripts/seed_lab_quickstart.sh -dup "111,222"

Behavior:
  - Reads TTS_API_URL from (priority):
    1) current shell env TTS_API_URL
    2) ./.env file
  - Reads OpenAI keys from (priority):
    1) current shell env OPENAI_API_KEY_SEEDLAB_ASR / OPENAI_API_KEY_SEEDLAB_JUDGE
    2) current shell env OPENAI_FALLBACK_API_KEY (or OPENAI_API_KEY legacy)
    3) ./.env file
  - Ensures scripts/seed_lab_dataset.local.json exists
    (copies from example on first run)
  - Default mode:
    samples=30, takes_per_seed=1, concurrency=2 (총 30개)
  - Dup mode (-dup/--dup):
    samples=10, takes_per_seed=3, concurrency=2 (총 30개)
  - In default mode, if provided seed list has fewer than 30 seeds,
    remaining seeds are auto-filled randomly
  - In dup mode, if provided seed list has fewer than 10 seeds,
    remaining seeds are auto-filled randomly
  - If provided seed list has more than target seed count,
    only first N are used
  - Starts interactive serve mode automatically and opens serve URL
  - Runs auto-eval for all generated samples before opening serve UI
    (if both ASR/JUDGE keys are resolved)
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SAMPLES=30
TAKES_PER_SEED=1
CONCURRENCY=2
OPEN_BROWSER=1
DUP_MODE=0
SEED_LIST=""
SERVE_HOST="${SEED_LAB_SERVE_HOST:-127.0.0.1}"
SERVE_PORT="${SEED_LAB_SERVE_PORT:-8765}"
AUTO_EVAL_ALL="${SEED_LAB_AUTO_EVAL_ALL:-1}"
AUTO_EVAL_ASR_MODEL="${SEED_LAB_ASR_MODEL:-gpt-4o-transcribe}"
AUTO_EVAL_JUDGE_MODEL="${SEED_LAB_JUDGE_MODEL:-gpt-5.4}"
AUTO_EVAL_TIMEOUT="${SEED_LAB_AUTO_EVAL_TIMEOUT:-120}"
OPENAI_API_KEY_SEEDLAB_ASR="${OPENAI_API_KEY_SEEDLAB_ASR:-}"
OPENAI_API_KEY_SEEDLAB_JUDGE="${OPENAI_API_KEY_SEEDLAB_JUDGE:-}"
OPENAI_FALLBACK_API_KEY="${OPENAI_FALLBACK_API_KEY:-}"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
SEEDLAB_BASE_SCRIPT_TEXT="${SEEDLAB_BASE_SCRIPT_TEXT:-}"

POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -dup|--dup)
      DUP_MODE=1
      shift
      ;;
    -*)
      echo "ERROR: unknown option: $1" >&2
      usage
      exit 1
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

if [[ "${#POSITIONAL[@]}" -gt 1 ]]; then
  echo "ERROR: only one seed-list argument is allowed: \"seed1,seed2,...\"" >&2
  usage
  exit 1
fi
SEED_LIST="${POSITIONAL[0]:-}"

if (( DUP_MODE == 1 )); then
  SAMPLES=10
  TAKES_PER_SEED=3
fi

if [[ -z "${TTS_API_URL:-}" ]] && [[ -f ".env" ]]; then
  # .env 전체 source는 포맷 이슈 가능성이 있어 필요한 키만 안전 추출
  TTS_API_URL="$(grep -E '^TTS_API_URL=' .env | head -n1 | cut -d'=' -f2- || true)"
fi

if [[ -z "${OPENAI_API_KEY_SEEDLAB_ASR:-}" ]] && [[ -f ".env" ]]; then
  OPENAI_API_KEY_SEEDLAB_ASR="$(grep -E '^[[:space:]]*OPENAI_API_KEY_SEEDLAB_ASR=' .env | head -n1 | cut -d'=' -f2- || true)"
fi
if [[ -z "${OPENAI_API_KEY_SEEDLAB_JUDGE:-}" ]] && [[ -f ".env" ]]; then
  OPENAI_API_KEY_SEEDLAB_JUDGE="$(grep -E '^[[:space:]]*OPENAI_API_KEY_SEEDLAB_JUDGE=' .env | head -n1 | cut -d'=' -f2- || true)"
fi
if [[ -z "${OPENAI_FALLBACK_API_KEY:-}" ]] && [[ -f ".env" ]]; then
  OPENAI_FALLBACK_API_KEY="$(grep -E '^[[:space:]]*OPENAI_FALLBACK_API_KEY=' .env | head -n1 | cut -d'=' -f2- || true)"
fi
if [[ -z "${OPENAI_API_KEY:-}" ]] && [[ -f ".env" ]]; then
  OPENAI_API_KEY="$(grep -E '^[[:space:]]*OPENAI_API_KEY=' .env | head -n1 | cut -d'=' -f2- || true)"
fi
if [[ -z "${SEEDLAB_BASE_SCRIPT_TEXT:-}" ]] && [[ -f ".env" ]]; then
  SEEDLAB_BASE_SCRIPT_TEXT="$(python3 - <<'PY'
from pathlib import Path
key = "SEEDLAB_BASE_SCRIPT_TEXT="
text = Path(".env").read_text(encoding="utf-8")
lines = text.splitlines()
for i, line in enumerate(lines):
    if not line.startswith(key):
        continue
    value = line[len(key):]
    collected = [value]
    for nxt in lines[i + 1:]:
        if "=" in nxt and not nxt.startswith((" ", "\t")):
            break
        collected.append(nxt)
    print("\n".join(collected).rstrip())
    break
PY
)"
fi

# .env 값이 따옴표로 감싸진 경우 제거
TTS_API_URL="${TTS_API_URL%\"}"
TTS_API_URL="${TTS_API_URL#\"}"
OPENAI_API_KEY_SEEDLAB_ASR="${OPENAI_API_KEY_SEEDLAB_ASR%\"}"
OPENAI_API_KEY_SEEDLAB_ASR="${OPENAI_API_KEY_SEEDLAB_ASR#\"}"
OPENAI_API_KEY_SEEDLAB_ASR="$(printf '%s' "${OPENAI_API_KEY_SEEDLAB_ASR}" | tr -d '\r')"
OPENAI_API_KEY_SEEDLAB_JUDGE="${OPENAI_API_KEY_SEEDLAB_JUDGE%\"}"
OPENAI_API_KEY_SEEDLAB_JUDGE="${OPENAI_API_KEY_SEEDLAB_JUDGE#\"}"
OPENAI_API_KEY_SEEDLAB_JUDGE="$(printf '%s' "${OPENAI_API_KEY_SEEDLAB_JUDGE}" | tr -d '\r')"
OPENAI_FALLBACK_API_KEY="${OPENAI_FALLBACK_API_KEY%\"}"
OPENAI_FALLBACK_API_KEY="${OPENAI_FALLBACK_API_KEY#\"}"
OPENAI_FALLBACK_API_KEY="$(printf '%s' "${OPENAI_FALLBACK_API_KEY}" | tr -d '\r')"
OPENAI_API_KEY="${OPENAI_API_KEY%\"}"
OPENAI_API_KEY="${OPENAI_API_KEY#\"}"
OPENAI_API_KEY="$(printf '%s' "${OPENAI_API_KEY}" | tr -d '\r')"
SEEDLAB_BASE_SCRIPT_TEXT="${SEEDLAB_BASE_SCRIPT_TEXT%\"}"
SEEDLAB_BASE_SCRIPT_TEXT="${SEEDLAB_BASE_SCRIPT_TEXT#\"}"
SEEDLAB_BASE_SCRIPT_TEXT="$(printf '%s' "${SEEDLAB_BASE_SCRIPT_TEXT}" | tr -d '\r')"

if [[ -z "${OPENAI_API_KEY_SEEDLAB_ASR:-}" ]]; then
  OPENAI_API_KEY_SEEDLAB_ASR="${OPENAI_FALLBACK_API_KEY:-${OPENAI_API_KEY:-}}"
fi
if [[ -z "${OPENAI_API_KEY_SEEDLAB_JUDGE:-}" ]]; then
  OPENAI_API_KEY_SEEDLAB_JUDGE="${OPENAI_FALLBACK_API_KEY:-${OPENAI_API_KEY:-}}"
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
if [[ -n "${OPENAI_API_KEY_SEEDLAB_ASR:-}" && -n "${OPENAI_API_KEY_SEEDLAB_JUDGE:-}" ]]; then
  export OPENAI_API_KEY_SEEDLAB_ASR
  export OPENAI_API_KEY_SEEDLAB_JUDGE
  export OPENAI_FALLBACK_API_KEY
  echo "[seed-lab] OPENAI_API_KEY_SEEDLAB_ASR loaded: yes"
  echo "[seed-lab] OPENAI_API_KEY_SEEDLAB_JUDGE loaded: yes"
  echo "[seed-lab] OpenAI keys exported to serve env: yes"
else
  echo "[seed-lab] OpenAI keys loaded: no (AI 자동평가 비활성)"
  echo "[seed-lab] OpenAI keys exported to serve env: no"
fi
if [[ -n "${SEEDLAB_BASE_SCRIPT_TEXT:-}" ]]; then
  export SEEDLAB_BASE_SCRIPT_TEXT
  echo "[seed-lab] SEEDLAB_BASE_SCRIPT_TEXT loaded: yes"
else
  echo "[seed-lab] SEEDLAB_BASE_SCRIPT_TEXT loaded: no (dataset s1 fallback 사용)"
fi
echo "[seed-lab] dataset=${DATASET_LOCAL}"
echo "[seed-lab] stage=a samples=${SAMPLES} takes_per_seed=${TAKES_PER_SEED} concurrency=${CONCURRENCY}"
if [[ -n "$SEED_LIST" ]]; then
  echo "[seed-lab] seed_list=${SEED_LIST}"
fi

TMP_LOG="$(mktemp)"

set +e
CMD=(
  python3 scripts/seed_lab.py run
  --dataset "$DATASET_LOCAL"
  --api-url "$TTS_API_URL"
  --stage a
  --samples "$SAMPLES"
  --takes-per-seed "$TAKES_PER_SEED"
  --concurrency "$CONCURRENCY"
)
if [[ -n "$SEED_LIST" ]]; then
  CMD+=(--seed-list "$SEED_LIST")
fi
"${CMD[@]}" | tee "$TMP_LOG"
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
RUN_DIR="$(cd "$(dirname "$REVIEW_HTML")" && pwd)"

if [[ "${AUTO_EVAL_ALL}" != "0" ]]; then
  if [[ -n "${OPENAI_API_KEY_SEEDLAB_ASR:-}" && -n "${OPENAI_API_KEY_SEEDLAB_JUDGE:-}" ]]; then
    echo "[seed-lab] auto-eval-all: start (run_dir=$RUN_DIR)"
    echo "[seed-lab] auto-eval-all: asr_model=${AUTO_EVAL_ASR_MODEL} judge_model=${AUTO_EVAL_JUDGE_MODEL} timeout=${AUTO_EVAL_TIMEOUT}s"
    set +e
    python3 scripts/seed_lab.py auto-eval \
      --run-dir "$RUN_DIR" \
      --asr-model "$AUTO_EVAL_ASR_MODEL" \
      --judge-model "$AUTO_EVAL_JUDGE_MODEL" \
      --timeout "$AUTO_EVAL_TIMEOUT" \
      --openai-api-key-asr "$OPENAI_API_KEY_SEEDLAB_ASR" \
      --openai-api-key-judge "$OPENAI_API_KEY_SEEDLAB_JUDGE"
    AUTO_EVAL_EXIT="$?"
    set -e
    if (( AUTO_EVAL_EXIT == 0 )); then
      echo "[seed-lab] auto-eval-all: done"
    else
      echo "[seed-lab] WARN: auto-eval-all failed (exit=$AUTO_EVAL_EXIT). serve는 계속 진행합니다."
    fi
  else
    echo "[seed-lab] auto-eval-all: skipped (OPENAI API keys missing)"
  fi
else
  echo "[seed-lab] auto-eval-all: skipped (SEED_LAB_AUTO_EVAL_ALL=0)"
fi

SERVE_URL="http://${SERVE_HOST}:${SERVE_PORT}"
echo "[seed-lab] interactive_server_cmd=python3 scripts/seed_lab.py serve --run-dir \"$RUN_DIR\" --api-url \"$TTS_API_URL\" --host \"$SERVE_HOST\" --port \"$SERVE_PORT\""
echo "[seed-lab] opening serve url: $SERVE_URL"

if command -v open >/dev/null 2>&1; then
  open "$SERVE_URL" || true
  echo "[seed-lab] browser opened: $SERVE_URL"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$SERVE_URL" >/dev/null 2>&1 || true
  echo "[seed-lab] browser opened via xdg-open: $SERVE_URL"
else
  echo "[seed-lab] open/xdg-open 명령이 없어 자동 열기를 건너뜁니다."
fi

echo "[seed-lab] starting interactive server (Ctrl+C to stop)"
exec python3 scripts/seed_lab.py serve \
  --run-dir "$RUN_DIR" \
  --api-url "$TTS_API_URL" \
  --host "$SERVE_HOST" \
  --port "$SERVE_PORT"
