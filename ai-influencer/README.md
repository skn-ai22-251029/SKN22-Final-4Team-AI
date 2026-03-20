# AI Influencer Automation Pipeline — Phase 1

Discord 기반 AI 인플루언서 자동화 파이프라인.

---

## 아키텍처 개요

```
[Discord 사용자]
      │  /create concept   /report
      ▼
[discord-bot]  ──────────────────────────────────→  [messenger-gateway :8080]
                                                              │  /internal/*
                                    ┌─────────────────────────┤
                                    ▼                         ▼
                             [PostgreSQL]              [n8n :5678]
                                    ▲                  WF-01 ~ WF-10
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
       │                       [컨펌 버튼 전송]            [채널 선택]
[WF-10: 매일 노트북 생성]           │                          │
                               [WF-05: 승인/수정]         [보고서 목록 조회]
                                    │ 승인                      │
                               [WF-07: TTS+영상 생성]    [WF-06: 보고서 생성]
                                    │                          │
                               [WF-08: SNS 업로드]        [영상 제작 요청]
```

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
      ├─[approved]──────────────────────────────────────────→ [WF-07 Webhook 호출]
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
[요청 파싱] job_id, prompt, notebook_id, topic
      │
      ▼
[DB 업데이트] status=GENERATING
      │
      ▼
[notebooklm-service /generate 호출]
      │
      ├─[성공]──→ [gateway /internal/send-report 호출]
      │           → Discord에 보고서 텍스트 + [🎬 영상 제작] 버튼 전송
      │
      └─[실패]──→ [gateway /internal/send-text 호출]
                  → Discord에 오류 메시지 전송
```

---

### WF-07: TTS + HeyGen 영상 생성

```
[Webhook 수신] ← WF-05 승인 후 호출
      │
      ▼
[요청 파싱] job_id, script
      │
      ▼
[DB 업데이트] status=GENERATING
      │
      ▼
[TTS 생성] 스크립트 → 음성 파일
      │
      ▼
[HeyGen 영상 생성 요청] video_id 발급
      │
      ▼
[DB 저장] video_id 저장
      │
      ▼
[폴링 루프] HeyGen 상태 주기적 조회
      │
      ├─[completed]──→ [DB 업데이트] status=WAITING_VIDEO_APPROVAL
      │                      │
      │                      ▼
      │               [gateway /internal/send-video-preview 호출]
      │               → Discord에 영상 미리보기 + [✅ 승인] [❌ 재작업] 버튼 전송
      │
      └─[failed]────→ [DB 업데이트] status=FAILED
                             │
                             ▼
                      [gateway /internal/send-text 호출]
                      → Discord에 오류 메시지 전송
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
  → [{channelId, topic(=채널이름)}, ...] 목록 생성
      │
      ▼
[IF: 채널 존재 여부 확인]
  ├─[skip=true]──→ 종료
  │
  └─[유효]──→ [YouTube RSS 조회] (채널별 병렬 처리)
                https://www.youtube.com/feeds/videos.xml?channel_id={channelId}
                      │
                      ▼
              [새 영상 필터링] 최근 1.5시간(90분) 이내 업로드만 추출
                      │
                      ▼
              [IF: 새 영상 존재 여부]
                ├─[없음]──→ 종료 (해당 채널)
                │
                └─[있음]──→ [notebooklm-service /check-and-add-source 호출]
                              { source_url, source_title, topic(=채널이름) }
                                    │
                                    ▼
                            [결과 로깅]
                            중복 건너뜀 / 소스 추가 완료 / 오류
```

---

### WF-10: 일일 노트북 생성 (매일 자정)

```
[Schedule Trigger] 매일 00:00 실행
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
               [notebooklm-service /all-channels 조회]
               → library.json의 모든 채널명 반환
                        │
                        ▼
               [Discord 채널 선택 버튼 전송]
               [채널A] [채널B] [채널C] ...
                        │
      사용자가 채널 버튼 클릭
                        │
                        ▼
[discord-bot] → [gateway /internal/channel-select]
                 { job_id, channel_name }
                        │
                        ▼
               [notebooklm-service /list-reports 조회]
               → 해당 채널의 기존 보고서 목록
                        │
                ┌───────┴───────────────┐
         [보고서 있음]            [보고서 없음]
                │                      │
                ▼                      ▼
   [Discord 보고서 선택 버튼]   [WF-06 즉시 실행]
   [보고서1] [보고서2] [새로 생성]  → 보고서 생성
                │
      사용자가 선택
                │
      ├─[기존 보고서 선택]──→ Discord에 보고서 전송
      │
      └─[새로 생성]────────→ [WF-06 실행] → 보고서 생성 → 전송
```

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
git clone https://github.com/<org>/SKN22-Final-4Team-WEB.git
cd SKN22-Final-4Team-WEB/ai-influencer
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
| `NOTEBOOKLM_SERVICE_URL` | notebooklm-service 내부 URL | `http://notebooklm-service:8000` |
| `NOTEBOOKLM_MAX_SOURCES` | 채널당 최대 소스 수 | `20` |
| `TOPIC_CHANNELS` | YouTube 채널 목록 | `노마드코더/UCUpJs89fSBXNolQGOYKn0YQ+조코딩/UCQNE2JmbasNYbjGAcuBiRRg` |
| `GOOGLE_EMAIL` | NotebookLM 구글 계정 | |
| `GOOGLE_PASSWORD` | NotebookLM 구글 비밀번호 | |
| `OPENAI_API_KEY` | OpenAI API 키 | |

**`TOPIC_CHANNELS` 형식:**
```
TOPIC_CHANNELS=채널이름/채널ID+채널이름/채널ID+...
예: 노마드코더/UCUpJs89fSBXNolQGOYKn0YQ+조코딩/UCQNE2JmbasNYbjGAcuBiRRg
```
- 채널이름: 노트북 및 보고서 식별 키로 사용됨
- 채널ID: YouTube 채널 ID (UC로 시작하는 24자리)

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
   | `WF-07_tts_heygen.json` | TTS + HeyGen 영상 생성 | Webhook |
   | `WF-08_sns_upload.json` | SNS 업로드 | Webhook |
   | `WF-09-youtube-source.json` | YouTube 소스 자동 수집 | 매시간 Schedule |
   | `WF-10-daily-notebook.json` | 일일 노트북 생성 | 매일 자정 Schedule |

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
cd ~/SKN22-Final-4Team-WEB/ai-influencer
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
| `POST` | `/internal/send-video-preview` | n8n WF-07 | 영상 미리보기 전송 |
| `POST` | `/internal/report-message` | discord-bot | /report 요청 → 채널 선택 버튼 |
| `POST` | `/internal/channel-select` | discord-bot | 채널 버튼 클릭 → 보고서 목록 |
| `POST` | `/internal/report-select` | discord-bot | 보고서 선택 또는 새로 생성 |
| `POST` | `/internal/send-report` | n8n WF-06 | 보고서 텍스트 전송 |
| `POST` | `/internal/report-to-video` | discord-bot | 보고서 → 영상 제작 요청 |
| `GET`  | `/health` | 모니터링 | 헬스체크 |

---

## 테스트 시나리오

### 콘텐츠 생성 (/create)

1. Discord에서 `/create concept:20대 여성을 위한 재테크 팁` 실행
   → "✅ 요청이 접수되었습니다!" 메시지 확인
2. WF-01 자동 실행 → 스크립트 + `[✅ 승인하기] [✏️ 수정 지시]` 버튼 수신
3. **✅ 승인하기** → WF-07 실행 → TTS + HeyGen 영상 생성
4. 영상 완료 → `[✅ 승인] [❌ 재작업]` 버튼 수신
5. 영상 승인 → WF-08 실행 → SNS 업로드 완료 알림

### 보고서 조회 (/report)

1. Discord에서 `/report` 실행
   → 채널 선택 버튼 표시 (TOPIC_CHANNELS에 등록된 채널 수만큼)
2. 채널 버튼 클릭 → 해당 채널의 보고서 목록 표시
3. 보고서 선택 또는 [새로 생성] → 보고서 텍스트 수신
4. `[🎬 영상 제작]` 버튼 클릭 → WF-06/07 실행

### 자동 수집 확인

```bash
# WF-09 수동 실행 (n8n UI에서 Execute 클릭)
# → notebooklm-service 로그에서 소스 추가 확인
docker-compose logs -f notebooklm-service

# WF-10 수동 실행
# → library.json에 채널별 노트북 생성 확인
cat notebooklm-service/data/library.json | python3 -m json.tool | grep '"topics"' -A 50
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
| WF-09 HTTP Body "Field required" | n8n HTTP Request 노드 `specifyBody: "json"` + `jsonBody: JSON.stringify(...)` 방식 사용 확인 |
| notebooklm-service 연결 실패 | `NOTEBOOKLM_SERVICE_URL=http://notebooklm-service:8000` 형식 및 same network 확인 |
| Gateway DB 연결 실패 | postgres healthcheck 통과 여부 및 환경변수 확인 |

---

## Phase 로드맵

| Phase | 내용 | 상태 |
|-------|------|------|
| **Phase 1** | Discord 메신저 파이프라인 | ✅ 완료 |
| **Phase 2** | NotebookLM 연동 — 채널별 소스 수집 + 보고서 생성 | ✅ 완료 |
| **Phase 3** | TTS + HeyGen 영상 자동 생성 | ✅ 완료 |
| **Phase 4** | SNS 자동 업로드 (YouTube, Instagram, TikTok) | ✅ 완료 |
