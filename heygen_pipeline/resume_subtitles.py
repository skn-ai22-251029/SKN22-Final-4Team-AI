import os
import sys
import glob
from generate_video import extract_audio, burn_ass_subtitles, OUTPUT_DIR
from whisper_align import align_audio

def resume_pipeline(raw_video_name, script_path=None):
    """
    이미 생성된 raw_*.mp4 파일을 기반으로 자막 작업만 수행합니다.
    """
    raw_path = os.path.join(OUTPUT_DIR, raw_video_name)
    if not os.path.exists(raw_path):
        print(f"❌ 파일을 찾을 수 없습니다: {raw_path}")
        return

    # 타임스탬프 추출 (예: raw_20260313_081730.mp4 -> 20260313_081730)
    ts = raw_video_name.replace("raw_", "").replace(".mp4", "")
    
    # 1. 대본 가져오기 (지정되지 않았으면 최신 파일 사용)
    if not script_path:
        script_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "input_script")
        txt_files = glob.glob(os.path.join(script_dir, "*.txt"))
        if not txt_files:
            print("❌ 대본 파일을 찾을 수 없습니다.")
            return
        script_path = sorted(txt_files)[-1]
    
    print(f"📄 대상 영상: {raw_video_name}")
    print(f"📄 대상 대본: {os.path.basename(script_path)}")

    # 2. 오디오 추출
    audio_path = os.path.join(OUTPUT_DIR, f"audio_{ts}.wav")
    print(f"\n🎵 오디오 추출 중...")
    extract_audio(raw_path, audio_path)

    # 3. WhisperX 정렬 및 ASS 생성 (새로운 DTW 로직 사용)
    ass_path = os.path.join(OUTPUT_DIR, f"subtitles_{ts}.ass")
    print(f"\n🧠 자막 정렬 및 ASS 생성 중 (DTW 로직)...")
    try:
        align_audio(audio_path, script_path, ass_path)
    except Exception as e:
        print(f"❌ 자막 생성 실패: {e}")
        return

    # 4. 자막 번인
    final_filepath = os.path.join(OUTPUT_DIR, f"final_{ts}.mp4")
    burn_ass_subtitles(raw_path, ass_path, final_filepath)

    print("\n" + "=" * 60)
    print("🎉 자막 작업 완료!")
    print(f"   📂 최종 영상 : {final_filepath}")
    print("=" * 60)

if __name__ == "__main__":
    # 사용자 파일명에 맞춰 실행
    target_raw = "raw_20260313_081730.mp4"
    resume_pipeline(target_raw)
