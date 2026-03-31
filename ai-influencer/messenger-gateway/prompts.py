TTS_SCRIPT_REWRITE_PROMPT_BASE = """
당신은 20대 초반의 발랄한 성격을 가진 숏폼 인플루언서 '하리'입니다.
팬덤명은 '보리'입니다.
규칙을 엄격하게 준수하여, [소스 내용]을 소개하는 350자 분량의 대본을 작성해 주세요.
반드시 350자에서 370자 사이로 맞추세요.
1. 출력 형식 제한: 제목, 화자 이름, 지문, 메모를 절대 쓰지 말고 대사만 작성합니다.
2. 100% 한글 표기: 알파벳과 숫자는 절대 쓰지 말고 모두 한글 발음으로 바꿉니다.
3. 마크다운 금지: 굵게, 기울임 등 모든 마크다운 문법을 금지합니다.
4. TTS 최적화:
- 사전에 없는 단어는 실제 한국어 발음대로 적습니다.
- 숫자 관련 발음은 자연스럽게 붙여 적습니다.
- 발랄한 억양을 살리기 위해 물음표를 적극적으로 사용합니다.
- 오늘, 어제 등 상대적인 날짜 표현은 쓰지 않습니다.
- 매 문장 끝에는 줄바꿈을 넣습니다.

[대본 구성]
오프닝(인사) - 본문(핵심 내용 소개) - 마무리(엔딩)의 흐름으로 작성합니다.
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
    "원문에 나온 핵심 대상, 기술, 제품, 사건, 개념을 그대로 유지해야 한다. "
    "예를 들어 원문이 레이 트레이싱이면 결과도 레이 트레이싱이어야 하며, 자율주행 같은 다른 주제로 바꾸면 안 된다. "
    "반드시 JSON 객체 하나만 출력한다."
)


def build_tts_script_rewrite_instruction(custom_prompt: str) -> str:
    custom_prompt = (custom_prompt or "").strip()
    if not custom_prompt:
        return TTS_SCRIPT_REWRITE_PROMPT_BASE
    return f"{TTS_SCRIPT_REWRITE_PROMPT_BASE}\n\n[추가 사용자 지침]\n{custom_prompt}"


def build_script_rewrite_user_prompt(*, raw_report_text: str, rewrite_instruction: str) -> str:
    return (
        "다음 NotebookLM 원문 보고서를 바탕으로 최종 대본 2종을 작성하라.\n"
        "출력은 반드시 JSON 객체 하나만 사용하고, 키는 subtitle_script_text 와 tts_script_text 두 개만 사용한다.\n"
        "반드시 다음 순서를 지킨다.\n"
        "1. 먼저 tts_script_text를 작성한다.\n"
        "2. 그 다음 subtitle_script_text는 tts_script_text를 그대로 바탕으로 만든다.\n"
        "3. subtitle_script_text는 tts_script_text와 내용, 문장 순서, 줄 순서, 정보량이 완전히 같아야 한다.\n"
        "4. subtitle_script_text에서 허용되는 수정은 문법, 띄어쓰기, 맞춤법, 문장부호, 표준 표기만이다.\n"
        "5. subtitle_script_text를 만들 때 문장을 추가, 삭제, 요약, 확장, 재배열, 재해석하면 안 된다.\n"
        "6. 원문 보고서의 주제를 다른 주제로 바꾸면 안 된다.\n"
        "7. 원문 보고서의 핵심 대상과 용어를 유지해야 한다.\n\n"
        "[tts_script_text 작성 지침]\n"
        f"{rewrite_instruction}\n\n"
        "[subtitle_script_text 작성 지침]\n"
        "- tts_script_text를 그대로 복제한 뒤, 자막용 표기만 보정한다.\n"
        "- 내용은 tts_script_text와 완전히 동일해야 한다.\n"
        "- 허용되는 수정은 문법, 띄어쓰기, 맞춤법, 문장부호, 표준 표기만이다.\n"
        "- 숫자, 버전, 영문 고유명사, 제품명은 일반적인 자막 표기로 되돌린다.\n"
        "- 예: 오쩜사 -> 5.4, 지피티 오쩜사 -> GPT-5.4, 에이아이 -> AI.\n"
        "- 발음 최적화를 위한 변형 표기, 과한 구어체, 중복 표현은 자막 문장에 맞게 바로잡는다.\n"
        "- 제목, 화자 이름, 마크다운, 메모는 넣지 않는다.\n\n"
        "[원문 보고서]\n"
        f"{raw_report_text.strip()}\n"
    )
