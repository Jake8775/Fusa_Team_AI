"""
글짓기 AI 앱 — HMG 사내 게이트웨이 연동
단어 몇 개를 입력하면 AI가 글을 써드립니다.
"""

import streamlit as st
import time

# ── 상수 ──────────────────────────────────────────────────────────────────────
BASE_URL = "https://internal-apigw-kr.hmg-corp.io/hchat-in/api/v3"
GEMINI_BASE_URL = "https://internal-apigw-kr.hmg-corp.io/hchat-in/api/v3?key="
GPT_API_VERSION = "2025-04-01-preview"

AI_ENGINES = {
    "Gemini": [
        "gemini-3.1-pro-preview",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ],
    "GPT": [
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5",
        "gpt-5-mini",
        "gpt-4.1",
        "gpt-4.1-mini",
    ],
    "Claude": [
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
    ],
}

GENRES = ["자유", "동화", "SF", "로맨스", "미스터리", "판타지", "일상 에세이", "공포"]
LENGTHS = {"짧게 (200자)": 200, "보통 (500자)": 500, "길게 (1000자)": 1000}

DEFAULT_SYSTEM_PROMPT = """당신은 창의적인 글쓰기 전문가입니다.
사용자가 제공하는 단어들을 모두 자연스럽게 포함하여 글을 작성합니다.
- 제시된 단어는 반드시 전부 사용해야 합니다.
- 문장이 자연스럽고 읽기 좋아야 합니다.
- 마크다운 없이 순수 텍스트로 작성합니다.
- 지정된 장르와 분량을 지켜야 합니다."""


# ── API 호출 함수 ──────────────────────────────────────────────────────────────
def call_gpt(api_key: str, model: str, system_prompt: str, user_message: str) -> str:
    from openai import AzureOpenAI
    client = AzureOpenAI(
        api_key=api_key,
        azure_endpoint=BASE_URL,
        api_version=GPT_API_VERSION,
        timeout=120,
        max_retries=0,
    )
    messages = []
    if system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.9,
        max_completion_tokens=4096,
    )
    return response.choices[0].message.content or ""


def call_claude(api_key: str, model: str, system_prompt: str, user_message: str) -> str:
    import anthropic
    client = anthropic.Anthropic(
        api_key=api_key,
        base_url=BASE_URL,
        max_retries=0,
        timeout=anthropic.Timeout(connect=30, read=120, write=60, pool=30),
    )
    kwargs = dict(
        model=model,
        max_tokens=8192,
        temperature=0.9,
        messages=[{"role": "user", "content": user_message}],
    )
    if system_prompt.strip():
        kwargs["system"] = system_prompt
    response = client.messages.create(**kwargs)
    return response.content[0].text


def call_gemini(api_key: str, model: str, system_prompt: str, user_message: str) -> str:
    from google import genai
    from google.genai.types import HttpOptions, GenerateContentConfig
    base = GEMINI_BASE_URL + api_key
    client = genai.Client(
        api_key=api_key,
        http_options=HttpOptions(api_version="v1", base_url=base),
    )
    config = GenerateContentConfig(temperature=0.9, max_output_tokens=8192)
    if system_prompt.strip():
        config.system_instruction = system_prompt
    response = client.models.generate_content(
        model=model, contents=user_message, config=config,
    )
    return response.text or ""


def call_api(engine: str, api_key: str, model: str,
             system_prompt: str, user_message: str) -> str:
    if engine == "GPT":
        return call_gpt(api_key, model, system_prompt, user_message)
    elif engine == "Claude":
        return call_claude(api_key, model, system_prompt, user_message)
    elif engine == "Gemini":
        return call_gemini(api_key, model, system_prompt, user_message)
    raise ValueError(f"알 수 없는 엔진: {engine}")


def load_api_key(path_or_key: str) -> str:
    """파일 경로면 읽고, 아니면 그대로 반환"""
    stripped = path_or_key.strip()
    if stripped.endswith(".txt"):
        try:
            for enc in ["utf-8-sig", "utf-8", "euc-kr", "cp949"]:
                try:
                    with open(stripped, encoding=enc) as f:
                        return f.read().strip()
                except UnicodeDecodeError:
                    continue
        except FileNotFoundError:
            pass
    return stripped


# ── Streamlit UI ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="AI 글짓기",
    page_icon="✍️",
    layout="wide",
)

st.title("✍️ AI 글짓기")
st.caption("단어 몇 개를 입력하면 AI가 글을 써드립니다 🚀")

# ── 사이드바 (QA팀 프롬프트 조정 영역) ────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 설정")

    st.subheader("API 키")
    uploaded_key_file = st.file_uploader(
        "API Key .txt 파일 드래그&드롭",
        type=["txt"],
    )
    api_key_input = ""
    if uploaded_key_file is not None:
        raw = uploaded_key_file.read()
        for enc in ["utf-8-sig", "utf-8", "euc-kr", "cp949"]:
            try:
                api_key_input = raw.decode(enc).strip()
                st.success("키 파일 로드 완료!")
                break
            except UnicodeDecodeError:
                continue

    st.subheader("모델 선택")
    engine = st.selectbox("AI 엔진", list(AI_ENGINES.keys()), index=0)
    model = st.selectbox("모델", AI_ENGINES[engine])

    st.divider()

    st.subheader("시스템 프롬프트")
    st.caption("QA팀: 여기서 프롬프트를 수정해 AI 성격을 바꾸세요!")
    system_prompt = st.text_area(
        label="system_prompt",
        value=DEFAULT_SYSTEM_PROMPT,
        height=250,
        label_visibility="collapsed",
    )

    st.divider()
    st.caption("🔄 코드 수정 후 Ctrl+S → 브라우저 Rerun 자동 감지")

# ── 메인 입력 영역 ─────────────────────────────────────────────────────────────
col1, col2 = st.columns([2, 1])

with col1:
    words_input = st.text_input(
        "단어 입력 (쉼표로 구분)",
        placeholder="예: 고양이, 우산, 여름밤, 비밀",
        help="2~10개 단어를 쉼표로 구분하여 입력하세요.",
    )

with col2:
    genre = st.selectbox("장르", GENRES)
    length_label = st.selectbox("분량", list(LENGTHS.keys()), index=1)

target_length = LENGTHS[length_label]

# ── 생성 버튼 ──────────────────────────────────────────────────────────────────
generate_btn = st.button("✨ 글짓기 시작!", type="primary", use_container_width=True)

# ── 결과 영역 ──────────────────────────────────────────────────────────────────
result_area = st.empty()

if generate_btn:
    # 입력 검증
    if not api_key_input.strip():
        st.warning("사이드바에서 API Key .txt 파일을 드래그&드롭해 주세요.")
        st.stop()

    words_raw = [w.strip() for w in words_input.split(",") if w.strip()]
    if len(words_raw) < 2:
        st.warning("단어를 쉼표로 구분하여 2개 이상 입력해 주세요.")
        st.stop()

    api_key = api_key_input

    words_str = ", ".join(f"**{w}**" for w in words_raw)
    user_message = (
        f"다음 단어를 모두 포함하여 {genre} 장르의 글을 작성해 주세요.\n"
        f"단어 목록: {', '.join(words_raw)}\n"
        f"목표 분량: 약 {target_length}자\n\n"
        f"단어를 자연스럽게 이야기 속에 녹여주세요."
    )

    with result_area.container():
        st.markdown("---")
        st.markdown(f"**입력 단어:** {words_str} &nbsp;|&nbsp; **장르:** {genre} &nbsp;|&nbsp; **분량:** {length_label}")
        with st.spinner("AI가 글을 쓰고 있습니다..."):
            try:
                start = time.time()
                result = call_api(engine, api_key, model, system_prompt, user_message)
                elapsed = time.time() - start

                st.success(f"완성! ({elapsed:.1f}초)")
                st.markdown("### 완성된 글")
                st.markdown(
                    f'<div style="background:#f8f9fa;padding:20px;border-radius:8px;'
                    f'border-left:4px solid #4CAF50;line-height:1.8;font-size:1.05em;">'
                    f'{result.replace(chr(10), "<br>")}'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                st.caption(f"모델: {engine} / {model} | 글자 수: {len(result)}자")

                # 복사용 텍스트
                with st.expander("텍스트 복사용"):
                    st.text_area("결과", result, height=200, label_visibility="collapsed")

            except Exception as e:
                st.error(f"API 호출 오류: {e}")
                st.info("API Key, 엔진/모델 선택, 네트워크 연결을 확인해 주세요.")
