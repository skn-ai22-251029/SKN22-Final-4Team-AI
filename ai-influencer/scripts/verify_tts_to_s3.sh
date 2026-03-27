#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/verify_tts_to_s3.sh <job_id> [--since 30m]

Example:
  ./scripts/verify_tts_to_s3.sh 1d2f3a4b-... --since 60m

What it verifies:
  1) WF-11/gateway 로그에서 TTS 전송/업로드 처리 흔적
  2) DB jobs row (script_text length, audio_url, audio_filename)
  3) audio_url(s3://...) 실제 S3 객체 HEAD 성공 여부
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
  usage
  exit 0
fi

JOB_ID="$1"
shift

SINCE="45m"
if [[ "${1:-}" == "--since" ]]; then
  SINCE="${2:-45m}"
fi

if [[ ! "$JOB_ID" =~ ^[A-Za-z0-9-]{8,}$ ]]; then
  echo "ERROR: invalid job_id format: $JOB_ID" >&2
  exit 1
fi

if ! command -v docker-compose >/dev/null 2>&1; then
  echo "ERROR: docker-compose command not found." >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f docker-compose.yml ]]; then
  echo "ERROR: docker-compose.yml not found in $ROOT_DIR" >&2
  exit 1
fi

FAILURES=0
WARNINGS=0

pass() { echo "✅ $*"; }
warn() { echo "⚠️  $*"; WARNINGS=$((WARNINGS + 1)); }
fail() { echo "❌ $*"; FAILURES=$((FAILURES + 1)); }

echo "== TTS -> S3 validation =="
echo "job_id : $JOB_ID"
echo "since  : $SINCE"
echo

echo "[1/4] gateway 로그 확인"
GATEWAY_LOG="$(docker-compose logs --since="$SINCE" messenger-gateway 2>/dev/null | grep -nE "$JOB_ID|send_audio done|tts upload failed|send_tts_audio_message|send_tts_link_message|audio presign failed" || true)"
if [[ -n "$GATEWAY_LOG" ]]; then
  echo "$GATEWAY_LOG"
else
  warn "지정 구간에서 gateway 관련 로그를 찾지 못했습니다."
fi

if grep -q "send_audio done job_id=$JOB_ID" <<<"$GATEWAY_LOG"; then
  pass "gateway send-audio 완료 로그 확인"
else
  fail "gateway send-audio 완료 로그 미확인 (job_id=$JOB_ID)"
fi

if grep -q "tts upload failed job_id=$JOB_ID" <<<"$GATEWAY_LOG"; then
  fail "gateway tts upload failed 로그가 존재합니다."
fi

echo
echo "[2/4] n8n 로그 참고 출력"
N8N_LOG="$(docker-compose logs --since="$SINCE" n8n 2>/dev/null | grep -nE "$JOB_ID|wf-11-tts-generate|script_text is required|TTS API failed" || true)"
if [[ -n "$N8N_LOG" ]]; then
  echo "$N8N_LOG"
else
  warn "지정 구간에서 n8n 관련 로그를 찾지 못했습니다."
fi

echo
echo "[3/4] DB jobs row 검증"
DB_ROW="$(
  docker-compose exec -T \
    -e TARGET_JOB_ID="$JOB_ID" \
    postgres \
    sh -lc '
      psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -A -F "|" \
        -v target_job_id="$TARGET_JOB_ID" \
        -c "SELECT id::text, status, COALESCE(audio_url, '\'''\''), COALESCE(final_url, '\'''\''), COALESCE(script_json->'\''media_names'\''->>'\''audio_filename'\'', '\'''\''), LENGTH(COALESCE(script_json->>'\''script_text'\'', script_json->>'\''script'\'', '\'''\''))::text FROM jobs WHERE id::text = :'\"'\"'target_job_id'\"'\"' LIMIT 1;"
    ' 2>/dev/null
)"

DB_ROW="$(echo "$DB_ROW" | tr -d '\r' | sed '/^\s*$/d' | head -n 1)"
if [[ -z "$DB_ROW" ]]; then
  fail "DB에서 job_id를 찾지 못했습니다."
  echo
  echo "SUMMARY: FAIL ($FAILURES failures, $WARNINGS warnings)"
  exit 1
fi

IFS='|' read -r DB_ID DB_STATUS AUDIO_URL FINAL_URL AUDIO_FILENAME SCRIPT_LEN <<<"$DB_ROW"
echo "id=$DB_ID"
echo "status=$DB_STATUS"
echo "audio_url=$AUDIO_URL"
echo "final_url=$FINAL_URL"
echo "audio_filename=$AUDIO_FILENAME"
echo "script_len=$SCRIPT_LEN"

if [[ "${SCRIPT_LEN:-0}" =~ ^[0-9]+$ ]] && (( SCRIPT_LEN > 0 )); then
  pass "script_text 길이 확인 ($SCRIPT_LEN)"
else
  fail "script_text가 비어 있습니다."
fi

if [[ "$AUDIO_URL" == s3://* ]]; then
  pass "audio_url이 s3:// 형식입니다."
else
  fail "audio_url이 s3:// 형식이 아닙니다."
fi

if [[ "$AUDIO_FILENAME" == *.wav && "$AUDIO_FILENAME" == *"-$JOB_ID.wav" ]]; then
  pass "audio_filename 규칙 확인 (${AUDIO_FILENAME})"
else
  warn "audio_filename이 기대 규칙(YYYYMMDD-${JOB_ID}.wav)과 다를 수 있습니다."
fi

if [[ "$AUDIO_URL" != s3://* ]]; then
  echo
  echo "SUMMARY: FAIL ($FAILURES failures, $WARNINGS warnings)"
  exit 1
fi

echo
echo "[4/4] S3 객체 HEAD 검증"
if ! HEAD_OUTPUT="$(
  docker-compose exec -T \
    -e TARGET_S3_URI="$AUDIO_URL" \
    messenger-gateway \
    python - <<'PY'
import os
import boto3
from botocore.config import Config

uri = (os.getenv("TARGET_S3_URI") or "").strip()
if not uri.startswith("s3://"):
    raise SystemExit("INVALID_S3_URI")
rest = uri[len("s3://"):]
if "/" not in rest:
    raise SystemExit("INVALID_S3_URI_PATH")
bucket, key = rest.split("/", 1)

region = (os.getenv("MEDIA_S3_REGION") or "ap-northeast-2").strip()
role_arn = (os.getenv("MEDIA_S3_ROLE_ARN") or "").strip()
external_id = (os.getenv("MEDIA_S3_EXTERNAL_ID") or "").strip()
session_name = (os.getenv("MEDIA_S3_ROLE_SESSION_NAME") or "ai-influencer-media-session").strip()

base = boto3.session.Session(region_name=region)
if role_arn:
    sts = base.client("sts")
    params = {"RoleArn": role_arn, "RoleSessionName": session_name}
    if external_id:
        params["ExternalId"] = external_id
    creds = sts.assume_role(**params)["Credentials"]
    s3 = boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        config=Config(signature_version="s3v4"),
    )
else:
    s3 = base.client("s3", region_name=region, config=Config(signature_version="s3v4"))

obj = s3.head_object(Bucket=bucket, Key=key)
size = int(obj.get("ContentLength", -1))
print(f"HEAD_OK bucket={bucket} key={key} size={size} etag={obj.get('ETag', '')}")
if size <= 0:
    raise SystemExit("INVALID_CONTENT_LENGTH")
PY
)"; then
  echo "$HEAD_OUTPUT"
  fail "S3 HEAD 검증 실패"
else
  echo "$HEAD_OUTPUT"
  pass "S3 객체 접근/크기 검증 완료"
fi

echo
if (( FAILURES > 0 )); then
  echo "SUMMARY: FAIL ($FAILURES failures, $WARNINGS warnings)"
  exit 1
fi
echo "SUMMARY: PASS (0 failures, $WARNINGS warnings)"
