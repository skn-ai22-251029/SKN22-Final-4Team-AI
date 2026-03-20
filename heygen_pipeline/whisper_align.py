"""
whisper_align.py — Segment Anchor 기반 자막 정렬

전략:
  1. openai-whisper 로 전사 → 세그먼트별 실제 발화 구간(start/end) 확보
  2. 원본 .txt 대본을 15자 청크로 분할
  3. 세그먼트 텍스트 글자수와 청크 글자수를 매칭하여,
     각 청크가 어느 세그먼트 구간의 몇 % 지점인지 계산
  4. 세그먼트 경계를 앵커로 삼아 청크에 타임스탬프 배정 → ASS 생성

장점:
  - whisperx 의존성 없음 (openai-whisper + ffmpeg만 필요)
  - 세그먼트 경계가 실제 오디오 기반 → 구간 간 drift 원천 차단
  - Whisper 전사 텍스트는 자막에 사용하지 않음 → 고유명사/한영 혼용 무관
"""

import os
import sys

# ── ASS 헤더 ──────────────────────────────────────────────────
ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Malgun Gothic,60,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,0,2,20,20,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _fmt(sec: float) -> str:
    """초 → ASS 타임스탬프 H:MM:SS.cc"""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


# ──────────────────────────────────────────────────────────────
# Step 1 : openai-whisper 전사 → 세그먼트 앵커 확보
# ──────────────────────────────────────────────────────────────
def get_segment_anchors(audio_path: str, device: str = "cuda") -> list[dict]:
    """
    openai-whisper로 전사 → 세그먼트별 (start, end, char_count) 반환.
    텍스트 내용은 사용하지 않고, 글자수만 비율 계산에 활용.
    """
    import whisper

    print("   ▸ [Step 1] Whisper 모델 로드 중 (large-v2)...")
    model = whisper.load_model("large-v2", device=device)

    print("   ▸ [Step 1] 전사 중 (언어: ko)...")
    result = model.transcribe(audio_path, language="ko", verbose=False)

    anchors = []
    for seg in result["segments"]:
        text = seg["text"].strip()
        if text:
            anchors.append({
                "start": seg["start"],
                "end":   seg["end"],
                "chars": len(text),  # 글자수만 사용 (텍스트 자체는 무시)
            })

    print(f"   ▸ [Step 1] 완료 → {len(anchors)}개 세그먼트 앵커 확보")
    return anchors


# ──────────────────────────────────────────────────────────────
# Step 2 : 앵커 기반 매핑
# ──────────────────────────────────────────────────────────────
def map_chunks_to_anchors(chunks: list[str],
                          anchors: list[dict]) -> list[tuple]:
    """
    원본 텍스트 청크를 세그먼트 앵커에 비율로 매핑.

    원리:
      1) 세그먼트 앵커의 전체 글자수(Whisper 쪽)와
         원본 청크의 전체 글자수를 각각 누적
      2) 청크의 누적 글자 위치를 0~1 비율로 계산
      3) 그 비율에 해당하는 세그먼트 시간 구간을 보간하여 시작/끝 시간 산출

    반환: [(start_sec, end_sec, chunk_text), ...]
    """
    # 앵커의 전체 타임라인을 하나의 연속 시간축으로 변환
    total_anchor_chars = sum(a["chars"] for a in anchors)
    total_chunk_chars  = sum(len(c) for c in chunks)

    if total_anchor_chars == 0 or total_chunk_chars == 0:
        raise ValueError("텍스트가 비어있습니다.")

    # 앵커별 누적 글자 비율 → 시간 보간 테이블 생성
    # [(ratio, time), ...] — 각 앵커의 시작과 끝을 등록
    interp_table = []
    cum_chars = 0
    for a in anchors:
        r_start = cum_chars / total_anchor_chars
        cum_chars += a["chars"]
        r_end = cum_chars / total_anchor_chars
        interp_table.append((r_start, a["start"]))
        interp_table.append((r_end,   a["end"]))

    def ratio_to_time(ratio: float) -> float:
        """비율(0~1) → 보간된 시간(초)"""
        ratio = max(0.0, min(1.0, ratio))
        # 테이블에서 이 비율이 들어갈 위치 찾기
        for i in range(len(interp_table) - 1):
            r0, t0 = interp_table[i]
            r1, t1 = interp_table[i + 1]
            if r0 <= ratio <= r1:
                if r1 == r0:
                    return t0
                frac = (ratio - r0) / (r1 - r0)
                return t0 + frac * (t1 - t0)
        return interp_table[-1][1]

    # 청크 매핑
    result = []
    cursor = 0
    for i, chunk in enumerate(chunks):
        r_start = cursor / total_chunk_chars
        cursor += len(chunk)
        r_end   = cursor / total_chunk_chars

        t_start = ratio_to_time(r_start)
        t_end   = ratio_to_time(r_end)

        # 최소 표시 시간 0.3초 보장
        if t_end - t_start < 0.3:
            t_end = t_start + 0.3

        result.append((t_start, t_end, chunk))

    return result


# ──────────────────────────────────────────────────────────────
# Step 3 : ASS 파일 생성
# ──────────────────────────────────────────────────────────────
def generate_ass(mapped: list[tuple], output_path: str) -> str:
    events = []
    for start, end, text in mapped:
        events.append(
            f"Dialogue: 0,{_fmt(start)},{_fmt(end)},Default,,0,0,0,,{text}"
        )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(ASS_HEADER)
        f.write("\n".join(events))
        f.write("\n")

    print(f"   ✅ ASS 생성 완료 ({len(events)}개 자막줄) → {output_path}")
    return output_path


# ──────────────────────────────────────────────────────────────
# 메인 진입점
# ──────────────────────────────────────────────────────────────
def align_audio(audio_path: str, script_path: str, output_ass_path: str,
                device: str = "cuda", max_chars: int = 15) -> str:
    """
    전체 파이프라인:
      1. openai-whisper 전사 → 세그먼트 앵커
      2. 원본 대본 15자 청킹
      3. 앵커 기반 보간 매핑
      4. ASS 저장
    """
    # ── 1. 세그먼트 앵커
    anchors = get_segment_anchors(audio_path, device)

    # ── 2. 원본 대본 청킹
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from preprocess_script import split_script_into_chunks

    with open(script_path, "r", encoding="utf-8") as f:
        raw_text = f.read()
    chunks = split_script_into_chunks(raw_text, max_chars=max_chars)
    print(f"   ▸ [Step 2] 원본 스크립트 → {len(chunks)}개 청크 (최대 {max_chars}자)")

    # ── 3. 매핑
    print("   ▸ [Step 3] 세그먼트 앵커 기반 보간 매핑 중...")
    mapped = map_chunks_to_anchors(chunks, anchors)

    # ── 4. ASS 저장
    return generate_ass(mapped, output_ass_path)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Segment Anchor 기반 자막 정렬")
    parser.add_argument("audio", help="WAV 파일 경로")
    parser.add_argument("script", help="원본 TXT 대본 경로")
    parser.add_argument("output", help="출력 ASS 파일 경로")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--max_chars", type=int, default=15)
    args = parser.parse_args()

    align_audio(args.audio, args.script, args.output, args.device, args.max_chars)
