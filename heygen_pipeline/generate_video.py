"""
HeyGen 아바타 영상 자동 생성 파이프라인 (WAV 오디오 입력 버전)

[흐름]
  1. WAV 파일 → MP3 변환 (ffmpeg)
  2. MP3 → HeyGen Upload Asset API로 업로드 → audio_asset_id 획득
  3. HeyGen API → talking_photo + audio 기반 영상 생성
  4. 렌더링 대기 → 영상 다운로드
  5. (선택) 스크립트 텍스트 → DB 자동 등록 + 임베딩

[필요 사전조건]
  - ffmpeg 가 PATH에 있어야 함
    Windows: https://ffmpeg.org/download.html 에서 설치 후 PATH 등록
"""

import argparse
import requests
import time
import os
import json
import sys
import subprocess
import glob
from datetime import datetime
from dotenv import load_dotenv

# .env 파일 로드 (heygen_pipeline/.env 먼저, 없으면 프로젝트 루트 .env)
_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, ".env"))
load_dotenv(os.path.join(_HERE, "..", ".env"))


# ============================================================
# 설정
# ============================================================
API_KEY = os.environ.get("HEYGEN_API_KEY")
if not API_KEY:
    raise ValueError(".env 파일에서 HEYGEN_API_KEY를 찾을 수 없습니다.")
BASE_URL = "https://api.heygen.com"
UPLOAD_URL = "https://upload.heygen.com/v1/asset"
HEADERS = {
    "X-Api-Key": API_KEY,
    "Content-Type": "application/json",
    "Accept": "application/json",
}

RAPI_TALKING_PHOTO_ID = "b903a1fd1ec846e0ba2e89620bc0aaae"

INPUT_AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "input_audio")
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output_videos")
os.makedirs(INPUT_AUDIO_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ============================================================
# 1. WAV → MP3 변환
# ============================================================
def convert_wav_to_mp3(wav_path: str) -> str:
    """ffmpeg으로 WAV를 MP3로 변환. 변환된 MP3 경로 반환."""
    mp3_path = os.path.splitext(wav_path)[0] + ".mp3"
    cmd = [
        "ffmpeg", "-y",
        "-i", wav_path,
        "-codec:a", "libmp3lame",
        "-qscale:a", "2",  # 고품질 VBR
        mp3_path,
    ]
    print(f"\n🔄 WAV → MP3 변환 중... → {os.path.basename(mp3_path)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg WAV→MP3 변환 실패: {result.stderr.strip()}")
    print(f"   ✅ 변환 완료! ({os.path.getsize(mp3_path):,} bytes)")
    return mp3_path


# ============================================================
# 2. HeyGen Upload Asset API로 오디오 업로드
# ============================================================
def upload_audio_to_heygen(mp3_path: str) -> str:
    """MP3 파일을 HeyGen에 업로드하고 audio_asset_id 반환."""
    print(f"\n☁️  HeyGen에 오디오 업로드 중... ({os.path.basename(mp3_path)})")

    with open(mp3_path, "rb") as f:
        data = f.read()

    headers = {
        "X-Api-Key": API_KEY,
        "Content-Type": "audio/mpeg",
    }
    resp = requests.post(UPLOAD_URL, headers=headers, data=data)

    resp.raise_for_status()
    result = resp.json()

    asset_id = result.get("data", {}).get("asset_id") or result.get("data", {}).get("id")
    if not asset_id:
        raise RuntimeError(
            f"오디오 업로드 실패 — asset_id를 받지 못했습니다:\n"
            f"{json.dumps(result, indent=2, ensure_ascii=False)}"
        )

    print(f"   ✅ 업로드 완료! asset_id: {asset_id}")
    return asset_id


# ============================================================
# 3. HeyGen 영상 생성 요청 (오디오 기반)
# ============================================================
def generate_video(audio_asset_id: str) -> str | None:
    """talking_photo + 업로드된 오디오로 영상 생성 (숏폼 9:16)"""
    url = f"{BASE_URL}/v2/video/generate"

    payload = {
        "video_inputs": [
            {
                "character": {
                    "type": "talking_photo",
                    "talking_photo_id": RAPI_TALKING_PHOTO_ID,
                    "use_avatar_iv_model": True,  # Avatar IV 엔진 활성화 (왜곡 감소, 품질 향상)
                },
                "voice": {
                    "type": "audio",
                    "audio_asset_id": audio_asset_id,
                },
            }
        ],
        "dimension": {"width": 1080, "height": 1920},
        "caption": False,
    }

    print("\n🎬 HeyGen 영상 생성 요청 중 (Audio 기반)...")
    resp = requests.post(url, headers=HEADERS, json=payload)
    resp.raise_for_status()
    result = resp.json()

    video_id = result.get("data", {}).get("video_id")
    if not video_id:
        print(f"❌ 생성 요청 실패:\n{json.dumps(result, indent=2, ensure_ascii=False)}")
        return None

    print(f"   ✅ video_id: {video_id}")
    return video_id


# ============================================================
# 4. 렌더링 대기
# ============================================================
def wait_for_video(video_id: str, poll_interval: int = 10, max_wait: int = 900) -> str | None:
    url = f"{BASE_URL}/v1/video_status.get"
    params = {"video_id": video_id}

    print(f"\n⏳ 렌더링 대기 중... (최대 {max_wait // 60}분)")
    start = time.time()

    while True:
        elapsed = time.time() - start
        if elapsed > max_wait:
            print("❌ 타임아웃!")
            return None

        resp = requests.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        status = data.get("status", "unknown")
        video_url = data.get("video_url")

        m, s = divmod(int(elapsed), 60)
        print(f"   [{m:02d}:{s:02d}] {status}")

        if status == "completed" and video_url:
            print("✅ 렌더링 완료!")
            return video_url
        elif status == "failed":
            print(f"❌ 실패: {data.get('error', '?')}")
            return None

        time.sleep(poll_interval)


# ============================================================
# 5. 영상 다운로드
# ============================================================
def download_video(video_url: str, filename: str) -> str:
    filepath = os.path.join(OUTPUT_DIR, filename)
    print(f"\n💾 다운로드 중... → {filepath}")

    resp = requests.get(video_url, stream=True)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    dl = 0
    with open(filepath, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)
            dl += len(chunk)
            if total:
                print(f"\r   {dl / total * 100:.1f}%", end="")
    print(f"\n   ✅ 저장 완료! ({os.path.getsize(filepath):,} bytes)")
    return filepath


# ============================================================
# 6. 콘텐츠 DB 자동 등록
# ============================================================
def generate_metadata(script_text: str) -> dict:
    """LLM으로 스크립트에서 title, summary, tags 자동 생성."""
    from openai import OpenAI
    from pydantic import BaseModel, Field

    class ContentMetadata(BaseModel):
        title: str = Field(description="Short Korean title for the video (under 50 chars)")
        summary: str = Field(description="1-2 sentence Korean summary of what the video covers")
        tags: list[str] = Field(description="3-5 English tech keyword tags")

    client = OpenAI()
    response = client.beta.chat.completions.parse(
        model="gpt-5.4-mini",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "You generate metadata for a Korean tech news short-form video. "
                    "Given the script, produce: a short Korean title, a 1-2 sentence Korean summary, "
                    "and 3-5 English tech keyword tags. Keep the title concise and catchy."
                ),
            },
            {"role": "user", "content": script_text},
        ],
        response_format=ContentMetadata,
    )
    meta = response.choices[0].message.parsed
    return {"title": meta.title, "summary": meta.summary, "tags": meta.tags}


def register_content(script_text: str, video_path: str | None = None, content_url: str | None = None):
    """
    스크립트를 generated_contents 테이블에 등록하고 임베딩을 생성한다.

    Args:
        script_text: 영상 대본 전문
        video_path: 로컬 영상 파일 경로 (선택)
        content_url: S3 등 외부 URL (선택)
    """
    import psycopg2
    from openai import OpenAI

    print("\n-- DB 등록 시작 --")

    # 1. LLM으로 메타데이터 생성
    print("   메타데이터 생성 중...")
    meta = generate_metadata(script_text)
    print(f"   title: {meta['title']}")
    print(f"   summary: {meta['summary']}")
    print(f"   tags: {meta['tags']}")

    # 2. 임베딩 생성
    print("   임베딩 생성 중...")
    client = OpenAI()
    embed_input = f"{meta['summary']}\n{script_text}"
    embed_resp = client.embeddings.create(model="text-embedding-3-small", input=embed_input)
    vector = embed_resp.data[0].embedding
    vector_str = "[" + ",".join(str(v) for v in vector) + "]"

    # 3. DB 삽입
    url = content_url or video_path or None
    conn = psycopg2.connect(
        host=os.environ.get("DB_HOST"),
        port=os.environ.get("DB_PORT", "5432"),
        database=os.environ.get("DB_NAME"),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASSWORD"),
        sslmode="require",
    )
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO generated_contents
                (title, platform, script_text, summary, tags, content_url, is_published, content_vector, uploaded_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s::vector, CURRENT_TIMESTAMP)
            RETURNING content_id
            """,
            [
                meta["title"],
                "youtube_shorts",
                script_text,
                meta["summary"],
                meta["tags"],
                url,
                True,
                vector_str,
            ],
        )
        content_id = cur.fetchone()[0]
        conn.commit()
        print(f"   DB 등록 완료! content_id: {content_id}")
    finally:
        conn.close()

    return content_id


# ============================================================
# 파이프라인 실행
# ============================================================
def run_pipeline(wav_path: str | None = None, script_text: str | None = None, content_url: str | None = None):
    """
    WAV 오디오 기반 HeyGen 영상 생성 파이프라인.

    Args:
        wav_path: WAV 파일 경로. None이면 input_audio 폴더에서 최신 파일 사용.
        script_text: 영상 대본. 제공 시 DB에 자동 등록 + 임베딩 생성.
        content_url: S3 등 외부 URL (선택).
    """
    # WAV 파일 결정
    if wav_path is None:
        wav_files = glob.glob(os.path.join(INPUT_AUDIO_DIR, "*.wav"))
        if not wav_files:
            print(f"❌ '{INPUT_AUDIO_DIR}' 폴더에 WAV 파일이 없습니다.")
            print("   사용법: python generate_video.py [wav파일경로]")
            return None
        wav_path = sorted(wav_files)[-1]

    if not os.path.exists(wav_path):
        print(f"❌ WAV 파일을 찾을 수 없습니다: {wav_path}")
        return None

    print("=" * 60)
    print("🚀 HeyGen 숏폼(9:16) WAV 오디오 기반 영상 생성")
    print("=" * 60)
    print(f"📂 입력 오디오: {wav_path}")

    # ── Step 1: WAV → MP3 변환
    mp3_path = convert_wav_to_mp3(wav_path)

    # ── Step 2: HeyGen에 오디오 업로드
    audio_asset_id = upload_audio_to_heygen(mp3_path)

    # ── Step 3: HeyGen 영상 생성
    video_id = generate_video(audio_asset_id)
    if not video_id:
        return None

    # ── Step 4: 렌더링 대기
    video_url = wait_for_video(video_id)
    if not video_url:
        return None

    # ── Step 5: 영상 다운로드
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_path = download_video(video_url, f"final_{ts}.mp4")

    # ── Step 6: 스크립트가 있으면 DB 자동 등록
    if script_text:
        try:
            register_content(script_text, video_path=final_path, content_url=content_url)
        except Exception as e:
            print(f"\n   DB 등록 실패 (영상은 정상 생성됨): {e}")

    print("\n" + "=" * 60)
    print("전체 완료!")
    print(f"   최종 영상 : {final_path}")
    print("=" * 60)
    return final_path


# ============================================================
# 실행
# ============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HeyGen video generation pipeline")
    parser.add_argument("wav", nargs="?", default=None, help="WAV file path")
    parser.add_argument("--script", type=str, default=None, help="Script text (inline)")
    parser.add_argument("--script-file", type=str, default=None, help="Path to script .txt file")
    parser.add_argument("--content-url", type=str, default=None, help="S3 or external URL for the content")
    args = parser.parse_args()

    script = args.script
    if args.script_file:
        with open(args.script_file, "r", encoding="utf-8") as f:
            script = f.read()

    run_pipeline(wav_path=args.wav, script_text=script, content_url=args.content_url)
