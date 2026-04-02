# AI Influencer Automation Pipeline — Phase 1

Discord 기반 AI 인플루언서 자동화 파이프라인.

테스트 편의를 위해 https://github.com/DJAeun/SKN22-Final-4Team-AI/tree/develop 클론
---

## 아키텍처 개요

```
[Discord 사용자]
      │  /create   /report   /jobs   /tts   /heygen
      ▼
[discord-bot]  ──────────────────────────────────→  [messenger-gateway :8080]
                                                              │  /internal/*
                                    ┌─────────────────────────┤
                                    ▼                         ▼
                             [PostgreSQL]      [n8n :5678 / WF-01,04,05,06,08,09,10,11,12]
                                    ▲
                                    │                         │
                                    └──────────────────────── ┤
                                                              │
                                                  [notebooklm-service]
                                                  (report 생성, notebook 관리)
```

### 서비스 구성

| 서비스 | 역할 | 포트 |
|--------|------|------|
| `postgres` | 데이터 저장소 | 내부 5432 |
| `n8n` | 워크플로 엔진 | 5678 (외부) |
| `messenger-gateway` | 메신저 허브 API | 8080 (외부) |
| `discord-bot` | Discord WebSocket 연결 | 없음 |
| `notebooklm-service` | NotebookLM 자동화 & 보고서 생성 | 내부 전용 |

---

## 워크플로 전체 흐름 개요

```
자동 수집 (WF-09/10)          수동 요청 (/create)        수동 요청 (/report)
       │                              │                          │
       ▼                              ▼                          ▼
[YouTube RSS 수집]           [WF-01: 스크립트 생성]    [채널 선택 버튼 표시]
[notebooklm 소스 추가]              │                          │
       │ (성공 시 auto-report)      │                          │
       └──────────────→ [gateway /internal/auto-report]       │
                               │                               │
                          [WF-06: 보고서 생성]                 │
                               │                               │
                       [Discord 자동 보고서 전송]              │
                                                               │
       │                       [컨펌 버튼 전송]            [채널 선택]
       [WF-10: 매일 노트북 생성]           │                          │
                                [WF-05: 승인/수정]         [보고서 목록 조회]
                                     │ 승인                      │
                               [WF-11: TTS 생성]    [WF-06: 보고서 생성]
                                     │                          │
                                 [WF-12: HeyGen 생성]        [영상 제작 요청]
```

---

## Discord 명령어 사용 흐름 (실운영)

### `/create concept:<콘셉트>`

```
/create
  → gateway /internal/message
  → WF-01 (스크립트 생성 + 승인 버튼 전송)
  → 승인 버튼 클릭: /internal/confirm-action
  → WF-05 approved
  → WF-11 (TTS 생성 + 승인/반려 버튼)
  → TTS 승인: /internal/tts-action
  → WF-12 (영상 생성 + 미리보기 승인/반려)
  → 영상 승인: /internal/video-action
  → WF-08 (SNS 업로드)
```

반려/수정:
- 스크립트 수정요청 → WF-05 `revision_requested` → WF-01 재실행
- TTS 반려 → 상태 `APPROVED`로 복귀 후 `/tts` 재실행 가능
- 영상 반려 → 스크립트/TTS/처음부터 단계 선택 재실행

### `/report [prompt]` (공백 허용)

```
/report
  → /internal/report-message
  → 채널 선택 버튼
  → /internal/channel-select
  → notebooklm-service /list-reports (gpt-5.4 CUA list loop)
      ├─ 기존 보고서 선택: /internal/report-select(select)
      │   → notebooklm-service /get-report
      │   → Discord 전송 + jobs.script_json.script_text 저장
      ├─ 기존 보고서 없음: [🆕 새로 생성] 버튼 노출
      └─ 조회 실패/지연: [🔄 다시 조회] / [🆕 새로 생성] 버튼 노출
          (자동 fallback으로 바로 생성하지 않음)
      → 새로 생성 선택 시: /internal/report-select(new) → WF-06
          → notebooklm-service /generate
          → /internal/send-report
          → Discord 전송 + jobs.script_json.script_text 저장
  → 보고서 메시지 버튼 분기
      ├─ [🔊 TTS만 제작]
      │   → /internal/report-to-tts
      │   → WF-11 (auto_trigger_wf12=false, TTS 승인/반려 버튼 유지)
      └─ [🎬 영상으로 제작]
          → /internal/report-to-video
          → WF-11 (auto_trigger_wf12=true) → WF-12 자동 진행
```

### `/jobs [purpose]`

- `purpose`: `all | tts | heygen`
- 최근 job 목록(8자리 ID, 상태, script/audio 보유 여부) 조회

### `/tts [job_id]`

- `job_id`는 전체 UUID/8자리 prefix/미입력 모두 허용
- 미입력 시 현재 사용자+채널의 최근 `script_text` 보유 job 자동 선택
- 실행 경로: `/internal/tts-generate` → WF-11

### `/heygen [job_id]`

- `job_id`는 전체 UUID/8자리 prefix/미입력 모두 허용
- 미입력 시 현재 사용자+채널의 최근 `audio_url` 보유 job 자동 선택
- 실행 경로: `/internal/heygen-generate` → WF-12
- avatar 선택 우선순위: 요청 `avatar_id` → job 저장값 → `characters.heygen_avatar_id` → `HEYGEN_AVATAR_ID`

### `POST /internal/heygen-smoke-test`

- HeyGen 실제 영상 생성 전에 인증/잔여 quota/avatar 접근만 검증
- 영상 생성은 호출하지 않으므로 과금 없는 스모크 테스트 용도
- 선택 body: `{ "avatar_id": "..." }`
- 응답에 현재 WF-12 기본값(`width/height/caption/speed/poll/max_wait/mock`)도 포함

### 시간별 자동 보고서 Discord 전송

- `WF-09 -> /internal/auto-report -> WF-06` 경로는 계속 동작
- 다만 Discord 전송은 `AUTO_REPORT_DISCORD_DELIVERY_ENABLED=false` 이면 생략
- 기본값은 `false`
- 이때도 대본 rewrite, S3 저장, DB 업데이트는 그대로 수행

### `POST /internal/character-avatar`

- 캐릭터 기본 HeyGen 아바타를 DB 상태로 저장
- body: `{ "character_id": "default-character", "avatar_id": "..." }`
- 빈 문자열을 보내면 캐릭터 기본 avatar를 제거하고 다음 우선순위(job/env fallback)로 내려감

---

## 파일명 규칙 (전 워크플로우 공통)

- 기준 규칙: `YYYYMMDD-{job_id}.{ext}`
- 날짜 기준 타임존: `Asia/Seoul`
- `job_id`는 전체 UUID를 사용
- 동일 job의 산출물은 basename을 공유
  - 대본: `YYYYMMDD-{job_id}.txt`
  - TTS: `YYYYMMDD-{job_id}.wav`
  - 영상(메타): `YYYYMMDD-{job_id}.mp4`

예시 (`job_id=550e8400-e29b-41d4-a716-446655440000`, 2026-03-26 생성):
- `20260326-550e8400-e29b-41d4-a716-446655440000.txt`
- `20260326-550e8400-e29b-41d4-a716-446655440000.wav`
- `20260326-550e8400-e29b-41d4-a716-446655440000.mp4`

---

## 교차 계정 S3 저장 전략

- 저장 대상: 대본(`.txt`), TTS(`.wav`), 영상(`.mp4`)
- 저장 위치: **다른 AWS 계정의 S3 버킷**
- 접근 방식: **AssumeRole + 비공개 버킷 + Presigned URL**
- 객체 키: `reports/`, `tts/`, `videos/` prefix + `YYYYMMDD-{job_id}.{ext}`

### Discord 전송 정책

- 파일 크기 ≤ 10MB: Discord 파일 첨부 전송
- 파일 크기 > 10MB: Discord 텍스트 메시지로 Presigned URL 전송(기본 24시간)
- 링크 전송 메시지에서도 기존 버튼 UX를 유지
  - 보고서: `[🔊 TTS만 제작]`, `[🎬 영상으로 제작]`
  - TTS: `[✅ 승인 (WF-12 진행)] [❌ 반려]`

### 필수 환경변수

| 변수 | 설명 |
|------|------|
| `MEDIA_S3_BUCKET` | 타겟 S3 버킷명(타 AWS 계정) |
| `MEDIA_S3_REGION` | S3 리전 |
| `MEDIA_S3_ROLE_ARN` | 타겟 계정에서 AssumeRole 할 IAM Role ARN |
| `MEDIA_S3_EXTERNAL_ID` | (선택) 외부 ID |
| `MEDIA_S3_ROLE_SESSION_NAME` | STS 세션명 |
| `MEDIA_PRESIGN_EXPIRES_SECONDS` | Presigned URL 만료(기본 86400) |
| `MEDIA_MAX_DISCORD_FILE_BYTES` | 첨부 임계치(기본 10485760, 10MB) |
| `MEDIA_S3_PREFIX_REPORTS` | 대본 prefix (기본 `reports`) |
| `MEDIA_S3_PREFIX_TTS` | TTS prefix (기본 `tts`) |
| `MEDIA_S3_PREFIX_VIDEOS` | 영상 prefix (기본 `videos`) |

### AWS IAM 설정 요약 (교차 계정)

1. **타겟 계정(S3 보유)**에 업로드 전용 Role 생성 (예: `AiInfluencerMediaWriterRole`)
2. Trust policy에서 소스 계정의 실행 Role(EC2/ECS)을 Principal로 허용
3. 권한은 버킷 전체가 아닌 `reports/*`, `tts/*`, `videos/*` prefix로 최소권한 부여
4. 소스 계정 실행 Role에 `sts:AssumeRole` 권한 추가

---

## 워크플로 상세 흐름

### WF-01: 콘텐츠 생성 요청 수신

```
[Webhook 수신] ← discord-bot /create 요청
      │
      ▼
[요청 파싱] job_id, concept_text, messenger 정보
      │
      ▼
[DB 저장] jobs 테이블 INSERT, status=SCRIPTING
      │
      ▼
[스크립트 생성] (AI 스크립트 생성 로직)
      │
      ▼
[DB 업데이트] status=WAITING_APPROVAL, script 저장
      │
      ▼
[gateway /internal/send-confirm 호출]
→ Discord에 스크립트 + [✅ 승인하기] [✏️ 수정 지시] 버튼 전송
```

---

### WF-04: 컨펌 재요청

```
[Webhook 수신] ← gateway confirm 재요청
      │
      ▼
[요청 파싱] job_id
      │
      ▼
[DB 조회] jobs 테이블에서 스크립트 조회
      │
      ▼
[gateway /internal/send-confirm 호출]
→ Discord에 기존 스크립트 + 버튼 재전송
```

---

### WF-05: 승인/수정 처리

```
[Webhook 수신] ← discord-bot 버튼 클릭 이벤트
      │
      ▼
[요청 파싱] job_id, action (approved / revision_requested)
      │
      ├─[approved]──────────────────────────────────────────→ [WF-11 Webhook 호출]
      │                                                         영상 제작 파이프라인 시작
      │
      └─[revision_requested]──→ [DB 업데이트] revision_note 저장
                                      │
                                      ▼
                                [WF-01 재실행] 수정 지시 반영한 스크립트 재생성
```

---

### WF-06: NotebookLM 보고서 생성

```
[Webhook 수신] ← gateway /internal/report-to-video 또는 /report 요청
      │
      ▼
[요청 파싱] job_id, prompt, notebook_id, channel_id
      │
      ▼
[DB 업데이트] status=GENERATING
      │
      ▼
[notebooklm-service /generate 호출]
      │
      ├─[성공]──→ [gateway /internal/send-report 호출]
      │           → Discord에 보고서 텍스트 + [🔊 TTS만 제작] [🎬 영상으로 제작] 버튼 전송
      │
      └─[실패]──→ [gateway /internal/send-text 호출]
                  → Discord에 오류 메시지 전송
```

---

### WF-11: TTS 생성 + Discord 공유

```
[Webhook 수신] ← WF-05 승인 / report_to_tts / report_to_video / 재생성
      │
      ▼
[요청 파싱] job_id, script_text, auto_trigger_wf12
      │
      ▼
[DB 업데이트] status=GENERATING
      │
      ▼
[TTS 생성 + WAV 저장] /home/node/.n8n/audio
      │
      ▼
[gateway /internal/send-audio 호출]
→ Discord에 WAV 전송
    ├─ auto_trigger_wf12=false: [✅ 승인(WF-12)] [❌ 반려] 버튼 전송
    └─ auto_trigger_wf12=true : 승인 버튼 없이 안내 후 WF-12 자동 진행
      │
      ▼
[DB 업데이트] status=APPROVED, audio_url 저장
      │
      ├─[auto_trigger_wf12=true]─→ [WF-12 호출]
      └─[기본]──────────────────→ Discord에서 승인 대기
```

---

### WF-12: HeyGen 영상 생성

```
[Webhook 수신] ← Discord TTS 승인 또는 자동 트리거
      │
      ▼
[요청 파싱] job_id, channel_id, user_id, audio_file_path|audio_url
      │
      ▼
[DB 업데이트] status=GENERATING
      │
      ▼
[HeyGen 업로드 + 영상 생성 + 폴링]
      │
      ├─[completed]──→ [DB 업데이트] status=WAITING_VIDEO_APPROVAL, video_url 저장
      │                      │
      │                      ▼
      │               [gateway /internal/send-video-preview 호출]
      │               → Discord에 영상 미리보기 + [✅ 승인] [❌ 반려] 버튼
      │
      └─[failed]────→ [DB 업데이트] status=FAILED
                             │
                             ▼
                      [gateway /internal/send-text 호출]
```

---

### WF-08: SNS 업로드

```
[Webhook 수신] ← discord-bot 영상 승인 버튼 클릭
      │
      ▼
[요청 파싱] job_id, video_url
      │
      ▼
[DB 업데이트] status=PUBLISHING
      │
      ▼
[병렬 업로드]
  ├─ YouTube 업로드
  ├─ Instagram 업로드
  └─ TikTok 업로드
      │
      ▼
[platform_posts INSERT] 각 플랫폼 게시물 ID 저장
      │
      ▼
[DB 업데이트] status=PUBLISHED
      │
      ▼
[gateway /internal/send-text 호출]
→ Discord에 업로드 완료 알림 전송
```

---

### WF-09: YouTube 소스 자동 수집 (매시간)

```
[Schedule Trigger] 매시간 실행
      │
      ▼
[채널 목록 파싱] TOPIC_CHANNELS 환경변수
  형식: 채널이름/채널ID+채널이름/채널ID+...
  → [{channelId, channelName}, ...] 목록 생성
      │
      ▼
[IF: 채널 존재 여부 확인]
  ├─[skip=true]──→ 종료
  │
  └─[유효]──→ [notebooklm-service /notebook-state 조회]
                │
                ▼
        [IF: active notebook에 기존 source 존재]
          ├─[있음]──→ 종료
          │           reason: active notebook already has sources
          │
          └─[없음]──→ [YouTube RSS 조회] (채널별 병렬 처리)
                        https://www.youtube.com/feeds/videos.xml?channel_id={channelId}
                              │
                              ▼
                      [새 영상 필터링]
                      최근 N시간(`WF09_LOOKBACK_HOURS`) 이내 업로드만 유지
                              │
                              ▼
                      [최신 1개만 선택]
                              │
                              ▼
                      [IF: 선택 결과 존재 여부]
                        ├─[없음]──→ 종료
                        │           reason: no recent video within lookback
                        │
                        └─[있음]──→ [notebooklm-service /check-and-add-source 호출]
                                      { source_url, source_title, channel_name }
                                            │
                                            ▼
                                    [결과 로깅]
                                    소스 추가 완료 / 오류
                                            │
                            [added=true일 때만 자동 보고서 트리거]
                                            ▼
                      [gateway /internal/auto-report]
                       - job_id 자동 생성
                       - Discord 전송 채널: DISCORD_ALLOWED_CHANNEL_IDS 첫 번째
                       - WF-06 호출 → 보고서 생성/첨부 전송
```

---

### WF-10: 일일 노트북 생성 (매일 오전 7시 30분, KST)

```
[Schedule Trigger] 매일 07:30 실행 (Asia/Seoul)
      │
      ▼
[채널 목록 파싱] TOPIC_CHANNELS 환경변수
  형식: 채널이름/채널ID+채널이름/채널ID+...
  → [{topic(=채널이름), channelIds, notebookName}, ...] 목록 생성
      │
      ▼
[IF: 채널 존재 여부 확인]
  ├─[skip=true]──→ 종료
  │
  └─[유효]──→ [notebooklm-service /create-notebook 호출] (채널별)
                { name: "채널이름 YYYY-MM-DD", topic: 채널이름, channel_ids: [channelId] }
                      │
                      ▼
              [결과 로깅]
              노트북 생성 완료(notebook_id, notebook_url) / 오류
```

---

### 보고서 요청 전체 흐름 (/report 명령어)

```
Discord 사용자: /report
      │
      ▼
[discord-bot] → [gateway /internal/report-message]
                        │
                        ▼
               [TOPIC_CHANNELS 기반 채널 목록 생성]
                        │
                        ▼
               [Discord 채널 선택 버튼 전송]
                [채널A] [채널B] [채널C] ...
                        │
      사용자가 채널 버튼 클릭
                        │
                        ▼
[discord-bot] → [gateway /internal/channel-select]
                 { job_id, channel_id }
                        │
                        ▼
               [notebooklm-service /list-reports 조회]
               → 해당 채널의 기존 보고서 목록
                        │
                ┌───────────┬───────────────┬────────────────────────┐
         [보고서 있음]  [보고서 없음]   [조회 실패/지연]
                │            │               │
                ▼            ▼               ▼
   [Discord 보고서 선택 버튼]  [🆕 새로 생성]  [🔄 다시 조회] [🆕 새로 생성]
   [보고서1] [보고서2] [새로 생성]  버튼 노출      버튼 노출 (자동 fallback 없음)
                │            │               │
      사용자가 선택          └──────┬────────┘
                │                   │
       ├─[기존 보고서 선택]──→ Discord에 보고서 전송
       │
       └─[새로 생성]────────→ [WF-06 실행] → 보고서 생성 → 전송
```

---

## n8n/workflows 파일별 전체 흐름 요약

현재 저장소의 `ai-influencer/n8n/workflows`에는 아래 9개 워크플로가 있습니다.

| 파일 | 트리거 | 진입점 | 핵심 처리 | 후속 |
|------|--------|--------|-----------|------|
| `WF-01_input_receive.json` | Webhook | `POST /webhook/wf-01-input` | job 수신, `SCRIPTING` 전이, 스크립트 생성/저장 | gateway `/internal/send-confirm` |
| `WF-04_confirm_request.json` | Webhook | `POST /webhook/wf-04-confirm-request` | 기존 스크립트/상태 조회 후 컨펌 재전송 | gateway `/internal/send-confirm` |
| `WF-05_confirm_handler.json` | Webhook | `POST /webhook/wf-05-confirm` | 승인/수정 분기 및 상태 업데이트 | 승인→WF-11, 수정→WF-01 |
| `WF-06_notebooklm_report.json` | Webhook | `POST /webhook/wf-06-report` | NotebookLM 보고서 생성 호출, 성공/실패 분기 | 성공→`/internal/send-report`, 실패→`/internal/send-text` |
| `WF-08_sns_upload.json` | Webhook | `POST /webhook/wf-08-sns-upload` | `PUBLISHING` 전이, SNS 업로드 처리, post 기록 | 완료 알림 + `PUBLISHED` |
| `WF-09-youtube-source.json` | Schedule(매시간) | n8n 스케줄 | `TOPIC_CHANNELS` 파싱, active notebook이 비어 있을 때만 RSS 조회, 최근 시간창 안의 최신 영상 1개만 선택 | source 추가 성공 시에만 gateway `/internal/auto-report` → WF-06 |
| `WF-10-daily-notebook.json` | Schedule(매일 07:30 KST) | n8n 스케줄 | 채널별 노트북 생성 요청 | notebooklm-service `/create-notebook` |
| `WF-11_tts_generate.json` | Webhook | `POST /webhook/wf-11-tts-generate` | TTS 생성, WAV 저장, Discord 전송, `audio_url` 저장 | 승인 대기 또는 자동 WF-12 |
| `WF-12_heygen_generate.json` | Webhook | `POST /webhook/WF12HeygenV2Run/webhook/wf-12-heygen-generate-v2` | HeyGen 생성/폴링 또는 mock preview 생성 | 성공→`/internal/send-video-preview`, 실패→`/internal/send-text` |

참고:
- 기존 단일 워크플로 `WF-07`은 삭제되었고, `WF-11`/`WF-12`로 완전 분리되었습니다.

---

## 사전 요구사항

- AWS EC2 t3.large (x86_64, Ubuntu 22.04 이상) 또는 동급 서버
- **Docker** + **Docker Compose v2.24+** 설치
- **Discord Bot Token** (Discord Developer Portal에서 발급)
  - Privileged Gateway Intents: **Message Content Intent** 활성화 필수
  - Bot Permissions: Send Messages, Read Message History, Add Reactions, Use Application Commands
- 인바운드 포트 오픈: **5678** (n8n), **8080** (messenger-gateway)

---

## 서버 초기 세팅 (최초 1회)

### 1. SSH 접속

```bash
ssh -i your-key.pem ubuntu@<서버-퍼블릭-IP>
```

### 2. Docker 설치

```bash
# 패키지 업데이트
sudo apt-get update && sudo apt-get upgrade -y

# Docker 공식 GPG 키 및 저장소 추가
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Docker 설치
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io
sudo usermod -aG docker $USER
newgrp docker
```

### 3. Docker Compose v2 설치

```bash
# x86_64 기준 (ARM이면 aarch64로 변경)
sudo curl -SL https://github.com/docker/compose/releases/download/v2.24.0/docker-compose-linux-x86_64 \
  -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose
docker-compose --version
# Docker Compose version v2.24.0
```

### 4. 코드 클론

```bash
# 저장소 전체 클론
git clone https://github.com/<org>/SKN22-Final-4Team-AI.git
cd SKN22-Final-4Team-AI/ai-influencer
```

---

## 배포 및 실행

### 5. 환경변수 설정

```bash
cp .env.example .env
nano .env   # 또는 vi .env
```

`.env` 파일은 .env.example 파일을 복사하여 사용해주세요.

**주요 환경변수:**

| 변수 | 설명 | 예시 |
|------|------|------|
| `POSTGRES_DB` | PostgreSQL DB명 | `ai_influencer` |
| `POSTGRES_USER` | DB 사용자 | `aiuser` |
| `POSTGRES_PASSWORD` | DB 비밀번호 | |
| `GATEWAY_INTERNAL_SECRET` | 내부 서비스 인증 키 | |
| `DISCORD_BOT_TOKEN` | Discord Bot 토큰 | |
| `DISCORD_ALLOWED_USER_IDS` | 허용 유저 ID (쉼표 구분) | `12345,67890` |
| `DISCORD_ALLOWED_CHANNEL_IDS` | 허용 채널 ID (쉼표 구분) | `11111,22222` |
| `DISCORD_GUILD_ID` | (선택) 슬래시 명령 즉시 반영용 Guild ID | `123456789012345678` |
| `N8N_RUNNERS_TASK_TIMEOUT` | n8n task runner 실행 제한(초) | `1200` |
| `NOTEBOOKLM_SERVICE_URL` | notebooklm-service 내부 URL | `http://notebooklm-service:8090` |
| `NOTEBOOKLM_MAX_SOURCES` | 채널당 최대 소스 수 | `20` |
| `WF09_LOOKBACK_HOURS` | WF-09 새 영상 판정 시간창(시간). 빈 active notebook에도 항상 적용됨 | `24` |
| `TOPIC_CHANNELS` | YouTube 채널 목록 | `노마드코더/UCUpJs89fSBXNolQGOYKn0YQ+조코딩/UCQNE2JmbasNYbjGAcuBiRRg` |
| `N8N_WF11_WEBHOOK_URL` | WF-11(TTS) 웹훅 URL | `http://n8n:5678/webhook/wf-11-tts-generate` |
| `N8N_WF12_WEBHOOK_URL` | WF-12(HeyGen) 웹훅 URL | `http://n8n:5678/webhook/WF12HeygenV2Run/webhook/wf-12-heygen-generate-v2` |
| `TTS_API_URL` | TTS API 서버 주소 | `https://...trycloudflare.com` |
| `TTS_REF_AUDIO_PATH` | (선택) 음색 클론용 참조 오디오 경로 | `/workspace/reference.wav` |
| `TTS_PROMPT_TEXT` | (선택) 참조 오디오 실제 문장 | `안녕하세요 ...` |
| `HEYGEN_API_KEY` | HeyGen Direct API 키 | |
| `HEYGEN_AVATAR_ID` | WF-12 마지막 fallback 아바타 ID | |
| `HEYGEN_VIDEO_WIDTH` | WF-12 출력 영상 너비 | `1080` |
| `HEYGEN_VIDEO_HEIGHT` | WF-12 출력 영상 높이 | `1920` |
| `HEYGEN_CAPTION_ENABLED` | HeyGen caption 사용 여부 | `false` |
| `HEYGEN_SPEED` | WF-12 audio voice speed | `1.3` |
| `HEYGEN_POLL_INTERVAL_SECONDS` | HeyGen 상태 polling 간격(초) | `10` |
| `HEYGEN_MAX_WAIT_SECONDS` | HeyGen 최대 대기 시간(초) | `900` |
| `HEYGEN_MOCK_ENABLED` | WF-12 mock preview 모드 | `false` |
| `HEYGEN_MOCK_VIDEO_URL` | mock 모드 샘플 mp4 URL | `https://samplelib.com/lib/preview/mp4/sample-5s.mp4` |
| `GOOGLE_EMAIL` | NotebookLM 구글 계정 | |
| `GOOGLE_PASSWORD` | NotebookLM 구글 비밀번호 | |
| `OPENAI_API_KEY` | OpenAI API 키 | |
| `OPENAI_CUA_API_KEY` | CUA 자동화 전용 OpenAI 키 (권장) | |

기본 경로: WF-11은 앞선 워크플로에서 전달된 `script_text`를 그대로 TTS 입력으로 사용합니다.  
음색 클론이 필요할 때만 `TTS_REF_AUDIO_PATH` + `TTS_PROMPT_TEXT`를 **둘 다** 설정하세요.

**`TOPIC_CHANNELS` 형식:**
```
TOPIC_CHANNELS=채널이름/채널ID+채널이름/채널ID+...
예: 노마드코더/UCUpJs89fSBXNolQGOYKn0YQ+조코딩/UCQNE2JmbasNYbjGAcuBiRRg
```
- 채널이름: 노트북 및 보고서 식별 키로 사용됨
- 채널ID: YouTube 채널 ID (UC로 시작하는 24자리)

자동 보고서(auto-report) 경로에서는 `DISCORD_ALLOWED_CHANNEL_IDS`의 **첫 번째 채널 ID만** 전송 대상으로 사용합니다.

### 6. 전체 서비스 빌드 및 기동

```bash
docker-compose up -d --build
```

상태 확인:

```bash
docker-compose ps
# 모든 서비스 State: Up 확인
```

로그 확인:

```bash
docker-compose logs -f               # 전체
docker-compose logs -f messenger-gateway
docker-compose logs -f discord-bot
docker-compose logs -f n8n
docker-compose logs -f notebooklm-service
```

---

## n8n 워크플로 설정

### 7. n8n 접속 및 Postgres 크레덴셜 등록

1. 브라우저에서 `http://<서버-퍼블릭-IP>:5678` 접속
2. `.env`의 `N8N_BASIC_AUTH_USER` / `N8N_BASIC_AUTH_PASSWORD`로 로그인
3. 좌측 메뉴 **Settings → Credentials → + New Credential → PostgreSQL** 선택
4. 아래 값 입력 후 **Save** (반드시 이름을 `pg-credentials`로 지정):

   | 항목 | 값 |
   |------|-----|
   | Name | `pg-credentials` |
   | Host | `postgres` |
   | Port | `5432` |
   | Database | `.env`의 `POSTGRES_DB` |
   | User | `.env`의 `POSTGRES_USER` |
   | Password | `.env`의 `POSTGRES_PASSWORD` |

### 8. 워크플로 임포트

1. 좌측 메뉴 **Workflows → + New Workflow**
2. 우상단 **⋮ (점 3개) → Import from file** 으로 아래 파일을 각각 임포트:

   | 파일 | 설명 | 트리거 |
   |------|------|--------|
   | `WF-01_input_receive.json` | 콘텐츠 생성 요청 수신 | Webhook |
   | `WF-04_confirm_request.json` | 컨펌 재요청 | Webhook |
   | `WF-05_confirm_handler.json` | 승인/수정 처리 | Webhook |
   | `WF-06_notebooklm_report.json` | 보고서 생성 | Webhook |
   | `WF-11_tts_generate.json` | TTS 생성 + Discord 공유 | Webhook |
   | `WF-12_heygen_generate.json` | HeyGen 영상 생성 | Webhook |
   | `WF-08_sns_upload.json` | SNS 업로드 | Webhook |
   | `WF-09-youtube-source.json` | YouTube 소스 자동 수집 | 매시간 Schedule |
   | `WF-10-daily-notebook.json` | 일일 노트북 생성 | 매일 07:30 KST Schedule |

3. Webhook 기반 워크플로(WF-01~08): **Postgres 노드** 클릭 → Credentials → `pg-credentials` 선택
4. 각 워크플로 우상단 **Active 토글 ON** → **Save**

> **워크플로 수정 시 재임포트 방법 (docker 재빌드 불필요):**
> ```bash
> git pull   # 로컬 변경사항 서버에 반영
> ```
> n8n UI에서 기존 워크플로 삭제 → 새 JSON 파일로 재임포트

---

## 헬스체크

```bash
# Gateway 상태
curl http://localhost:8080/health
# {"status":"ok","db":"connected","adapters":["discord"]}

# n8n 상태
curl http://localhost:5678/healthz
# {"status":"ok"}

# 컨테이너 상태
docker-compose ps

# Postgres DB 접속 확인
docker-compose exec postgres psql -U aiuser -d ai_influencer -c "SELECT COUNT(*) FROM jobs;"
```

---

## 업데이트 배포

코드/워크플로 변경 시:

```bash
cd ~/SKN22-Final-4Team-AI/ai-influencer
git pull

# 코드 변경 (messenger-gateway, discord-bot, notebooklm-service) → 재빌드 필요
docker-compose up -d --build messenger-gateway discord-bot notebooklm-service

# docker-compose.yml 또는 .env 변경 → 해당 서비스만 재시작
docker-compose up -d --force-recreate n8n

# 전체 재시작
docker-compose up -d --build
```

---

## Discord 봇 설정 (Discord Developer Portal)

1. [https://discord.com/developers/applications](https://discord.com/developers/applications) 접속
2. **New Application** 생성
3. **Bot** 탭 → **Reset Token** → 토큰 복사 → `.env`의 `DISCORD_BOT_TOKEN`에 입력
4. **Bot** 탭 → **Privileged Gateway Intents**
   - **MESSAGE CONTENT INTENT** 토글 활성화 (필수)
5. **OAuth2 → URL Generator**
   - Scopes: `bot`, `applications.commands`
   - Permissions: `Send Messages`, `Read Message History`, `Add Reactions`
6. 생성된 URL로 봇을 서버에 초대

### Discord ID 확인 방법

Discord 설정 → **고급 → 개발자 모드** 활성화 후:
- **유저 ID**: 사용자 이름 우클릭 → **ID 복사**
- **채널 ID**: 채널명 우클릭 → **ID 복사**

---

## Server Control Lambda 설정 (`/server` 명령어)

EC2가 꺼진 상태에서도 Discord에서 `/server on/off/status`로 인스턴스를 제어하는 별도 Lambda 봇.
기존 `discord-bot`과 독립적으로 동작하며 같은 채널에 공존합니다.

### 아키텍처

```
Discord 사용자 (/server on/off/status)
    ↓ HTTPS POST (WebSocket 불필요)
API Gateway (항상 ON, serverless)
    ↓
Lambda (항상 ON, ~무료)
    ├─ 서명 검증 (Ed25519 / PyNaCl)
    ├─ on     → ec2.start_instances()
    ├─ off    → ec2.stop_instances()
    └─ status → ec2.describe_instances()
```

### 수동 배포 순서

#### 1. Discord Application 생성

1. [discord.com/developers/applications](https://discord.com/developers/applications) → **New Application**
2. **Bot** 탭 → **Reset Token** → 토큰 복사
3. **General Information** → **Public Key** 복사

#### 2. AWS IAM 역할 생성

**신뢰 정책** (역할 → 신뢰 관계 탭):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "lambda.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
```

**권한 정책** (인라인 정책 추가):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ec2:StartInstances",
      "ec2:StopInstances",
      "ec2:DescribeInstances"
    ],
    "Resource": "*"
  }]
}
```

역할 이름: `discord-server-control-role`
추가 연결 정책: `AWSLambdaBasicExecutionRole` (CloudWatch 로그)

#### 3. Lambda 함수 생성 (AWS 콘솔)

1. AWS 콘솔 → **Lambda → 함수 생성 → 처음부터 작성**
   - 함수 이름: `discord-server-control`
   - 런타임: `Python 3.12`
   - 실행 역할 → 기존 역할 사용 → `discord-server-control-role`
2. **코드 업로드** — zip 파일 준비 후 업로드:
   ```bash
   cd ai-influencer/server-control-lambda
   mkdir package
   pip install PyNaCl==1.5.0 -t package/
   cp lambda_function.py package/
   cd package && zip -r ../function.zip . && cd ..
   ```
   함수 페이지 → **코드** 탭 → **업로드 위치 → .zip 파일** → `function.zip` 선택
3. **환경변수 설정** — **구성** 탭 → **환경 변수 → 편집**:

   | 키 | 값 |
   |----|-----|
   | `DISCORD_PUBLIC_KEY` | Discord 개발자 포털 Public Key |
   | `EC2_INSTANCE_ID` | `i-xxxxxxxxxxxxxxxxx` |
   | `EC2_REGION` | `ap-northeast-2` |
   | `DISCORD_ALLOWED_USER_IDS` | 허용할 Discord 유저 ID (쉼표 구분) |

4. **타임아웃 조정** — **구성** 탭 → **일반 구성 → 편집** → 타임아웃 `10초`

#### 4. API Gateway 생성

1. AWS 콘솔 → **API Gateway → HTTP API → 빌드**
2. 통합: **Lambda** → `discord-server-control` 선택
3. 라우트: `POST /discord`
4. 생성 후 **엔드포인트 URL** 복사

#### 5. Discord Interactions Endpoint 설정

1. Discord 개발자 포털 → 해당 Application → **General Information**
2. **Interactions Endpoint URL** = 4번에서 복사한 API Gateway URL
3. **Save Changes** → Discord가 PING 전송 → Lambda PONG 반환으로 자동 검증

#### 6. 슬래시 명령어 등록

```bash
cd ai-influencer/server-control-lambda
python register_command.py \
  --app-id <APPLICATION_ID> \
  --token  <BOT_TOKEN>
```

#### 7. 봇 서버 초대

1. Discord 개발자 포털 → **OAuth2 → URL Generator**
2. Scopes: `applications.commands`
3. 생성된 URL로 봇을 서버에 초대

### 환경변수 (`.env` 추가 항목)

| 변수 | 설명 |
|------|------|
| `EC2_INSTANCE_ID` | 제어할 EC2 인스턴스 ID |
| `EC2_REGION` | 인스턴스 리전 (기본값: `ap-northeast-2`) |
| `SERVER_CONTROL_DISCORD_PUBLIC_KEY` | Discord 개발자 포털 Public Key |
| `SERVER_CONTROL_DISCORD_APP_ID` | Discord Application ID |
| `SERVER_CONTROL_DISCORD_BOT_TOKEN` | Discord Bot Token |

---

## API 엔드포인트 (messenger-gateway)

모든 `/internal/*` 엔드포인트는 `X-Internal-Secret` 헤더 인증 필수.

| Method | Path | 호출자 | 설명 |
|--------|------|--------|------|
| `POST` | `/internal/message` | discord-bot | /create 요청 수신 |
| `POST` | `/internal/send-confirm` | n8n WF-01/04 | 스크립트 컨펌 버튼 전송 |
| `POST` | `/internal/confirm-action` | discord-bot | 승인/수정 버튼 클릭 처리 |
| `POST` | `/internal/send-text` | n8n WF-05/08 | 일반 텍스트 전송 |
| `POST` | `/internal/video-action` | discord-bot | 영상 승인/재작업 버튼 처리 |
| `POST` | `/internal/send-video-preview` | n8n WF-12 | 영상 미리보기 전송 |
| `POST` | `/internal/send-audio` | n8n WF-11 | TTS WAV + 승인/반려 버튼 전송 |
| `POST` | `/internal/tts-action` | discord-bot | TTS 승인/반려 처리 (WF-12 트리거) |
| `POST` | `/internal/report-message` | discord-bot | /report 요청 → 채널 선택 버튼 |
| `POST` | `/internal/channel-select` | discord-bot | 채널 버튼 클릭 → 보고서 목록 |
| `POST` | `/internal/report-select` | discord-bot | 보고서 선택 또는 새로 생성 |
| `POST` | `/internal/send-report` | n8n WF-06 | 보고서 텍스트 전송 |
| `POST` | `/internal/auto-report` | n8n WF-09 | 소스 추가 성공 시 자동 WF-06 생성/전송 트리거 |
| `POST` | `/internal/report-to-tts` | discord-bot | 보고서 → TTS만 제작 요청 |
| `POST` | `/internal/report-to-video` | discord-bot | 보고서 → 영상 제작 요청 |
| `POST` | `/internal/tts-generate` | discord-bot | `/tts [job_id]` 수동 WF-11 실행 (미입력 시 최근 job 자동 선택) |
| `POST` | `/internal/heygen-generate` | discord-bot | `/heygen [job_id]` 수동 WF-12 실행 (미입력 시 최근 job 자동 선택) |
| `POST` | `/internal/jobs` | discord-bot | `/jobs [purpose]` 최근 job 목록 조회 (`all/tts/heygen`) |
| `GET`  | `/health` | 모니터링 | 헬스체크 |

---

## 테스트 시나리오

### 수집 대상 채널
1. https://www.youtube.com/@nomadcoders
2. https://www.youtube.com/@jocoding
3. https://www.youtube.com/@Fireship
4. https://www.youtube.com/@t3dotgg
5. https://www.youtube.com/@matthew_berman
6. https://www.youtube.com/@TwoMinutePapers

### 콘텐츠 생성 (/create)

1. Discord에서 `/create concept:20대 여성을 위한 재테크 팁` 실행
   → "✅ 요청이 접수되었습니다!" 메시지 확인
2. WF-01 자동 실행 → 스크립트 + `[✅ 승인하기] [✏️ 수정 지시]` 버튼 수신
3. **✅ 승인하기** → WF-11 실행 → WF-11 TTS 생성
4. TTS 완료 → `[✅ 승인 (WF-12 진행)] [❌ 반려]` 버튼 수신
5. TTS 승인 → WF-12 실행 → 영상 미리보기 수신 후 승인 시 WF-08 실행

### 보고서 조회 (/report)

1. Discord에서 `/report` 실행
   → 채널 선택 버튼 표시 (TOPIC_CHANNELS에 등록된 채널 수만큼)
2. 채널 버튼 클릭 → 해당 채널의 보고서 목록 표시
   (목록 조회 실패/지연 시 `[🔄 다시 조회] / [🆕 새로 생성]` 버튼 표시)
3. 보고서 선택 또는 [새로 생성] → 보고서 텍스트 수신
4. `[🔊 TTS만 제작]` 버튼 클릭 → WF-11 실행 (TTS 승인/반려 버튼 노출)
5. `[🎬 영상으로 제작]` 버튼 클릭 → WF-11 실행 후 WF-12 자동 진행

### 수동 TTS / HeyGen 실행

1. Discord에서 `/jobs purpose:tts` 또는 `/jobs purpose:heygen` 실행  
   → 최근 job 목록(8자리 short id, 상태, script/audio 보유 여부) 확인
2. Discord에서 `/tts job_id:<8자리 또는 전체 job_id>` 실행  
   → 해당 job의 `script_text`로 WF-11(TTS) 수동 실행
3. Discord에서 `/heygen job_id:<8자리 또는 전체 job_id>` 실행  
   → 해당 job의 `audio_url`로 WF-12(HeyGen) 수동 실행
4. `job_id`를 생략하고 `/tts` 또는 `/heygen`만 실행하면  
   → 현재 채널/사용자의 최근 적합 job을 자동 선택해 실행

사전조건:
- `/tts`: job에 `script_json.script_text`(또는 `script`)가 있어야 함
- `/heygen`: job에 `audio_url`이 있어야 함

> Discord에서 옵션이 여전히 `필수`로 보이면, 봇 재배포 후 슬래시 명령 재동기화가 필요합니다.  
> `DISCORD_GUILD_ID`를 설정하면 길드 단위로 즉시 동기화됩니다.

### 대본→TTS→S3 저장 검증 (운영 E2E)

아래 스크립트로 특정 `job_id`의 WF-11 경로를 한 번에 검증할 수 있습니다.

```bash
cd ai-influencer
./scripts/verify_tts_to_s3.sh <job_id> --since 60m
```

검증 항목:
- gateway 로그에서 `/internal/send-audio` 완료 여부
- DB `jobs`에서 `script_text` 길이, `audio_url(s3://...)`, `media_names.audio_filename`
- `audio_url`이 가리키는 S3 객체 `HEAD` 성공(크기 > 0)

PASS 기준:
- `send_audio done job_id=<job_id>`
- `audio_url = s3://<bucket>/tts/YYYYMMDD-<job_id>.wav`
- S3 HEAD 성공

실패 시 우선 점검:
- `MEDIA_S3_ROLE_ARN`, `MEDIA_S3_BUCKET`, `MEDIA_S3_REGION`, `MEDIA_S3_EXTERNAL_ID`
- 타겟 계정 IAM Trust/Permission(`sts:AssumeRole`, `s3:PutObject/GetObject`)
- n8n WF-11 최신 워크플로 재임포트/재기동 여부

### 자동 수집 확인

```bash
# WF-09 수동 실행 (n8n UI에서 Execute 클릭)
# → notebooklm-service 로그에서 소스 추가 확인
docker-compose logs -f notebooklm-service

# WF-10 수동 실행
# → library.json에 channels[channel_id].notebook_url 생성 확인
cat notebooklm-service/data/library.json | python3 -m json.tool | grep '"channels"' -A 80
```

---

## 트러블슈팅

| 증상 | 확인 사항 |
|------|-----------|
| Discord 메시지 수신 안 됨 | Developer Portal → MESSAGE CONTENT INTENT 활성화 확인 |
| Gateway 401 응답 | `X-Internal-Secret` 값이 `.env`의 `GATEWAY_INTERNAL_SECRET`과 일치하는지 확인 |
| n8n Postgres 연결 실패 | `postgres` 컨테이너 healthy 상태 대기 (`docker-compose ps`) |
| n8n `$env.*` 접근 거부 | `docker-compose.yml`에 `N8N_BLOCK_ENV_ACCESS_IN_NODE: "false"` 추가 후 재시작 |
| n8n 환경변수 미반영 | `docker-compose up -d --force-recreate n8n` (재빌드 아닌 재생성) |
| n8n 워크플로 import 오류 | 기존 워크플로 삭제 후 재임포트. Postgres 크레덴셜 재선택 필수 |
| n8n `http://IP:5678` 접속 불가 | EC2 보안그룹 인바운드 5678 포트 오픈 확인 |
| 봇이 모든 채널에서 응답 | `.env`에 `DISCORD_ALLOWED_CHANNEL_IDS` 추가 후 `docker-compose up -d --build discord-bot` |
| WF-09 "채널 있는 경우만" false | `TOPIC_CHANNELS`가 n8n 컨테이너에 주입됐는지 docker-compose.yml 확인 |
| WF-09에서 특정 채널만 보이는 것처럼 보임 | `채널 목록 파싱` 로그의 `valid/invalid` 개수 확인. malformed 항목(예: `TwoM>`)은 `skip=true`로 노출되며 해당 항목은 탐색 대상에서 제외됨 |
| WF-09가 첫 채널만 보고 종료됨 | WF-09 코드 노드 실행 모드 확인: `채널 목록 파싱=runOnceForAllItems`, `RSS 조회 + 새 영상 필터링=runOnceForEachItem`, `결과 로깅=runOnceForEachItem` |
| WF-09 RSS 일시 오류(404/500) 후 0건 판정 | WF-09 최신 워크플로 재임포트 + n8n 재시작, 로그에서 `attempt` 재시도 후 `mode=xml-string/parsed-object` 성공 로그 확인 |
| WF-09 새 영상 기준 시간 변경 | `.env`의 `WF09_LOOKBACK_HOURS` 값을 수정(기본 24). 적용 후 `docker-compose up -d --force-recreate n8n` 실행 |
| WF-09 `Task execution timed out after 300 seconds` | `.env`에 `N8N_RUNNERS_TASK_TIMEOUT=1200` 설정 + `docker-compose up -d --force-recreate n8n` 적용. WF-09는 긴 단일 실행 대신 다음 스케줄 재시도 전략 사용 |
| WF-09 `A 'json' property isn't an object` | `RSS 조회 + 새 영상 필터링` 노드가 `runOnceForEachItem`이면 **반드시 단일 `{ json: {...} }` 반환**해야 함. 최신 워크플로 재임포트 후 재실행 |
| WF-09 HTTP Body "Field required" | n8n HTTP Request 노드 `specifyBody: "json"` + `jsonBody: JSON.stringify(...)` 방식 사용 확인 |
| notebooklm-service 연결 실패 | `NOTEBOOKLM_SERVICE_URL=http://notebooklm-service:8090` 형식(`=` 사용) 및 same network 확인 |
| `/report` 채널 버튼에 삭제된 채널이 계속 보임 | 버튼 소스는 `TOPIC_CHANNELS` 기준. `.env` 수정 후 `docker-compose up -d --force-recreate messenger-gateway` 적용 |
| `/report` 버튼 클릭 반응 없음 | discord-bot/gateway 최신 빌드 반영 확인: `docker-compose up -d --build discord-bot messenger-gateway` |
| `/report` 채널 선택 후 목록이 안 뜨고 멈춘 것처럼 보임 | gateway 로그 `[channel-select:bg]`에서 list-reports 응답 여부 확인. 최신 버전은 최대 180초 대기 후 실패 시 `다시 조회/새로 생성` 버튼을 노출 |
| `/report` 채널 선택 직후 `Temporary failure in name resolution` | 최신 gateway는 일시 DNS 오류를 자동 재시도(최대 3회)합니다. 계속 실패하면 1) `.env`의 `NOTEBOOKLM_SERVICE_URL=http://notebooklm-service:8090` 확인 2) `docker-compose exec messenger-gateway getent hosts notebooklm-service`로 DNS 확인 3) `docker-compose up -d --force-recreate messenger-gateway notebooklm-service` 재생성 |
| `/report`에서 기존 보고서가 있는데도 새 생성만 보임 | notebooklm-service 로그의 `[CUA][LIST] 종료 요약`(`elapsed/scroll/collect/premature_done/termination_reason`)을 먼저 확인. 조기 `done`은 무시되고 최소 탐색 게이트(시간/스크롤/스텝) 미충족이면 빈목록 성공으로 처리하지 않으며, 이 경우 `다시 조회` 버튼으로 재시도 |
| `/report`에서 `Looks like Playwright ... install` 또는 브라우저 실행 Traceback | `notebooklm-service` 이미지를 최신으로 재빌드해 Chromium 포함 여부를 반영: `docker-compose build --no-cache notebooklm-service && docker-compose up -d notebooklm-service` |
| WF-09 소스 추가는 성공했는데 자동 보고서가 안 옴 | gateway에 `DISCORD_ALLOWED_CHANNEL_IDS`가 주입됐는지 확인. 값이 비어 있으면 `/internal/auto-report`가 실패함 |
| 자동 보고서가 예상 채널이 아닌 곳에 옴 | auto-report는 `DISCORD_ALLOWED_CHANNEL_IDS`의 첫 번째 채널만 사용함. 순서 변경 후 gateway 재기동 필요 |
| TTS 승인 버튼 눌러도 WF-12 미실행 | `.env`의 `N8N_WF12_WEBHOOK_URL` 확인 + `docker-compose up -d --build messenger-gateway discord-bot` 재배포 |
| CUA가 잘못된 페이지로 이동하거나 키 노출 우려 | `OPENAI_CUA_API_KEY` 분리 사용 + NotebookLM/Google 로그인 외 도메인 접근 차단(최신 코드) |
| Gateway DB 연결 실패 | postgres healthcheck 통과 여부 및 환경변수 확인 |

---

## Phase 로드맵

| Phase | 내용 | 상태 |
|-------|------|------|
| **Phase 1** | Discord 메신저 파이프라인 | ✅ 완료 |
| **Phase 2** | NotebookLM 연동 — 채널별 소스 수집 + 보고서 생성 | ✅ 완료 |
| **Phase 3** | WF-11/12 분리(TTS + HeyGen) | ✅ 완료 |
| **Phase 4** | SNS 자동 업로드 (YouTube, Instagram, TikTok) | ✅ 완료 |

---

## RunPod & Cloudflare 서버 구축 튜토리얼 (GPT-SoVITS-v4 실습)

### 1. RunPod 환경 구성
- RunPod에서 **RTX 5090** 인스턴스를 생성합니다.
- 포트 번호를 `8888, 9874, 9880, 9872`로 설정하여 열어줍니다.
- 8888 포트를 통해 Jupyter Notebook으로 접속한 후, **Terminal 1**을 켭니다.

### 2. 로컬 서버 실행
```bash
# 디렉터리 이동
cd /workspace/GPT-SoVITS-v4-real

# Python 가상환경(venv) 활성화
source /workspace/GPT-SoVITS-v4/venv/bin/activate

# 로컬호스트 서버 실행 (API v2)
python api_v2.py
```

### 3. Cloudflare 터널링으로 퍼블릭 URL 생성
- 새 터미널(**Terminal 2**)을 켭니다.
- Cloudflare 터널링 도구를 실행하기 위해 가상환경을 활성화합니다.
- 포트 9880을 연결하여 퍼블릭 URL을 만들어줍니다.
```bash
# Python 가상환경(venv) 활성화
source /workspace/GPT-SoVITS-v4/venv/bin/activate

cloudflared tunnel --url http://127.0.0.1:9880/
```

### 트러블슈팅: Cloudflare 관련 오류 발생 시
만약 `cloudflared` 실행 중 오류가 나거나 명령어가 없으면 아래로 수동 설치를 진행합니다.

```bash
# 1. 최신 패키지 리스트 업데이트
apt-get update

# 2. cloudflared 다운로드용 도구 설치 (이미 있을 수도 있음)
apt-get install -y wget

# 3. cloudflared 최신 버전 다운로드 (Linux 64-bit 기준)
wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb

# 4. 다운로드한 패키지 설치
dpkg -i cloudflared-linux-amd64.deb
```

# 프롬프트

- _SCRIPT_REWRITE_PROMPT_BASE = (
    """
    당신은 20대 초반의 발랄한 성격을 가진 숏폼 인플루언서 '하리'입니다. 
    팬덤명은 '보리'입니다.
    규칙을 엄격하게 준수하여, [소스 내용]을 소개하는 "350자" 분량의 대본을 작성해 주세요.

    1. 출력 형식 제한: 대본의 제목, 화자 이름(하리:), 지문(예: [오프닝], [본문]) 등 대사가 아닌 모든 글자 및 기호는 절대 작성하지 마세요. 오직 화면에서 읽을 '대사'만 텍스트로 출력해야 합니다.
    2. 100% 한글 표기: 알파벳(영어)과 숫자는 절대 사용하지 마세요. 모두 한글 발음으로 변환하여 적어주세요. (예시: AI -> 에이아이, 350 -> 삼백오십)
    3. 마크다운 금지: 굵게(**), 기울임 등 어떠한 마크다운 문법도 사용하지 마세요.
    4. TTS 최적화 (가장 중요):
    - 사전에 없는 단어는 실제 한국어 발음대로 표기하세요. (예시: 역대급 -> 역대끕)
    - 숫자 관련 발음은 붙여 표기하세요. (예시: 오 점 사 -> 오쩜사)
    - 발랄한 억양을 살리기 위해 물음표(?)를 적극적으로 사용하세요.
    - 오늘, 어제 등 상대적인 날짜를 표기하지 마세요.
    - 매 문장이 끝날 때마다 반드시 "줄바꿈"(엔터)을 하세요.

    [대본 구성]
    오프닝(인사) - 본문(소스 내용 소개) - 마무리(엔딩)의 자연스러운 흐름으로 작성할 것.
"""
