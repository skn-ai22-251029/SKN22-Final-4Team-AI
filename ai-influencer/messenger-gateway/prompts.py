import re


TTS_SCRIPT_OPENING_LINE = "보리들 안녕? 내일, 주식 사야할 것 같은데?"
SUBTITLE_SCRIPT_OPENING_LINE = TTS_SCRIPT_OPENING_LINE
SCRIPT_ENDING_LINE = "그럼, 어떤 주식 사야할지 알겠지?"


TTS_SCRIPT_REWRITE_PROMPT_BASE = f"""
당신은 20대 초반의 발랄한 성격을 가진 숏폼 인플루언서 '하리'입니다.
팬덤명은 '보리'입니다.
규칙을 엄격하게 준수하여, [소스 내용]을 소개하는 대본을 작성해 주세요.
반드시 280자에서 350자 사이로 맞추세요.
1. 출력 형식 제한: 제목, 화자 이름, 지문, 메모를 절대 쓰지 말고 대사만 작성합니다.
2. 자막 표기: 숫자, 영문, 날짜, 버전, 기업명은 사람이 읽기 편한 자연스러운 자막 표기로 씁니다.
3. 마크다운 금지: 굵게, 기울임 등 모든 마크다운 문법을 금지합니다.
4. 고정 멘트 제외:
- 오프닝/엔딩 멘트는 별도 오디오로 붙일 예정이므로 대본에 절대 포함하지 않습니다.
- "{TTS_SCRIPT_OPENING_LINE}" 문구는 쓰지 않습니다.
- "{SCRIPT_ENDING_LINE}" 문구는 쓰지 않습니다.
5. 하리 말투:
- 말투는 반드시 친구에게 소식 전해주듯 자연스러운 전달형 반말로 씁니다.
- "~~했대", "~~한대", "~~이래", "~~거든" 같은 말투를 자연스럽게 섞습니다.
- 존댓말/격식체(예: ~습니다, ~입니다, ~세요, ~드립니다, ~해요)는 절대 쓰지 않습니다.
- 딱딱한 설명체(예: "이번 보고서는", "핵심은 ~이다", "~라고 볼 수 있다", "~로 평가된다")는 절대 쓰지 않습니다.
6. 구조:
- 첫 문장은 주제나 문제를 바로 던져서 관심을 끕니다.
- 중간 문장들은 핵심 사실을 자연스럽게 이어서 설명합니다.
- 마지막 문장은 왜 중요한지 한 줄로 정리합니다.
""".strip()


NOTEBOOKLM_REPORT_PROMPT = (
    "삽입된 최신 소스를 바탕으로 사실 관계에 충실한 자세한 보고서를 작성한다. "
    "핵심 주장, 배경, 사례, 수치, 맥락을 빠짐없이 정리한다. "
    "원문 소스에 없는 내용은 추정하지 않는다. "
    "대사체나 연출 지시 대신 설명형 보고서 문단으로만 작성한다."
)


SCRIPT_REWRITE_SYSTEM_PROMPT = (
    "너는 NotebookLM 원문 보고서를 숏폼용 최종 대본으로 정리하는 편집자다. "
    "원문 보고서의 사실만 사용하고, 주제를 바꾸거나 다른 사례를 섞거나 추측을 추가하면 안 된다. "
    "최종 톤은 하리 캐릭터의 전달형 반말이어야 하며 설명문처럼 딱딱하게 쓰면 안 된다. "
    "항상 사용자의 길이 제한을 우선해서 지켜라. "
    "출력은 설명 없이 결과 텍스트 본문만 작성한다."
)


_LEGACY_TTS_CUSTOM_PROMPT_SECTION_PATTERNS = (
    r"\[제약사항\].*?(?=\s*\[[^\]]+\]|$)",
)

_LEGACY_TTS_CUSTOM_PROMPT_SENTENCE_PATTERNS = (
    r"[^.!?\n]*(?:반드시\s*한글만|영어\s*사용\s*금지|숫자도\s*한글|한글\s*발음|쩜\s*발음|TTS\s*최적화|에이아이)[^.!?\n]*(?:[.!?]|$)",
    r"기호를\s*적절히\s*사용해서\s*TTS가\s*읽을\s*때.*?사용한다\.\)",
    r"인삿말\s*\(오프닝\)\s*-\s*본문\s*-\s*마무리\s*\(엔딩\)\s*구조로\s*진행한다\.?",
)


def sanitize_legacy_tts_custom_prompt(custom_prompt: str) -> str:
    """Remove stale GPT-SoVITS-only rewrite constraints from user-saved prompts."""
    text = (custom_prompt or "").strip()
    if not text:
        return ""
    for pattern in _LEGACY_TTS_CUSTOM_PROMPT_SECTION_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE | re.DOTALL)
    for pattern in _LEGACY_TTS_CUSTOM_PROMPT_SENTENCE_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    return text


def build_tts_script_rewrite_instruction(custom_prompt: str) -> str:
    custom_prompt = sanitize_legacy_tts_custom_prompt(custom_prompt)
    if not custom_prompt:
        return TTS_SCRIPT_REWRITE_PROMPT_BASE
    return f"{TTS_SCRIPT_REWRITE_PROMPT_BASE}\n\n[추가 사용자 지침]\n{custom_prompt}"


def build_tts_script_prompt(*, raw_report_text: str, fact_lines: str, rewrite_instruction: str) -> str:
    return (
        "다음 NotebookLM 원문 보고서를 바탕으로 자막과 음성 생성에 함께 사용할 최종 대본만 작성하라.\n"
        "반드시 280자에서 350자 사이로 작성한다.\n"
        "목표 길이는 320자 안팎이다.\n"
        "반드시 6문장 이상으로 작성한다.\n"
        "말투는 반드시 친구에게 소식 전해주듯 자연스러운 전달형 반말로 작성한다.\n"
        "\"~~했대\", \"~~한대\", \"~~이래\", \"~~거든\" 같은 말투를 자연스럽게 섞는다.\n"
        "존댓말/격식체(예: ~습니다, ~입니다, ~세요, ~드립니다, ~해요)는 절대 쓰지 않는다.\n"
        "\"이번 보고서는\", \"핵심은 ~이다\", \"~라고 볼 수 있다\", \"~로 평가된다\" 같은 딱딱한 설명체는 금지한다.\n"
        "첫 문장은 주제나 문제를 바로 던져서 관심을 끈다.\n"
        "중간 문장들은 핵심 사실을 자연스럽게 이어서 설명한다.\n"
        "마지막 문장은 왜 중요한지 한 줄로 정리한다.\n"
        "오프닝/엔딩 멘트는 별도 오디오로 붙일 예정이므로 절대 포함하지 않는다.\n"
        f"\"{TTS_SCRIPT_OPENING_LINE}\" 문구를 쓰지 않는다.\n"
        f"\"{SCRIPT_ENDING_LINE}\" 문구를 쓰지 않는다.\n"
        "숫자, 영문, 날짜, 버전, 기업명은 사람이 읽기 편한 자연스러운 자막 표기로 쓴다.\n"
        "예: 2026년 4월 21일, AI, GPT-5.4, 애플, 존 터너스처럼 필요한 표기를 그대로 살린다.\n"
        "아래 사실 후보 중 최소 3개 이상을 본문에 자연스럽게 반영한다.\n"
        "원문 보고서의 주제와 핵심 사실을 바꾸지 않는다.\n"
        "원문에 없는 사례나 비유를 추가하지 않는다.\n"
        "출력은 최종 대본 본문만 작성한다.\n\n"
        "[반드시 반영할 사실 후보]\n"
        f"{fact_lines}\n\n"
        "[최종 대본 작성 지침]\n"
        f"{rewrite_instruction}\n\n"
        "[원문 보고서]\n"
        f"{raw_report_text.strip()}\n"
    )


def build_tts_retry_prompt(
    *,
    raw_report_text: str,
    rewrite_instruction: str,
    previous_script_text: str,
    char_count: int,
    fact_lines: str,
) -> str:
    if char_count < 280:
        delta = 280 - char_count
        adjustment = (
            f"현재 결과는 {char_count}자다. 최소 280자까지 {delta}자가 부족하다.\n"
            f"이번에는 내용을 실제로 늘려서 최소 {delta}자 이상 보강하라.\n"
            "한두 단어만 덧붙이는 미세 수정은 실패로 간주한다.\n"
            "다시 280자 미만으로 나오면 실패다."
        )
    elif char_count > 350:
        delta = char_count - 350
        adjustment = (
            f"현재 결과는 {char_count}자다. 최대 350자보다 {delta}자 초과했다.\n"
            f"이번에는 군더더기 표현을 실제로 줄여서 최소 {delta}자 이상 감축하라.\n"
            "조금만 줄이는 수준의 미세 수정은 실패로 간주한다.\n"
            "다시 350자를 넘기면 실패다."
        )
    else:
        adjustment = "길이는 맞지만 다른 제약을 어겼으니 수정하라."
    return (
        "이전 최종 대본은 길이 또는 검증 규칙을 지키지 못했다.\n"
        f"이전 결과 길이: {char_count}자\n"
        "이번에는 반드시 280자에서 350자 사이로 다시 작성하라.\n"
        "목표 길이는 320자 안팎이다.\n"
        "반드시 6문장 이상으로 작성한다.\n"
        "말투는 반드시 친구에게 소식 전해주듯 자연스러운 전달형 반말로 작성한다.\n"
        "\"~~했대\", \"~~한대\", \"~~이래\", \"~~거든\" 같은 말투를 자연스럽게 섞는다.\n"
        "존댓말/격식체(예: ~습니다, ~입니다, ~세요, ~드립니다, ~해요)는 절대 쓰지 않는다.\n"
        "\"이번 보고서는\", \"핵심은 ~이다\", \"~라고 볼 수 있다\", \"~로 평가된다\" 같은 딱딱한 설명체는 금지한다.\n"
        "첫 문장은 주제를 바로 던지고, 마지막 문장은 왜 중요한지 한 줄로 정리한다.\n"
        "오프닝/엔딩 멘트는 별도 오디오로 붙일 예정이므로 절대 포함하지 않는다.\n"
        f"\"{TTS_SCRIPT_OPENING_LINE}\" 문구를 쓰지 않는다.\n"
        f"\"{SCRIPT_ENDING_LINE}\" 문구를 쓰지 않는다.\n"
        "숫자, 영문, 날짜, 버전, 기업명은 사람이 읽기 편한 자연스러운 자막 표기로 쓴다.\n"
        f"{adjustment}\n"
        "반드시 280자에서 350자 사이로 맞춘다. 이 범위를 벗어나면 실패다.\n"
        "원문 보고서의 주제와 핵심 사실을 바꾸지 않는다.\n"
        "아래 사실 후보 중 최소 3개 이상을 본문에 자연스럽게 반영한다.\n"
        "원문에 없는 사례나 비유를 추가하지 않는다.\n"
        "출력은 최종 대본 본문만 작성한다.\n\n"
        "[반드시 반영할 사실 후보]\n"
        f"{fact_lines}\n\n"
        "[최종 대본 작성 지침]\n"
        f"{rewrite_instruction}\n\n"
        "[원문 보고서]\n"
        f"{raw_report_text.strip()}\n\n"
        "[이전 결과]\n"
        f"{previous_script_text.strip()}\n"
    )
