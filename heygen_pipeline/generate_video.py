"""
HeyGen 아바타 영상 자동 생성 파이프라인 (자막 하드번인 버전)

[흐름]
  1. HeyGen API → 멀티씬 영상 생성 (caption 비활성, TTS 대본만 사용)
  2. 영상 다운로드 (raw)
  3. ffprobe → 전체 재생시간 측정
  4. 씬별 TTS 글자수 비율 → 각 씬의 자막 타이밍 추정
  5. SRT 자막 파일 생성 (caption 텍스트 사용)
  6. ffmpeg → 자막 하드번인 → 최종 영상 저장

[필요 사전조건]
  - ffmpeg, ffprobe 가 PATH에 있어야 함
    Windows: https://ffmpeg.org/download.html 에서 설치 후 PATH 등록
"""

import requests
import time
import os
import json
import sys
import subprocess
import textwrap
from datetime import datetime


# ============================================================
# 설정
# ============================================================
API_KEY = "sk_V2_hgu_kXf2VFgX4hI_M4YZKY7V8EZ9SeIng3FJP58yVOkpC04K"
BASE_URL = "https://api.heygen.com"
HEADERS = {
    "X-Api-Key": API_KEY,
    "Content-Type": "application/json",
    "Accept": "application/json",
}

RAPI_TALKING_PHOTO_ID = "d47793678f5340f7ab928723710d20fb"
PEPPY_PRIYA_VOICE_ID  = "4bc7940bbb4c4227adb46bb28a019bff"

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output_videos")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 자막 스타일 (ffmpeg ASS 스타일, Windows 기본 한국어 폰트)
# 숏폼(9:16) 화면에 맞춰 폰트 크기와 여백 조정
SUBTITLE_STYLE = (
    "FontName=Malgun Gothic,"
    "FontSize=13,"               # 숏폼 가독성을 위해 크기 조절
    "PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,"
    "BackColour=&H80000000,"
    "Outline=2,"
    "Shadow=0,"
    "MarginV=30,"               # 숏폼 하단 UI(아이콘 등)를 피해 위치 상향
    "Alignment=2"
)


# ============================================================
# 씬 정의
# SCENES는 이제 input_script.txt에서 자동으로 생성됩니다.
# ============================================================
SCENES = []


# ============================================================
# 1. HeyGen 영상 생성 요청
# ============================================================
def generate_video(full_text: str, speed: float = 1.3) -> str | None:
    """단일 씬 영상 생성 (숏폼 9:16)"""
    url = f"{BASE_URL}/v2/video/generate"

    payload = {
        "video_inputs": [
            {
                "character": {
                    "type": "talking_photo",
                    "talking_photo_id": RAPI_TALKING_PHOTO_ID,
                },
                "voice": {
                    "type": "text",
                    "input_text": full_text,
                    "voice_id": PEPPY_PRIYA_VOICE_ID,
                    "speed": speed,
                },
            }
        ],
        "dimension": {"width": 1080, "height": 1920},
        "caption": False,
    }

    print("\n🎬 HeyGen 영상 생성 요청 중 (Single Cut)...")
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
# 2. 렌더링 대기
# ============================================================
def wait_for_video(video_id: str, poll_interval: int = 10, max_wait: int = 900) -> str | None:
    url = f"{BASE_URL}/v1/video_status.get"
    params = {"video_id": video_id}

    print(f"\n⏳ 렌더링 대기 중... (최대 {max_wait//60}분)")
    start = time.time()

    while True:
        elapsed = time.time() - start
        if elapsed > max_wait:
            print("❌ 타임아웃!")
            return None

        resp = requests.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        status   = data.get("status", "unknown")
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
# 3. 영상 다운로드 (raw — 자막 미포함)
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
# 4. ffprobe — 영상 총 재생시간(초) 추출
# ============================================================
def get_video_duration(filepath: str) -> float:
    """ffprobe로 영상 길이(초) 반환"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        filepath,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 오류: {result.stderr.strip()}")
    return float(result.stdout.strip())


# ============================================================
# 5. 씬별 자막 타이밍 추정 (TTS 글자수 비율 기반)
# ============================================================
def estimate_scene_timings(scenes: list[dict], total_duration: float) -> list[tuple[float, float]]:
    """
    각 씬의 TTS 글자수 비율로 타이밍 추정
    반환: [(start_sec, end_sec), ...]
    """
    lengths = [len(s["tts"]) for s in scenes]
    total_len = sum(lengths)
    timings = []
    cursor = 0.0
    for length in lengths:
        duration = (length / total_len) * total_duration
        timings.append((cursor, cursor + duration))
        cursor += duration
    return timings


# ============================================================
# 6. SRT 자막 파일 생성
# ============================================================
def seconds_to_srt_time(sec: float) -> str:
    """초(float) → SRT 타임스탬프 문자열 (HH:MM:SS,mmm)"""
    ms = int((sec % 1) * 1000)
    total_s = int(sec)
    h = total_s // 3600
    m = (total_s % 3600) // 60
    s = total_s % 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_srt(scenes: list[dict], timings: list[tuple[float, float]]) -> str:
    """SRT 자막 파일 내용 생성"""
    lines = []
    for idx, (scene, (start, end)) in enumerate(zip(scenes, timings), 1):
        lines.append(str(idx))
        lines.append(f"{seconds_to_srt_time(start)} --> {seconds_to_srt_time(end)}")
        lines.append(scene["caption"])
        lines.append("")
    return "\n".join(lines)


# ============================================================
# 8. 오디오 추출
# ============================================================
def extract_audio(video_path: str, audio_path: str):
    """ffmpeg으로 영상에서 오디오만 추출 (WAV)"""
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        audio_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return audio_path

# ============================================================
# 9. ffmpeg — ASS 자막 하드번인
# ============================================================
def burn_ass_subtitles(video_path: str, ass_path: str, out_path: str):
    """ffmpeg으로 ASS 자막을 영상에 하드번인"""
    ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:")
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"ass='{ass_escaped}'",
        "-c:a", "copy",
        out_path,
    ]
    print(f"\n🖊️  ASS 자막 하드번인 중...")
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path


def run_pipeline(speed: float = 1.3):
    import glob
    
    # input_script 폴더 내의 최신 txt 파일 검색
    script_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "input_script")
    txt_files = glob.glob(os.path.join(script_dir, "*.txt"))
    
    if not txt_files:
        print(f"❌ '{script_dir}' 폴더에 txt 파일이 없습니다.")
        return None
    
    latest_script = sorted(txt_files)[-1]
    print(f"📄 최신 대본 로딩 중: {os.path.basename(latest_script)}")
    
    with open(latest_script, 'r', encoding='utf-8') as f:
        full_text = f.read().strip()
    
    print("=" * 60)
    print("🚀 HeyGen 숏폼(9:16) WhisperX 정렬 파이프라인")
    print("=" * 60)

    # ── Step 1: HeyGen 영상 생성
    video_id = generate_video(full_text, speed)
    if not video_id: return None
    
    # ── Step 2: 렌더링 대기
    video_url = wait_for_video(video_id)
    if not video_url: return None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = download_video(video_url, f"raw_{ts}.mp4")

    # ── Step 3: 오디오 추출
    audio_path = os.path.join(OUTPUT_DIR, f"audio_{ts}.wav")
    print(f"\n🎵 오디오 추출 중...")
    extract_audio(raw_path, audio_path)

    # ── Step 4: WhisperX 정렬 및 ASS 생성
    from whisper_align import align_audio
    ass_path = os.path.join(OUTPUT_DIR, f"subtitles_{ts}.ass")
    print(f"\n🧠 WhisperX Forced Alignment 진행 중...")
    try:
        align_audio(audio_path, latest_script, ass_path)
    except Exception as e:
        print(f"❌ Alignment 실패: {e}")
        return raw_path

    # ── Step 5: 자막 번인
    final_filepath = os.path.join(OUTPUT_DIR, f"final_{ts}.mp4")
    burn_ass_subtitles(raw_path, ass_path, final_filepath)

    print("\n" + "=" * 60)
    print("🎉 전체 완료!")
    print(f"   📂 최종 영상 : {final_filepath}")
    print("=" * 60)
    return final_filepath


# ============================================================
# 실행
# ============================================================
if __name__ == "__main__":
    run_pipeline(speed=1.3)
