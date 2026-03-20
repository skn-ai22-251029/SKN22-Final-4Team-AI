# 🎥 HARI: The AI Virtual Influencer
> **"AI 에이전트 시대, 팬 경험을 스케일링하는 새로운 테크 크리에이터"**

<p align="center">
  <a href="https://linktr.ee/chatting_hari">
    <img src="https://img.shields.io/badge/Linktree-하리_공식_링크-39E09B?style=for-the-badge&logo=linktree&logoColor=white" alt="HARI Linktree"/>
  </a>
</p>

<p align="center">
  <img width="1024" height="1024" alt="Image" src="https://github.com/user-attachments/assets/19d961a5-22cc-4c22-84a7-677388c86e5f" />
</p>

---

## 0. Project Overview
**하리(HARI)**는 단순한 정보 전달용 챗봇을 넘어, 사용자 전용 '페르소나'를 가진 테크 전문 가상 인플루언서 플랫폼입니다. AI 에이전트 시대를 맞아, 1:1 대화, 실시간 음성 스트리밍, SNS 콘텐츠 자동 생성을 통해 사용자에게 실존하는 셀럽과의 교감(Parasocial Interaction)을 제공합니다.

* **진행 기간:** 2026.03.04 ~ 2026.04.24 (SK네트웍스 Family AI 22기)
* **공식 링크:** [하리 링크트리 바로가기](https://linktr.ee/chatting_hari)
* **핵심 가치:** 단방향 콘텐츠 소비에서 벗어난 일상 속 '과몰입' 상호작용 구현

---

## 1. Key Features
* **💬 1:1 Private Chat:** 유저별 맥락을 기억(Memory)하고 실시간 음성(TTS)으로 대답하는 개인화 채팅.
* **🎬 Auto-Content Pipeline:** 최신 테크 트렌드를 분석하여 숏폼 영상과 SNS 피드를 자동으로 생성 및 업로드.
* **👤 Consistent Persona:** LoRA 학습을 통한 외형 일관성 유지 및 고유의 말투/취향(Preference) 반영.
* **🛒 Point Shop:** 활동 점수를 통한 굿즈 구매 및 멤버십 전용 콘텐츠 제공.

---

## 2. Tech Stack

### AI Modeling
* **LLM:** LangChain, LangGraph (Multi-turn 대화 및 가드레일 제어)
* **Voice:** GPT-SoVITS v4 (성우 데이터 기반 고정밀 음색 복제)
* **Vision:** Z-Image-turbo(이미지 생성), AI-toolkit(LoRA 학습), Heygen API (영상 생성)
* **Preprocessing:** Pandas, Seedvr2 (이미지 업스케일링), Z-image-Turbo

### Infrastructure & Backend
* **Servers:** AWS (EC2, Elastic Beanstalk), Runpod (RTX 5090 GPU 인스턴스)
* **Frameworks:** Django, FastAPI (WebSockets 실시간 통신)
* **Automation:** n8n, Playwright CUA (SNS 스크래핑), Celery & Redis (작업 큐 관리)
* **Database:** PostgreSQL (유저 및 페르소나 데이터, Vector DB, RAG 지식 검색)

---

## 3. System Architecture

<p align="center">
  <img width="3840" height="1680" alt="Image" src="https://github.com/user-attachments/assets/0e7b97a3-f3d0-487d-9157-e485a5109137" />
</p>

위 아키텍처 다이어그램은 하리 플랫폼의 전체적인 데이터 흐름과 컴포넌트 간 상호작용을 시각화합니다.
1. **Automation Pipeline (AWS Cloud 1):** SNS 트렌드 분석 및 대본 생성, 영상 제작 오케스트레이션.
2. **Web Server (AWS Cloud 2):** Django 기반의 서비스 로직 및 LangChain을 활용한 대화 엔진 구동.
3. **GPU Instance (Runpod):** 이미지 생성 및 TTS 추론 등 고부하 연산 전담.
4. **Database Layer:** 유저별 대화 이력 및 하리의 페르소나 벡터값 저장/조회.

---

## 4. Data Preprocessing & Training
* **TTS Data:** 전문 성우 녹음본 40문장 정제 (**LSD 9.27, Similarity 0.9636** 확보)
* **Persona Data:** 놉크릭 버번, 테크 트렌드 등 구체적인 취향 데이터를 수기 구축하여 RAG 시스템에 이식.
* **Image Data:** 캐릭터 일관성을 위해 고유 LoRA 가중치 모델(`safetensors`) 제작.

---

## 5. Getting Started

### Prerequisites
* Python 3.10+
* NVIDIA GPU (VRAM 16GB+ 권장)
* AWS & OpenAI API Keys

### Installation
```bash
# Repository 클론
git clone [https://github.com/skn-ai22-251029/SKN22-Final-4Team-Web.git](https://github.com/skn-ai22-251029/SKN22-Final-4Team-Web.git)

# 백엔드 디렉토리 이동 및 패키지 설치
cd SKN22-Final-4Team-Web/backend
pip install -r requirements.txt

# 서버 실행 (Django)
python manage.py runserver
```

---

## 6. Team Members (SKN22-Final-4Team)

| 사진 | 이름 | 역할 | 주요 업무 |
| :---: | :--- | :--- | :--- |
| <img width="60" alt="Image" src="https://github.com/user-attachments/assets/17c43ef6-fbc6-484e-9fe2-09b365c283d1" /> | **최민호** | **PM** | PM, BM 개발, 시장 조사, 데이터 수집 총괄 |
| <img width="60" alt="Image" src="https://github.com/user-attachments/assets/28447344-fbb5-4f26-90bb-11ad3a8fd477" /> | **박준석** | **Creative** | 세계관 구축, UI/UX 설계, 데이터 수집 |
| <img width="60" alt="Image" src="https://github.com/user-attachments/assets/71c56c29-1306-4cd4-9fe3-78c1b7942096" /> | **안민제** | **AI Lead** | TTS(GPT-SoVITS) 학습 및 생성, LLM TTS 스트리밍 설계 |
| <img width="60" alt="Image" src="https://github.com/user-attachments/assets/c9470eb1-95db-43b0-b8f0-0d953a559891" /> | **한승혁** | **Infra** | 클라우드 서버 관리, 이미지/영상 학습 및 생성 |
| <img width="60" alt="Image" src="https://github.com/user-attachments/assets/05847c89-5f59-4184-81b3-355e85a85fa5" /> | **엄형은** | **Contents** | 대본 생성 자동화, 콘텐츠 생성-업로드 파이프라인 구축 |

---

## 7. License

본 프로젝트는 **SK네트웍스 Family AI 22기** 교육 과정의 일환으로 제작되었으며, 모든 권리는 **SKN22-Final-4Team**에 있습니다.
