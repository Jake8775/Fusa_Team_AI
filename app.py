"""
글짓기 AI 앱 — HMG 사내 게이트웨이 연동
단어 몇 개를 입력하면 AI가 글을 써드립니다.
"""

import os
os.environ.setdefault("PYTHONUTF8", "1")

import streamlit as st
import time
import subprocess
import uuid
from relay_component import relay_call


def get_git_version() -> str:
    try:
        root = os.path.dirname(os.path.abspath(__file__))
        # VERSION 파일: 1행=major, 2행=major 올릴 때의 커밋 기준값
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
        major = int(lines[0])
        base = int(lines[1]) if len(lines) > 1 else 0
        # git 전체 커밋 수 - 기준값 = minor
        r = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, cwd=root,
        )
        minor = max(0, int(r.stdout.strip()) - base) if r.returncode == 0 else 0
        return f"{major}.{minor:02d}"
    except Exception:
        return "dev"

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


# API 호출 함수는 relay.py(EXE)로 이전
# 이 앱은 UI + 릴레이 컴포넌트 연동만 담당


# ── Streamlit UI ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="AI 글짓기",
    page_icon="✍️",
    layout="wide",
)

st.title("✍️ AI 글짓기")
st.caption("단어 몇 개를 입력하면 AI가 글을 써드립니다... 🚀")

# ── 사이드바 (QA팀 프롬프트 조정 영역) ────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 설정")
    st.caption(f"ver. {get_git_version()}")

    # ── 릴레이 EXE 다운로드 (앱 진입 시 항상 표시) ──────────────────────────
    exe_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "HMG_Relay.exe")
    if os.path.exists(exe_path):
        with open(exe_path, "rb") as _f:
            st.download_button(
                label="⬇️ HMG_Relay.exe 다운로드",
                data=_f,
                file_name="HMG_Relay.exe",
                mime="application/octet-stream",
                use_container_width=True,
            )
        st.caption("다운로드 후 실행하고 이 앱을 사용하세요.")
    st.divider()

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

# ── 버튼 클릭: 릴레이 요청 생성 ──────────────────────────────────────────────
if generate_btn:
    if not api_key_input.strip():
        st.warning("사이드바에서 API Key .txt 파일을 드래그&드롭해 주세요.")
        st.stop()

    words_raw = [w.strip() for w in words_input.split(",") if w.strip()]
    if len(words_raw) < 2:
        st.warning("단어를 쉼표로 구분하여 2개 이상 입력해 주세요.")
        st.stop()

    user_message = (
        f"다음 단어를 모두 포함하여 {genre} 장르의 글을 작성해 주세요.\n"
        f"단어 목록: {', '.join(words_raw)}\n"
        f"목표 분량: 약 {target_length}자\n\n"
        f"단어를 자연스럽게 이야기 속에 녹여주세요."
    )
    st.session_state.relay_req = {
        "request_id":   str(uuid.uuid4()),
        "engine":       engine,
        "api_key":      api_key_input,
        "model":        model,
        "system_prompt": system_prompt,
        "user_message": user_message,
    }
    st.session_state.relay_meta = {
        "words_raw":    words_raw,
        "genre":        genre,
        "length_label": length_label,
        "engine":       engine,
        "model":        model,
    }
    st.session_state.relay_res = None

# ── 릴레이 컴포넌트 (비동기, 비표시) ─────────────────────────────────────────
req = st.session_state.get("relay_req")
if req:
    comp = relay_call(**req)
    if comp is not None and comp.get("request_id") == req["request_id"]:
        st.session_state.relay_res = comp
        st.session_state.relay_req = None
        st.rerun()

# ── 결과 표시 ──────────────────────────────────────────────────────────────────
if st.session_state.get("relay_req"):
    st.info("🔄 릴레이 EXE가 내부 API를 호출 중입니다...")

elif st.session_state.get("relay_res"):
    res  = st.session_state.relay_res
    meta = st.session_state.get("relay_meta", {})

    if res.get("error"):
        st.error(f"API 호출 오류: {res['error']}")
    else:
        result  = res["result"]
        elapsed = res["elapsed"]
        words_str = ", ".join(f"**{w}**" for w in meta.get("words_raw", []))

        st.markdown("---")
        st.markdown(
            f"**입력 단어:** {words_str} &nbsp;|&nbsp; "
            f"**장르:** {meta.get('genre','')} &nbsp;|&nbsp; "
            f"**분량:** {meta.get('length_label','')}"
        )
        st.success(f"완성! ({elapsed:.1f}초)")
        st.markdown("### 완성된 글")
        st.markdown(
            f'<div style="background:#f8f9fa;padding:20px;border-radius:8px;'
            f'border-left:4px solid #4CAF50;line-height:1.8;font-size:1.05em;">'
            f'{result.replace(chr(10), "<br>")}'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            f"모델: {meta.get('engine','')} / {meta.get('model','')} | 글자 수: {len(result)}자"
        )
        with st.expander("텍스트 복사용"):
            st.text_area("결과", result, height=200, label_visibility="collapsed")
