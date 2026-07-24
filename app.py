"""
차종 게이트 일정관리 앱 — HMG 사내 게이트웨이 연동
"""

import os
os.environ.setdefault("PYTHONUTF8", "1")

import base64
import calendar
import io
import json
import subprocess
import re
import uuid
from datetime import date, datetime

import pandas as pd
import requests
import streamlit as st
from requests.auth import HTTPBasicAuth
from relay_component import relay_call

try:
    import plotly.express as px
    PLOTLY_OK = True
except ImportError:
    PLOTLY_OK = False

try:
    from PyPDF2 import PdfReader
    PYPDF2_OK = True
except ImportError:
    PYPDF2_OK = False


# ── 상수 ─────────────────────────────────────────────────────────────────────
AI_ENGINES = {
    "Gemini": ["gemini-2.5-pro", "gemini-2.5-flash"],
    "GPT":    ["gpt-4.1", "gpt-4.1-mini"],
    "Claude": ["claude-sonnet-4-6", "claude-haiku-4-5"],
}

CONFLUENCE_BASE    = "https://hmg.atlassian.net"
CONFLUENCE_PAGE_ID = "604276516"

JIRA_BASE    = "https://ade-jira.hmckmc.co.kr"
JIRA_PROJECT = "VDPGTINSP"

DATE_KEYS = ["submission_request", "practical_meeting", "preliminary_meeting", "final_meeting"]
DATE_LABELS = {
    "submission_request":  "제출요청",
    "practical_meeting":   "실무회의",
    "preliminary_meeting": "예비회의",
    "final_meeting":       "본회의",
}
COLOR_MAP = {
    "submission_request":  "#1E90FF",
    "practical_meeting":   "#2ECC71",
    "preliminary_meeting": "#9B59B6",
    "final_meeting":       "#E67E22",
}

SAVE_LIMIT_MSG = (
    "저장 기능은 현재 제한됩니다.\n"
    "연결된 JIRA는 운영(Live) 서버로, 실제 데이터의 추가·수정·삭제 기능은 구현할 수 있으나 "
    "테스트 및 실제 호출은 허용되지 않습니다.\n"
    "티켓 조회 화면은 실제 웹 브라우저 티켓보기와 속성 및 형식이 일치해야 합니다. "
    "조회화면에서 사용자 입력은 허용하나, 실제 저장 버튼은 Disable 되어야 합니다."
)

EXTRACT_PROMPT = (
    "아래 문서에서 차종 게이트 일정을 추출하세요.\n"
    "결과는 반드시 JSON 배열로만 답변하세요:\n"
    '[{{"car_type":"","event":"","owner":"","dates":{{"submission_request":"",'
    '"practical_meeting":"","preliminary_meeting":"","final_meeting":""}}}}]\n'
    "날짜 없으면 null. 이벤트는 GATE-3/GATE-4/GATE-5/P2/LP2 중 하나.\n"
    "문서: {text}"
)


# ── 유틸리티 ──────────────────────────────────────────────────────────────────
def get_git_version() -> str:
    try:
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
        major = int(lines[0])
        base  = int(lines[1]) if len(lines) > 1 else 0
        r = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, cwd=root,
        )
        minor = max(0, int(r.stdout.strip()) - base) if r.returncode == 0 else 0
        return f"{major}.{minor:02d}"
    except Exception:
        return "dev"


def read_txt_file(uploaded, session_key: str) -> str:
    if uploaded is not None:
        try:
            uploaded.seek(0)
            raw = uploaded.read()
            for enc in ["utf-8-sig", "utf-8", "euc-kr", "cp949"]:
                try:
                    val = raw.decode(enc).strip()
                    st.session_state[session_key] = val
                    return val
                except UnicodeDecodeError:
                    continue
        except Exception:
            pass
    return st.session_state.get(session_key, "")


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return d.replace(year=d.year + m // 12, month=m % 12 + 1, day=1)


def detect_mime(raw: bytes) -> str:
    if raw[:4] == b"\x89PNG":  return "image/png"
    if raw[:2] == b"\xff\xd8": return "image/jpeg"
    if raw[:4] == b"GIF8":     return "image/gif"
    return "image/webp"


def parse_file(uploaded_file) -> str:
    uploaded_file.seek(0)
    raw  = uploaded_file.read()
    name = uploaded_file.name.lower()
    if name.endswith((".xlsx", ".xls")):
        try:
            return pd.read_excel(io.BytesIO(raw)).to_string(index=False)
        except Exception as e:
            return f"[Excel 오류: {e}]"
    if name.endswith(".pdf"):
        if PYPDF2_OK:
            try:
                reader = PdfReader(io.BytesIO(raw))
                return "\n".join(p.extract_text() or "" for p in reader.pages)
            except Exception as e:
                return f"[PDF 오류: {e}]"
        return "[PyPDF2 미설치]"
    if name.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
        mime = detect_mime(raw)
        b64  = base64.b64encode(raw).decode("ascii")
        return f"[IMAGE|{mime}|{b64}]"
    for enc in ["utf-8-sig", "utf-8", "euc-kr", "cp949"]:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp949", errors="replace")


def parse_ai_response(text: str) -> list:
    try:
        s = text.find("[")
        e = text.rfind("]") + 1
        if s >= 0 and e > s:
            return json.loads(text[s:e])
    except (json.JSONDecodeError, ValueError):
        pass
    return []


def candidates_to_html(candidates: list) -> str:
    header = (
        "<tr><th>차종</th><th>이벤트</th><th>담당자</th>"
        "<th>제출요청</th><th>실무회의</th><th>예비회의</th><th>본회의</th></tr>"
    )
    rows = "".join(
        f"<tr><td>{c.get('car_type','')}</td><td>{c.get('event','')}</td>"
        f"<td>{c.get('owner','')}</td>"
        f"<td>{(c.get('dates') or {}).get('submission_request','') or ''}</td>"
        f"<td>{(c.get('dates') or {}).get('practical_meeting','') or ''}</td>"
        f"<td>{(c.get('dates') or {}).get('preliminary_meeting','') or ''}</td>"
        f"<td>{(c.get('dates') or {}).get('final_meeting','') or ''}</td></tr>"
        for c in candidates if c.get("confirmed")
    )
    return f"<table><tbody>{header}{rows}</tbody></table>"


def _atlassian_kwargs(email: str, token: str) -> dict:
    if email:
        return {"auth": HTTPBasicAuth(email, token)}
    return {"headers": {"Authorization": f"Bearer {token}"}}


def confluence_update(email: str, token: str, html_body: str):
    page_url = f"{CONFLUENCE_BASE}/wiki/api/v2/pages/{CONFLUENCE_PAGE_ID}"
    kw = _atlassian_kwargs(email, token)
    try:
        r = requests.get(page_url, params={"body-format": "storage"}, timeout=10, **kw)
        if r.status_code == 404:
            return False, "404: 페이지 없음 또는 인증 오류"
        r.raise_for_status()
        data    = r.json()
        version = data["version"]["number"] + 1
        title   = data["title"]
    except requests.RequestException as e:
        return False, f"GET 실패: {e}"
    payload = {
        "id": CONFLUENCE_PAGE_ID, "status": "current", "title": title,
        "body": {"representation": "storage", "value": html_body},
        "version": {"number": version},
    }
    try:
        r = requests.put(page_url, json=payload, timeout=15, **kw)
        if r.status_code in (200, 201):
            return True, "Confluence 업데이트 성공"
        return False, f"PUT {r.status_code}: {r.text[:300]}"
    except requests.RequestException as e:
        return False, f"PUT 실패: {e}"


def jira_get_fields(jira_token: str) -> dict:
    """JIRA 필드 목록 조회 — ade-jira는 Bearer 토큰 사용"""
    if st.session_state.get("jira_field_map"):
        return st.session_state.jira_field_map
    kw = _atlassian_kwargs("", jira_token)   # 이메일 없음 → Bearer
    try:
        r = requests.get(f"{JIRA_BASE}/rest/api/3/field", timeout=10, **kw)
        if r.ok:
            fmap = {f["id"]: f["name"] for f in r.json()}
            st.session_state.jira_field_map = fmap
            return fmap
    except Exception:
        pass
    return {}


def find_custom_field(field_map: dict, target: str):
    for fid, fname in field_map.items():
        if target in fname:
            return fid
    return None


def jira_search(jira_token: str, jql: str, my_only: bool = False):
    """JIRA 검색 — ade-jira는 Bearer 토큰 사용"""
    kw  = _atlassian_kwargs("", jira_token)  # 이메일 없음 → Bearer
    url = f"{JIRA_BASE}/rest/api/3/search"
    if my_only:
        jql = f"({jql}) AND assignee = currentUser()"
    params = {"jql": jql, "maxResults": 50, "fields": "*all"}
    try:
        r = requests.get(url, params=params, timeout=15, **kw)
        if r.status_code == 404:
            return [], "404: 인증 오류 또는 URL 오류"
        r.raise_for_status()
        return r.json().get("issues", []), ""
    except requests.RequestException as e:
        return [], str(e)


def field_val_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("displayName", "name", "value", "key"):
            if v.get(k):
                return str(v[k])
    if isinstance(v, list):
        return ", ".join(field_val_str(i) for i in v)
    return str(v)


def fmt_dt(s: str) -> str:
    try:
        return datetime.fromisoformat(s[:19]).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s or ""


def cf_val(fields: dict, fmap: dict, *names) -> str:
    """이름으로 커스텀 필드 값 조회 (복수 후보 순서대로)"""
    for name in names:
        fid = find_custom_field(fmap, name)
        if fid:
            v = field_val_str(fields.get(fid))
            if v:
                return v
    return ""


# ── 세션 상태 초기화 ─────────────────────────────────────────────────────────
def _init():
    for k, v in {
        "candidates":     [],
        "relay_req":      None,
        "relay_res":      None,
        "relay_images":   [],
        "current_month":  date.today().replace(day=1),
        "jira_issues":    [],
        "jira_field_map": {},
        "selected_car":   None,
        "file_cache":     {},
        "selected_file":  None,
        "api_key":        "",
        "jira_token":     "",   # ade-jira.hmckmc.co.kr (Bearer)
        "cf_token":       "",   # hmg.atlassian.net (Basic Auth)
        "cf_email":       "",   # Confluence Basic Auth용 이메일
        "ai_engine":      "Gemini",
        "ai_model":       "gemini-2.5-pro",
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()


# ── 페이지 설정 ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="차종 게이트 일정관리",
    page_icon="📅",
    layout="wide",
    initial_sidebar_state="collapsed",
)
st.markdown("""
<style>
  [data-testid="stSidebar"] { display:none; }
  .block-container { padding-top:0.6rem !important; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# 설정 모달
# ══════════════════════════════════════════════════════════════════════════════
@st.dialog("settings", width="large")
def settings_dialog():
    st.subheader("JIRA / Confluence / H-CHAT 설정")
    c1, c2, c3 = st.columns(3)

    with c1:
        st.markdown("**JIRA**")
        st.text_input("JIRA Base URL", value=JIRA_BASE, disabled=True)
        st.text_input("Project Key",   value=JIRA_PROJECT, disabled=True)
        f_tok = st.file_uploader("JIRA Token (.txt)", type=["txt"], key="dlg_jtok")
        tok_from_file = read_txt_file(f_tok, "cf_token")
        jt = st.text_input("JIRA Token",
                           value=tok_from_file or st.session_state.get("cf_token", ""),
                           type="password")

    with c2:
        st.markdown("**Confluence**")
        st.text_input("Base URL", value=CONFLUENCE_BASE, disabled=True)
        st.text_input("Space / Page", value=f"RND / {CONFLUENCE_PAGE_ID}", disabled=True)
        st.info("Confluence Token = JIRA Token (동일 Atlassian 계정)")
        em = st.text_input("이메일 (Cloud만 필요)",
                           value=st.session_state.get("cf_email", ""),
                           placeholder="user@hmg.com")

    with c3:
        st.markdown("**H-CHAT**")
        f_api = st.file_uploader("H-CHAT API Key (.txt)", type=["txt"], key="dlg_api")
        ak = read_txt_file(f_api, "api_key")
        if ak:
            st.success("API 키 로드됨")
        eng = st.selectbox("AI 엔진", list(AI_ENGINES.keys()), key="dlg_eng",
                           index=list(AI_ENGINES.keys()).index(
                               st.session_state.get("ai_engine", "Gemini")))
        mdl = st.selectbox("모델", AI_ENGINES[eng], key="dlg_mdl")

    st.divider()
    if st.button("설정 저장", type="primary", use_container_width=True):
        st.session_state.cf_token       = jt or tok_from_file
        st.session_state.cf_email       = em
        st.session_state.ai_engine      = eng
        st.session_state.ai_model       = mdl
        st.session_state.jira_field_map = {}
        st.success("저장됨")
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# JIRA 상세 모달
# ══════════════════════════════════════════════════════════════════════════════
@st.dialog("JIRA detail", width="large")
def jira_detail_dialog(issue: dict):
    f    = issue.get("fields", {}) or {}
    fmap = st.session_state.get("jira_field_map", {})
    key  = issue.get("key", "")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**JIRA Key:** `{key}`")
        st.markdown(f"**프로젝트:** {field_val_str(f.get('project'))}")
        st.markdown(f"**차종:** {cf_val(f, fmap, 'Vehicle', 'Car', 'car')}")
        st.markdown(f"**단계:** {cf_val(f, fmap, 'Stage', 'Phase', 'step')}")
        st.markdown(f"**점검유형:** {cf_val(f, fmap, 'Inspection', 'Check', 'Type')}")
        st.markdown(f"**시스템:** {cf_val(f, fmap, 'System', 'system')}")
    with c2:
        st.markdown(f"**Summary:** {f.get('summary', '')}")
        st.markdown(f"**Status:** {field_val_str(f.get('status'))}")
        st.markdown(f"**Priority:** {field_val_str(f.get('priority'))}")
        st.markdown(f"**Assignee:** {field_val_str(f.get('assignee'))}")
        st.markdown(f"**최근 업데이트:** {fmt_dt(f.get('updated', ''))}")
        st.markdown(f"**Issue Type:** {field_val_str(f.get('issuetype'))}")

    st.divider()
    c3, c4 = st.columns(2)
    with c3:
        st.text_input("점검결과", value=cf_val(f, fmap, "Result", "Outcome"),
                      key=f"res_{key}")
    with c4:
        st.text_input("URL", value=cf_val(f, fmap, "URL", "Link"), key=f"url_{key}")

    st.markdown("**첨부 / 결과 파일**")
    st.file_uploader("파일을 드래그하세요", key=f"att_{key}",
                     accept_multiple_files=True, label_visibility="collapsed")
    st.text_area("결과 메모", key=f"memo_{key}", height=80)

    st.divider()
    b1, b2 = st.columns([3, 1])
    with b1:
        st.warning(SAVE_LIMIT_MSG)
    with b2:
        st.button("닫기", key=f"close_{key}", use_container_width=True)
        st.button("JIRA 업데이트", disabled=True, key=f"upd_{key}", use_container_width=True)


# ── 런타임 인증값 ──────────────────────────────────────────────────────────────
api_key    = st.session_state.get("api_key", "")
jira_token = st.session_state.get("jira_token", "")
cf_token   = st.session_state.get("cf_token", "")
cf_email   = st.session_state.get("cf_email", "")
engine     = st.session_state.get("ai_engine", "Gemini")
model      = st.session_state.get("ai_model", "gemini-2.5-pro")
has_ai     = bool(api_key)
has_jira   = bool(jira_token)
has_cf     = bool(cf_token)   # Confluence (hmg.atlassian.net)


# ══════════════════════════════════════════════════════════════════════════════
# 헤더
# ══════════════════════════════════════════════════════════════════════════════
hc = st.columns([5, 1, 1, 1, 0.7])
with hc[0]:
    st.markdown(
        "<div style='white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"
        "font-size:20px;font-weight:700;line-height:1.3;padding-top:2px'>"
        f"📅 FS 차종 게이트 일정 관리 App"
        f"<span style='font-size:11px;color:#888;font-weight:400'>"
        f" &nbsp;ver.{get_git_version()}</span></div>",
        unsafe_allow_html=True,
    )
with hc[1]:
    dot = "🟢" if has_cf else "🔴"
    st.markdown(
        f"<div style='text-align:center;padding-top:6px;font-size:12px'>"
        f"{dot}<br>Confluence</div>",
        unsafe_allow_html=True,
    )
with hc[2]:
    dot = "🟢" if has_jira else "🔴"
    st.markdown(
        f"<div style='text-align:center;padding-top:6px;font-size:12px'>"
        f"{dot}<br>ADE Jira</div>",
        unsafe_allow_html=True,
    )
with hc[3]:
    dot = "🟢" if has_ai else "🔴"
    st.markdown(
        f"<div style='text-align:center;padding-top:6px;font-size:12px'>"
        f"{dot}<br>AI</div>",
        unsafe_allow_html=True,
    )
with hc[4]:
    if st.button("⚙️", help="AI 모델 등 추가 설정", use_container_width=True):
        settings_dialog()

st.divider()


# ══════════════════════════════════════════════════════════════════════════════
# 메인: [입력 33%] | [타임라인+하단 67%]
# ══════════════════════════════════════════════════════════════════════════════
left_col, right_col = st.columns([33, 67])


# ─────────────────────────────────────────────────────────────────────────────
# 왼쪽 — 입력
# ─────────────────────────────────────────────────────────────────────────────
with left_col:
    # ── 인증 파일 (항상 표시) ────────────────────────────────────────────────
    c_ai, c_jira = st.columns(2)
    with c_ai:
        f_api_main = st.file_uploader("① H-CHAT API 키", type=["txt"], key="main_api")
        read_txt_file(f_api_main, "api_key")
        api_key = st.session_state.get("api_key", "")
        has_ai  = bool(api_key)
    with c_jira:
        f_jira_main = st.file_uploader("② JIRA 토큰", type=["txt"], key="main_jira")
        read_txt_file(f_jira_main, "jira_token")
        jira_token = st.session_state.get("jira_token", "")
        has_jira   = bool(jira_token)

    c_cf, c_em = st.columns(2)
    with c_cf:
        f_cf_main = st.file_uploader("③ Confluence 토큰", type=["txt"], key="main_cf")
        read_txt_file(f_cf_main, "cf_token")
        cf_token = st.session_state.get("cf_token", "")
        has_cf   = bool(cf_token)
    with c_em:
        st.markdown("<div style='font-size:14px'>④ Confluence 이메일</div>",
                    unsafe_allow_html=True)
        cf_email_input = st.text_input(
            "Confluence 이메일",
            value=st.session_state.get("cf_email", ""),
            placeholder="user@hmg.com",
            label_visibility="collapsed",
        )
        if cf_email_input:
            st.session_state.cf_email = cf_email_input
            cf_email = cf_email_input

    st.divider()
    st.markdown("#### 입력")
    st.caption("입력자료를 선택하면 해당 입력자료에 대응되는 일정 추출 후보가 표시됩니다.")

    uploaded_files = st.file_uploader(
        "파일 또는 텍스트를 여기로 드래그하세요",
        type=["xlsx", "xls", "pdf", "jpg", "jpeg", "png", "gif", "webp", "txt", "msg"],
        accept_multiple_files=True,
        help="Email / PDF / JPG / PNG / Text / Excel 지원",
    )
    if uploaded_files:
        cur_names = {uf.name for uf in uploaded_files}
        for name in list(st.session_state.file_cache.keys()):
            if name not in cur_names and not name.startswith("_txt_"):
                del st.session_state.file_cache[name]
        for uf in uploaded_files:
            if uf.name not in st.session_state.file_cache:
                st.session_state.file_cache[uf.name] = parse_file(uf)
    else:
        for k in [k for k in st.session_state.file_cache if not k.startswith("_txt_")]:
            del st.session_state.file_cache[k]

    with st.expander("텍스트 붙여넣기 (이메일/메모)"):
        direct_text = st.text_area("내용", height=90, label_visibility="collapsed",
                                   placeholder="이메일 본문, 회의록 등...",
                                   key="direct_text_input")
        if st.button("추가", use_container_width=True):
            if direct_text.strip():
                st.session_state.file_cache[f"_txt_{uuid.uuid4().hex[:6]}"] = direct_text.strip()
                st.rerun()

    # 입력자료 목록 (sourceList)
    all_files = list(st.session_state.file_cache.keys())
    if all_files:
        st.markdown("**입력자료 목록**")
        for fname in all_files:
            label = fname if not fname.startswith("_txt_") else f"텍스트({fname[-6:]})"
            fc1, fc2 = st.columns([7, 1])
            btn_type = "primary" if st.session_state.selected_file == fname else "secondary"
            if fc1.button(label[:38], key=f"src_{fname}",
                          use_container_width=True, type=btn_type):
                st.session_state.selected_file = fname
                st.rerun()
            if fc2.button("x", key=f"del_{fname}"):
                del st.session_state.file_cache[fname]
                if st.session_state.selected_file == fname:
                    st.session_state.selected_file = None
                st.rerun()

    if st.button("AI 일정 추출", type="primary", use_container_width=True,
                 disabled=not (st.session_state.file_cache and has_ai)):
        _img_re = re.compile(r"^\[IMAGE\|([^|]+)\|(.+)\]$", re.DOTALL)
        _images, _texts = [], []
        for _n, _t in st.session_state.file_cache.items():
            _m = _img_re.match(_t.strip())
            if _m:
                _images.append({"mime": _m.group(1), "b64": _m.group(2)})
            else:
                _texts.append(f"[{_n}]\n{_t}")
        combined = "\n\n".join(_texts)
        st.session_state.relay_req = {
            "request_id":    str(uuid.uuid4()),
            "engine":        engine,
            "api_key":       api_key,
            "model":         model,
            "system_prompt": "당신은 일정 정보를 추출하는 전문가입니다. JSON 배열로만 답변하세요.",
            "user_message":  EXTRACT_PROMPT.format(text=combined[:12000]),
            "images":        _images,
        }
        st.session_state.relay_images = _images
        st.session_state.relay_res = None

    req = st.session_state.relay_req
    if req:
        comp = relay_call(**req)
        if comp is not None and comp.get("request_id") == req["request_id"]:
            st.session_state.relay_res = comp
            st.session_state.relay_req = None
            st.rerun()
    if st.session_state.relay_req:
        st.info("AI 추출 중...")
    if st.session_state.relay_res:
        res = st.session_state.relay_res
        st.session_state.relay_res = None
        if res.get("error"):
            st.error(f"오류: {res['error']}")
        else:
            extracted = parse_ai_response(res["result"])
            if extracted:
                existing = {(c["car_type"], c["event"]) for c in st.session_state.candidates}
                for nc in extracted:
                    nc.setdefault("confirmed", False)
                    nc.setdefault("source", "")
                    if (nc.get("car_type", ""), nc.get("event", "")) not in existing:
                        st.session_state.candidates.append(nc)
                        existing.add((nc.get("car_type", ""), nc.get("event", "")))
                st.success(f"{len(extracted)}개 추출 완료")
            else:
                st.warning("일정을 찾지 못했습니다.")

    st.divider()
    st.markdown("**선택된 입력자료별 일정 추출 후보**")

    if st.session_state.candidates:
        rows = [
            {
                "확정":   c.get("confirmed", False),
                "차종":   c.get("car_type", ""),
                "이벤트": c.get("event", ""),
                "담당자": c.get("owner", ""),
                "제출요청": (c.get("dates") or {}).get("submission_request", "") or "",
                "실무회의": (c.get("dates") or {}).get("practical_meeting", "") or "",
                "예비회의": (c.get("dates") or {}).get("preliminary_meeting", "") or "",
                "본회의":  (c.get("dates") or {}).get("final_meeting", "") or "",
            }
            for c in st.session_state.candidates
        ]
        edited = st.data_editor(
            pd.DataFrame(rows), use_container_width=True,
            num_rows="dynamic", height=190,
            column_config={
                "확정":   st.column_config.CheckboxColumn("확정"),
                "이벤트": st.column_config.SelectboxColumn(
                    "이벤트", options=["GATE-3", "GATE-4", "GATE-5", "P2", "LP2"]
                ),
            },
            key="candidates_editor",
        )
        st.session_state.candidates = [
            {
                "confirmed": bool(row.get("확정", False)),
                "car_type":  str(row.get("차종", "")),
                "event":     str(row.get("이벤트", "")),
                "owner":     str(row.get("담당자", "")),
                "dates": {
                    "submission_request":  row.get("제출요청", "") or None,
                    "practical_meeting":   row.get("실무회의", "") or None,
                    "preliminary_meeting": row.get("예비회의", "") or None,
                    "final_meeting":       row.get("본회의", "")  or None,
                },
                "source": "",
            }
            for _, row in edited.iterrows()
        ]
        confirmed_count = sum(1 for c in st.session_state.candidates if c.get("confirmed"))

        bc1, bc2 = st.columns(2)
        with bc1:
            if st.button("초기화", use_container_width=True):
                st.session_state.candidates  = []
                st.session_state.file_cache  = {}
                st.session_state.selected_file = None
                st.rerun()
        with bc2:
            if st.button(
                f"Confluence 반영 ({confirmed_count}건)",
                type="primary", use_container_width=True,
                disabled=not (confirmed_count and has_cf and cf_email),
                key="confirmBtn",
            ):
                ok, msg = confluence_update(
                    cf_email, cf_token,
                    candidates_to_html(st.session_state.candidates),
                )
                (st.success if ok else st.error)(msg)
    else:
        st.caption("파일을 업로드하고 AI 추출을 실행하세요.")


# ─────────────────────────────────────────────────────────────────────────────
# 오른쪽 — 타임라인 + 하단
# ─────────────────────────────────────────────────────────────────────────────
with right_col:
    cur_m  = st.session_state.current_month
    next_m = add_months(cur_m, 1)

    # 네비게이션
    nav = st.columns([1, 7, 1, 1])
    with nav[0]:
        if st.button("◀", use_container_width=True, key="prevMonthBtn"):
            st.session_state.current_month = add_months(cur_m, -1)
            st.rerun()
    with nav[1]:
        st.markdown(
            "<h4 style='text-align:center;margin:4px 0'>"
            f"차종 게이트 이벤트 타임라인 "
            f"— {cur_m.strftime('%Y년 %m월')} ~ {next_m.strftime('%m월')}"
            "</h4>"
            "<p style='text-align:center;margin:0;font-size:12px;color:#888'>"
            "차종 클릭 시, 우측의 Jira 조회가 연동됩니다</p>",
            unsafe_allow_html=True,
        )
    with nav[2]:
        if st.button("▶", use_container_width=True, key="nextMonthBtn"):
            st.session_state.current_month = add_months(cur_m, 1)
            st.rerun()
    with nav[3]:
        if st.button("🔄", help="새로고침", use_container_width=True, key="refreshBtn"):
            st.session_state.jira_field_map = {}
            st.rerun()

    # 빅 간트 (차종별 1라인)
    if not PLOTLY_OK:
        st.warning("`pip install plotly` 필요")
    elif not st.session_state.candidates:
        st.info("파일을 업로드하고 AI 추출을 실행하면 타임라인이 표시됩니다.")
    else:
        _, last2  = calendar.monthrange(next_m.year, next_m.month)
        range_end = next_m.replace(day=last2)

        tl_rows = []
        for c in st.session_state.candidates:
            for dk in DATE_KEYS:
                dval = (c.get("dates") or {}).get(dk)
                if not dval:
                    continue
                try:
                    dt = datetime.strptime(dval, "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    continue
                if cur_m <= dt <= range_end:
                    tl_rows.append({
                        "차종":    c.get("car_type", ""),
                        "이벤트":  c.get("event", ""),
                        "담당자":  c.get("owner", ""),
                        "일정구분": DATE_LABELS[dk],
                        "시작":    dval,
                        "종료":    dval,
                    })

        if tl_rows:
            df_tl = pd.DataFrame(tl_rows)
            df_tl["시작"] = pd.to_datetime(df_tl["시작"])
            df_tl["종료"] = pd.to_datetime(df_tl["종료"]) + pd.Timedelta(days=1)
            color_map = {v: COLOR_MAP[k] for k, v in DATE_LABELS.items()}

            fig = px.timeline(
                df_tl, x_start="시작", x_end="종료",
                y="차종", color="일정구분",
                hover_data={"이벤트": True, "담당자": True},
                color_discrete_map=color_map,
            )
            fig.update_yaxes(autorange="reversed")
            fig.update_xaxes(
                range=[str(cur_m), str(range_end)],
                tickformat="%m/%d", dtick="D3",
            )
            n_cars = len({r["차종"] for r in tl_rows})
            fig.update_layout(
                height=max(200, n_cars * 55 + 100),
                margin=dict(l=10, r=10, t=10, b=10),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            try:
                event = st.plotly_chart(
                    fig, use_container_width=True,
                    on_select="rerun", key="timelineRows",
                )
                if (event and hasattr(event, "selection")
                        and event.selection and event.selection.points):
                    car = str(event.selection.points[0].get("y") or "").strip()
                    if car:
                        st.session_state.selected_car = car
                        st.toast(f"'{car}' 선택 — JIRA 조회 중...")
            except TypeError:
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.info(f"{cur_m.strftime('%Y-%m')} ~ {next_m.strftime('%Y-%m')} 범위에 일정이 없습니다.")

    st.divider()

    # 하단: [월별이벤트 30%] | [JIRA 70%]
    ev_col, jira_col = st.columns([30, 70])

    with ev_col:
        st.markdown(f"**월별 이벤트 조회 — {cur_m.year}년 {cur_m.month}월**")
        ev_rows = []
        for c in st.session_state.candidates:
            for dk in DATE_KEYS:
                dval = (c.get("dates") or {}).get(dk)
                if not dval:
                    continue
                try:
                    dt = datetime.strptime(dval, "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    continue
                _, last2 = calendar.monthrange(next_m.year, next_m.month)
                if cur_m <= dt <= next_m.replace(day=last2):
                    ev_rows.append({
                        "날짜":   dval,
                        "차종":   c.get("car_type", ""),
                        "이벤트": c.get("event", ""),
                        "구분":   DATE_LABELS[dk],
                        "담당":   c.get("owner", ""),
                        "확정":   "O" if c.get("confirmed") else "-",
                    })
        if ev_rows:
            st.dataframe(
                pd.DataFrame(ev_rows).sort_values("날짜").reset_index(drop=True),
                use_container_width=True, hide_index=True, height=240,
            )
        else:
            st.caption("이 기간에 이벤트가 없습니다.")

    with jira_col:
        car_types = sorted({c["car_type"] for c in st.session_state.candidates if c.get("car_type")})
        car_opts  = ["전체"] + car_types

        auto_car = st.session_state.get("selected_car")
        default_idx = car_opts.index(auto_car) if auto_car and auto_car in car_types else 0

        st.markdown("**JIRA 티켓**")
        st.caption("특정 차종을 선택하면 관련 Jira가 조회됩니다.")

        jc = st.columns([4, 1, 1])
        with jc[0]:
            selected_car = st.selectbox(
                "Jira 조회 선택 차종",
                car_opts, index=default_idx,
                label_visibility="collapsed",
            )
        with jc[1]:
            my_only = st.toggle("내 티켓", key="myTicketsOnly")
        with jc[2]:
            jira_btn = st.button("조회", type="primary", use_container_width=True)

        auto_run = bool(auto_car and auto_car in car_types and has_jira)
        if auto_run:
            st.session_state.selected_car = None

        if jira_btn or auto_run:
            if not has_jira:
                st.warning("왼쪽 패널에서 JIRA 토큰(.txt)을 업로드하세요.")
            else:
                jql = f"project={JIRA_PROJECT} ORDER BY updated DESC"
                if selected_car != "전체":
                    jql = f'project={JIRA_PROJECT} AND text~"{selected_car}" ORDER BY updated DESC'
                with st.spinner("JIRA 조회 중..."):
                    fmap = jira_get_fields(jira_token)
                    issues, err = jira_search(jira_token, jql, my_only)
                st.session_state.jira_issues = issues
                if err:
                    st.error(err)
                elif not issues:
                    st.info("검색 결과가 없습니다.")
                else:
                    st.success(f"{len(issues)}건")

        if st.session_state.jira_issues:
            fmap = st.session_state.get("jira_field_map", {})

            # 스펙 컬럼: Key | 단계 | 점검유형 | 시스템 | 상태 | 담당자 | 최근업데이트
            grid_rows = []
            for issue in st.session_state.jira_issues:
                f = issue.get("fields", {}) or {}
                grid_rows.append({
                    "Key":      issue.get("key", ""),
                    "단계":     cf_val(f, fmap, "Stage", "Phase"),
                    "점검유형": cf_val(f, fmap, "Inspection", "Check"),
                    "시스템":   cf_val(f, fmap, "System"),
                    "상태":     field_val_str(f.get("status")),
                    "담당자":   field_val_str(f.get("assignee")),
                    "최근업데이트": fmt_dt(f.get("updated", "")),
                })

            st.caption("행을 클릭하면 상세보기가 열립니다.")
            try:
                sel = st.dataframe(
                    pd.DataFrame(grid_rows),
                    use_container_width=True, hide_index=True, height=230,
                    on_select="rerun", selection_mode="single-row",
                    key="jiraGridBody",
                )
                if sel.selection.rows:
                    jira_detail_dialog(
                        st.session_state.jira_issues[sel.selection.rows[0]]
                    )
            except (TypeError, AttributeError):
                st.dataframe(pd.DataFrame(grid_rows),
                             use_container_width=True, hide_index=True, height=200)
                for idx, issue in enumerate(st.session_state.jira_issues[:10]):
                    if st.button(f"{issue.get('key','')} 상세", key=f"db_{idx}"):
                        jira_detail_dialog(issue)
        else:
            st.caption("차종을 선택하고 조회 버튼을 클릭하세요.")


# ══════════════════════════════════════════════════════════════════════════════
# Status Bar
# ══════════════════════════════════════════════════════════════════════════════
st.divider()
sb = st.columns([2, 2, 2, 2, 4, 1.5])
with sb[0]:
    st.caption(f"{'[CF]' if has_cf else '[CF]'} Confluence {'연결됨' if has_cf else '미설정'}")
with sb[1]:
    st.caption(f"JIRA {'연결됨' if has_cf else '미설정'}")
with sb[2]:
    st.caption(f"AI {'대기' if has_ai else '미설정'}")
with sb[3]:
    pending = sum(1 for c in st.session_state.candidates if not c.get("confirmed"))
    st.caption(f"미반영 {pending}건")
with sb[4]:
    msg = (
        f"후보 {len(st.session_state.candidates)}건 | JIRA {len(st.session_state.jira_issues)}건"
        if st.session_state.candidates or st.session_state.jira_issues
        else "준비 완료 — 파일을 드래그하거나 차종을 선택하세요."
    )
    st.caption(msg)
with sb[5]:
    st.caption(datetime.now().strftime("%H:%M:%S"))
