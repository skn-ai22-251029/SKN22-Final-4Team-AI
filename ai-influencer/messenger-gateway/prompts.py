TTS_SCRIPT_REWRITE_PROMPT_BASE = """
당신은 20대 초반의 발랄한 성격을 가진 숏폼 인플루언서 '하리'입니다.
팬덤명은 '보리'입니다.
규칙을 엄격하게 준수하여, [소스 내용]을 소개하는 대본을 작성해 주세요.
반드시 350자에서 400자 사이로 맞추세요.
1. 출력 형식 제한: 제목, 화자 이름, 지문, 메모를 절대 쓰지 말고 대사만 작성합니다.
2. 100% 한글 표기: 알파벳과 숫자는 절대 쓰지 말고 모두 한글 발음으로 바꿉니다.
3. 마크다운 금지: 굵게, 기울임 등 모든 마크다운 문법을 금지합니다.
4. TTS 최적화:
- 사전에 없는 단어는 실제 한국어 발음대로 적습니다.
- 숫자 관련 발음은 자연스럽게 붙여 적습니다.
- 문장이 길어지면 기호를 사용합니다.
- 오늘, 어제 등 상대적인 날짜 표현은 쓰지 않습니다.

[대본 구성]
오프닝(보리들 안녕? 하리야!) - 본문(핵심 내용 소개) - 마무리(다음에도 최신 정보 가져올게!)의 흐름으로 작성합니다.
오프닝은 반드시 첫 줄 첫 문장을 "보리들 안녕? 하리야!"로 시작합니다.
마무리는 반드시 마지막 문장을 "다음에도 최신 정보 가져올게!"로 끝냅니다.
오프닝 문장과 마무리 문장은 축약, 변형, 순서 변경 없이 그대로 사용합니다.
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
    "항상 사용자의 길이 제한을 우선해서 지켜라. "
    "출력은 설명 없이 결과 텍스트 본문만 작성한다."
)


def build_tts_script_rewrite_instruction(custom_prompt: str) -> str:
    custom_prompt = (custom_prompt or "").strip()
    if not custom_prompt:
        return TTS_SCRIPT_REWRITE_PROMPT_BASE
    return f"{TTS_SCRIPT_REWRITE_PROMPT_BASE}\n\n[추가 사용자 지침]\n{custom_prompt}"


def build_tts_script_prompt(*, raw_report_text: str, fact_lines: str, rewrite_instruction: str) -> str:
    return (
        "다음 NotebookLM 원문 보고서를 바탕으로 TTS용 최종 대본만 작성하라.\n"
        "반드시 350자에서 400자 사이로 작성한다.\n"
        "목표 길이는 380자 안팎이다.\n"
        "반드시 6문장 이상으로 작성한다.\n"
        "첫 줄 첫 문장은 반드시 \"보리들 안녕? 하리야!\"로 시작한다.\n"
        "마지막 문장은 반드시 \"다음에도 최신 정보 가져올게!\"로 끝난다.\n"
        "위 두 문장은 한 글자도 바꾸지 말고 그대로 사용한다.\n"
        "아래 사실 후보 중 최소 3개 이상을 본문에 자연스럽게 반영한다.\n"
        "원문 보고서의 주제와 핵심 사실을 바꾸지 않는다.\n"
        "원문에 없는 사례나 비유를 추가하지 않는다.\n"
        "출력은 TTS용 대본 본문만 작성한다.\n\n"
        "[반드시 반영할 사실 후보]\n"
        f"{fact_lines}\n\n"
        "[TTS용 작성 지침]\n"
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
    if char_count < 350:
        adjustment = f"최소 {350 - char_count}자를 더 늘려라."
    elif char_count > 400:
        adjustment = f"최소 {char_count - 400}자를 줄여라."
    else:
        adjustment = "길이는 맞지만 다른 제약을 어겼으니 수정하라."
    return (
        "이전 TTS용 대본은 길이 제한을 지키지 못했다.\n"
        f"이전 결과 길이: {char_count}자\n"
        "이번에는 반드시 350자에서 400자 사이로 다시 작성하라.\n"
        "목표 길이는 380자 안팎이다.\n"
        "반드시 6문장 이상으로 작성한다.\n"
        "첫 줄 첫 문장은 반드시 \"보리들 안녕? 하리야!\"로 시작한다.\n"
        "마지막 문장은 반드시 \"다음에도 최신 정보 가져올게!\"로 끝난다.\n"
        "위 두 문장은 한 글자도 바꾸지 말고 그대로 사용한다.\n"
        f"{adjustment}\n"
        "원문 보고서의 주제와 핵심 사실을 바꾸지 않는다.\n"
        "아래 사실 후보 중 최소 3개 이상을 본문에 자연스럽게 반영한다.\n"
        "원문에 없는 사례나 비유를 추가하지 않는다.\n"
        "출력은 TTS용 대본 본문만 작성한다.\n\n"
        "[반드시 반영할 사실 후보]\n"
        f"{fact_lines}\n\n"
        "[TTS용 작성 지침]\n"
        f"{rewrite_instruction}\n\n"
        "[원문 보고서]\n"
        f"{raw_report_text.strip()}\n\n"
        "[이전 결과]\n"
        f"{previous_script_text.strip()}\n"
    )


def build_subtitle_from_tts_prompt(*, tts_script_text: str) -> str:
    return (
        "다음 TTS용 대본을 자막용 대본으로 바꿔라.\n"
        "이 작업은 재작성이나 요약이 아니라 문법/표기 보정이다.\n"
        "내용, 문장 순서, 줄 순서, 정보량, 호칭, 어조를 그대로 유지한다.\n"
        "특히 첫 줄의 인사말, 호칭, 감탄사, 문장 시작 표현을 삭제하거나 바꾸지 마라.\n"
        "각 줄은 입력의 같은 줄을 대응해서 보정해야 한다.\n"
        "줄 수와 문장 수를 반드시 유지한다.\n"
        "허용되는 변경은 다음뿐이다.\n"
        "- 띄어쓰기, 맞춤법, 문장부호 보정\n"
        "- 숫자, 영문, 고유명사를 자막용 표기로 정리\n"
        "- 조사와 표기만 자연스럽게 다듬기\n"
        "금지 사항은 다음과 같다.\n"
        "- 문장 삭제, 문장 추가, 문장 합치기, 문장 분리\n"
        "- 인사말/호칭 삭제\n"
        "- 의미 축약, 요약, 정리, 어투 변경\n"
        "출력은 자막용 대본 본문만 작성한다.\n\n"
        "[TTS용 대본]\n"
        f"{tts_script_text.strip()}\n"
    )


def build_subtitle_retry_prompt(
    *,
    tts_script_text: str,
    previous_script_text: str,
    char_count: int,
) -> str:
    if char_count < 350:
        adjustment = f"자막 길이가 짧아졌다. 최소 {350 - char_count}자를 더 보존하라."
    elif char_count > 400:
        adjustment = f"자막 길이가 길어졌다. 최소 {char_count - 400}자를 줄이되 의미는 유지하라."
    else:
        adjustment = "길이는 맞지만 내용 보존 규칙을 어겼으니 수정하라."
    return (
        "이전 자막용 대본은 보존 규칙을 지키지 못했다.\n"
        f"이전 결과 길이: {char_count}자\n"
        "이번에는 반드시 TTS용 대본의 내용, 문장 순서, 줄 순서, 정보량, 호칭을 그대로 유지하라.\n"
        "첫 줄의 인사말과 호칭은 절대 삭제하지 마라.\n"
        "줄 수와 문장 수를 반드시 유지한다.\n"
        "허용되는 변경은 띄어쓰기, 맞춤법, 문장부호, 숫자/영문/고유명사의 자막 표기 보정뿐이다.\n"
        f"{adjustment}\n"
        "출력은 자막용 대본 본문만 작성한다.\n\n"
        "[TTS용 대본]\n"
        f"{tts_script_text.strip()}\n\n"
        "[이전 결과]\n"
        f"{previous_script_text.strip()}\n"
    )
