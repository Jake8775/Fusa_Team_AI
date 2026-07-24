"""
차종 게이트 일정관리 앱 — HMG 사내 게이트웨이 연동
파일 업로드 → AI 추출 → 후보 관리 → Confluence 반영 → 타임라인 → JIRA 조회
"""

import os
os.environ.setdefault("PYTHONUTF8", "1")

import base64
import calendar
import io
import json
import subprocess
import uuid
from datetime import date, datetime, timedelta

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

# 스펙 원문 그대로
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

JIRA_FIELD_KO = {
    "summary": "제목", "status": "상태", "assignee": "담당자",
    "issuetype": "유형", "priority": "우선순위", "description": "설명",
    "reporter": "보고자", "created": "생성일", "updated": "수정일",
    "labels": "레이블", "components": "컴포넌트",
    "duedate": "마감일", "fixVersions": "수정버전",
}


# ── 유틸리티 ──────────────────────────────────────────────────────────────────
def get_git_version() -> str:
    try:
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
        major = int(lines[0])
        base = int(lines[1]) if len(lines) > 1 else 0
        r = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, cwd=root,
        )
        minor = max(0, int(r.stdout.strip()) - base) if r.returncode == 0 else 0
        return f"{major}.{minor:02d}"
    except Exception:
        return "dev"


def read_txt_file(uploaded, session_key: str) -> str:
    """TXT 파일에서 값 읽기. 세션에 캐시."""
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
    """날짜에 n개월 추가"""
    m = d.month - 1 + n
    return d.replace(year=d.year + m // 12, month=m % 12 + 1, day=1)


def detect_mime(raw: bytes) -> str:
    if raw[:4] == b"\x89PNG":   return "image/png"
    if raw[:2] == b"\xff\xd8":  return "image/jpeg"
    if raw[:4] == b"GIF8":      return "image/gif"
    return "image/webp"


def parse_file(uploaded_file) -> str:
    """업로드 파일 → 텍스트"""
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
    """이메일 있으면 Basic Auth, 없으면 Bearer 토큰 (Data Center PAT)"""
    if email:
        return {"auth": HTTPBasicAuth(email, token)}
    return {"headers": {"Authorization": f"Bearer {token}"}}


def confluence_update(email: str, token: str, html_body: str):
    page_url = f"{CONFLUENCE_BASE}/wiki/api/v2/pages/{CONFLUENCE_PAGE_ID}"
    kw       = _atlassian_kwargs(email, token)
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
        "id": page_id, "status": "current", "title": title,
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


def jira_get_fields(email: str, token: str, jira_base: str) -> dict:
    """JIRA 커스텀 필드명 매핑. {id: name}"""
    if st.session_state.get("jira_field_map"):
        return st.session_state.jira_field_map
    kw = _atlassian_kwargs(email, token)
    try:
        r = requests.get(f"{jira_base.rstrip('/')}/rest/api/3/field", timeout=10, **kw)
        if r.ok:
            fmap = {f["id"]: f["name"] for f in r.json()}
            st.session_state.jira_field_map = fmap
            return fmap
    except Exception:
        pass
    return {}


def jira_search(email: str, token: str, jira_base: str, jql: str):
    kw     = _atlassian_kwargs(email, token)
    url    = f"{jira_base.rstrip('/')}/rest/api/3/search"
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
    """JIRA 필드 값 → 표시 문자열"""
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("displayName", "name", "value", "key"):
            if v.get(k):
                return str(v[k])
    if isinstance(v, list):
        return ", ".join(field_val_str(i) for i in v)
    return str(v)


# ── 세션 상태 초기화 ─────────────────────────────────────────────────────────
def _init():
    for k, v in {
        "candidates":     [],
        "relay_req":      None,
        "relay_res":      None,
        "relay_meta":     None,
        "current_month":  date.today().replace(day=1),
        "jira_issues":    [],
        "jira_field_map": {},
        "timeline_car":   None,   # 타임라인 클릭으로 선택된 차종
        "file_cache":     {},
        # TXT 파일 캐시
        "api_key":        "",
        "cf_email":       "",
        "cf_token":       "",
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()


# ── 페이지 설정 ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="차종 게이트 일정관리", page_icon="📅", layout="wide")


# ══════════════════════════════════════════════════════════════════════════════
# 사이드바 — 3개 TXT 파일 입력
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.header("⚙️ 설정")
    st.caption(f"ver. {get_git_version()}")

    exe_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "HMG_Relay.exe")
    if os.path.exists(exe_path):
        with open(exe_path, "rb") as _f:
            st.download_button(
                "⬇️ HMG_Relay.exe 다운로드", _f,
                file_name="HMG_Relay.exe", mime="application/octet-stream",
                use_container_width=True,
            )

    st.divider()
    st.subheader("🔑 인증 파일 (TXT)")
    st.caption("각 항목을 .txt 파일로 드래그하세요")

    f_api   = st.file_uploader("① HMG AI API 키", type=["txt"], key="up_api")
    f_token = st.file_uploader("② Atlassian API 토큰", type=["txt"], key="up_token")

    api_key  = read_txt_file(f_api,   "api_key")
    cf_token = read_txt_file(f_token, "cf_token")

    cf_email = st.text_input(
        "③ Atlassian 이메일",
        value=st.session_state.get("cf_email", ""),
        placeholder="user@hmg.com",
    )
    st.session_state.cf_email = cf_email

    loaded = sum(bool(x) for x in [api_key, cf_token])
    st.progress(loaded / 2, text=f"{loaded}/2 필수 파일 로드")

    st.divider()
    st.subheader("🌐 연결 설정")
    jira_base = st.text_input(
        "JIRA Base URL",
        value=st.session_state.get("jira_base", ""),
        placeholder="https://hmg.atlassian.net",
    )
    if jira_base:
        st.session_state.jira_base = jira_base
    jira_base = st.session_state.get("jira_base", "")

    st.divider()
    st.subheader("🤖 AI 엔진")
    engine = st.selectbox("엔진", list(AI_ENGINES.keys()), index=0)
    model  = st.selectbox("모델", AI_ENGINES[engine])


# ── 연결 상태 플래그 ──────────────────────────────────────────────────────────
has_ai = bool(api_key)
has_cf = bool(cf_token and jira_base)   # 이메일은 선택 (Cloud만 필요)


# ── 헤더 ─────────────────────────────────────────────────────────────────────
h1, h2, h3 = st.columns([4, 1, 1])
with h1:
    st.title("📅 차종 게이트 일정관리")
with h2:
    st.metric("Confluence", "🟢 연결됨" if has_cf else "🔴 미설정")
with h3:
    st.metric("JIRA",       "🟢 연결됨" if has_cf else "🔴 미설정")


# ══════════════════════════════════════════════════════════════════════════════
# 메인 레이아웃
# ══════════════════════════════════════════════════════════════════════════════
left_col, right_col = st.columns([45, 55])


# ─────────────────────────────────────────────────────────────────────────────
# 왼쪽 패널
# ─────────────────────────────────────────────────────────────────────────────
with left_col:
    st.subheader("📂 파일 업로드")

    uploaded_files = st.file_uploader(
        "파일 선택 (복수 가능)",
        type=["xlsx", "xls", "pdf", "jpg", "jpeg", "png", "gif", "webp", "txt", "msg"],
        accept_multiple_files=True,
        label_visibility="collapsed",
    )

    # 파일 캐시 갱신
    if uploaded_files:
        cur_names = {uf.name for uf in uploaded_files}
        for name in list(st.session_state.file_cache.keys()):
            if name not in cur_names:
                del st.session_state.file_cache[name]
        for uf in uploaded_files:
            if uf.name not in st.session_state.file_cache:
                st.session_state.file_cache[uf.name] = parse_file(uf)
        st.caption(f"{len(st.session_state.file_cache)}개 파일 로드됨")
        for fname in st.session_state.file_cache:
            st.text(f"  • {fname}")
    else:
        st.session_state.file_cache = {}

    extract_btn = st.button(
        "🤖 AI 일정 추출", type="primary", use_container_width=True,
        disabled=not (st.session_state.file_cache and has_ai),
    )

    if extract_btn:
        combined = "\n\n".join(
            f"[{n}]\n{t}" for n, t in st.session_state.file_cache.items()
        )
        st.session_state.relay_req = {
            "request_id":    str(uuid.uuid4()),
            "engine":        engine,
            "api_key":       api_key,
            "model":         model,
            "system_prompt": "당신은 일정 정보를 추출하는 전문가입니다. JSON 배열로만 답변하세요.",
            "user_message":  EXTRACT_PROMPT.format(text=combined[:8000]),
        }
        st.session_state.relay_meta = {"files": list(st.session_state.file_cache.keys())}
        st.session_state.relay_res  = None

    # 릴레이 컴포넌트 (비동기)
    req = st.session_state.relay_req
    if req:
        comp = relay_call(**req)
        if comp is not None and comp.get("request_id") == req["request_id"]:
            st.session_state.relay_res = comp
            st.session_state.relay_req = None
            st.rerun()

    if st.session_state.relay_req:
        st.info("🔄 AI 추출 중...")

    if st.session_state.relay_res:
        res = st.session_state.relay_res
        st.session_state.relay_res = None
        if res.get("error"):
            st.error(f"오류: {res['error']}")
        else:
            extracted = parse_ai_response(res["result"])
            if extracted:
                existing = {(c["car_type"], c["event"]) for c in st.session_state.candidates}
                added = 0
                for nc in extracted:
                    nc.setdefault("confirmed", False)
                    nc.setdefault("source", "")
                    if (nc.get("car_type", ""), nc.get("event", "")) not in existing:
                        st.session_state.candidates.append(nc)
                        added += 1
                st.success(f"{added}개 일정 추출 (총 {len(st.session_state.candidates)}건)")
            else:
                st.warning("일정을 찾지 못했습니다.")

    st.divider()
    st.subheader("📋 일정 후보 편집")

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
            pd.DataFrame(rows),
            use_container_width=True,
            num_rows="dynamic",
            column_config={
                "확정":   st.column_config.CheckboxColumn("확정"),
                "이벤트": st.column_config.SelectboxColumn(
                    "이벤트", options=["GATE-3", "GATE-4", "GATE-5", "P2", "LP2"]
                ),
            },
            key="candidates_editor",
        )

        # 편집 반영
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

        ca, cb = st.columns(2)
        with ca:
            if st.button("🗑️ 전체 초기화", use_container_width=True):
                st.session_state.candidates = []
                st.session_state.file_cache = {}
                st.rerun()
        with cb:
            cf_btn = st.button(
                f"☁️ Confluence 반영 ({confirmed_count}건)",
                type="primary", use_container_width=True,
                disabled=not (confirmed_count and has_cf),
            )

        if cf_btn:
            html = candidates_to_html(st.session_state.candidates)
            ok, msg = confluence_update(cf_email, cf_token, html)
            (st.success if ok else st.error)(msg)
    else:
        st.caption("파일을 업로드하고 AI 추출을 실행하세요.")


# ─────────────────────────────────────────────────────────────────────────────
# 오른쪽 패널 — 탭
# ─────────────────────────────────────────────────────────────────────────────
with right_col:
    tab1, tab2, tab3 = st.tabs(["📊 타임라인", "📆 월별 이벤트", "🎫 JIRA 티켓"])

    # ── Tab1: 타임라인 (2개월 범위) ──────────────────────────────────────────
    with tab1:
        nav_l, nav_m, nav_r = st.columns([1, 4, 1])
        cur_m = st.session_state.current_month

        with nav_l:
            if st.button("◀", use_container_width=True, key="prev_m"):
                st.session_state.current_month = add_months(cur_m, -1)
                st.rerun()
        with nav_m:
            st.markdown(
                f"<h3 style='text-align:center;margin:4px 0'>"
                f"◀ {cur_m.strftime('%Y-%m')} ▶</h3>",
                unsafe_allow_html=True,
            )
        with nav_r:
            if st.button("▶", use_container_width=True, key="next_m"):
                st.session_state.current_month = add_months(cur_m, 1)
                st.rerun()

        if not PLOTLY_OK:
            st.warning("`pip install plotly`를 실행하세요.")
        elif not st.session_state.candidates:
            st.info("일정 후보를 추가하면 타임라인이 표시됩니다.")
        else:
            # 2개월 범위
            next_m     = add_months(cur_m, 1)
            _, last1   = calendar.monthrange(cur_m.year, cur_m.month)
            _, last2   = calendar.monthrange(next_m.year, next_m.month)
            range_end  = next_m.replace(day=last2)

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
                    # 2개월 범위 필터
                    if cur_m <= dt <= range_end:
                        tl_rows.append({
                            "차종_이벤트": f"{c.get('car_type','')} {c.get('event','')}",
                            "일정구분":  DATE_LABELS[dk],
                            "시작": dval,
                            "종료": dval,
                        })

            if tl_rows:
                df_tl = pd.DataFrame(tl_rows)
                df_tl["시작"] = pd.to_datetime(df_tl["시작"])
                df_tl["종료"] = pd.to_datetime(df_tl["종료"]) + pd.Timedelta(days=1)
                color_map = {v: COLOR_MAP[k] for k, v in DATE_LABELS.items()}

                fig = px.timeline(
                    df_tl, x_start="시작", x_end="종료",
                    y="차종_이벤트", color="일정구분",
                    color_discrete_map=color_map,
                    title=f"{cur_m.strftime('%Y-%m')} ~ {next_m.strftime('%Y-%m')} 게이트 일정",
                )
                fig.update_layout(height=420, margin=dict(l=10, r=10, t=40, b=10))
                fig.update_xaxes(
                    range=[str(cur_m), str(range_end)],
                    tickformat="%m/%d",
                )

                # 차종 클릭 → JIRA 연동 (Streamlit >= 1.36)
                st.caption("💡 막대 클릭 → JIRA 탭 자동 조회")
                try:
                    event = st.plotly_chart(
                        fig, use_container_width=True,
                        on_select="rerun", key="timeline_chart",
                    )
                    if (event and hasattr(event, "selection")
                            and event.selection and event.selection.points):
                        pt    = event.selection.points[0]
                        y_val = str(pt.get("y") or pt.get("label") or "")
                        car   = y_val.split()[0] if y_val else ""
                        if car:
                            st.session_state.timeline_car = car
                            st.toast(f"'{car}' 선택 → JIRA 탭 조회 중...")
                except TypeError:
                    # on_select 미지원 버전
                    st.plotly_chart(fig, use_container_width=True)
            else:
                st.info(f"{cur_m.strftime('%Y-%m')} ~ {next_m.strftime('%Y-%m')} 기간에 일정이 없습니다.")

    # ── Tab2: 월별 이벤트 목록 ───────────────────────────────────────────────
    with tab2:
        cur_m = st.session_state.current_month
        next_m = add_months(cur_m, 1)
        st.caption(f"{cur_m.strftime('%Y-%m')} ~ {next_m.strftime('%Y-%m')} 이벤트 목록")

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
                        "담당자": c.get("owner", ""),
                        "확정":   "✅" if c.get("confirmed") else "⬜",
                    })

        if ev_rows:
            st.dataframe(
                pd.DataFrame(ev_rows).sort_values("날짜").reset_index(drop=True),
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("해당 기간에 이벤트가 없습니다.")

    # ── Tab3: JIRA 티켓 ──────────────────────────────────────────────────────
    with tab3:
        st.subheader("JIRA 티켓 조회")

        # 차종 목록 (candidates에서 추출)
        car_types = sorted({
            c["car_type"] for c in st.session_state.candidates
            if c.get("car_type")
        })
        car_options = ["전체"] + car_types

        # 타임라인 클릭으로 자동 선택된 차종
        auto_car = st.session_state.pop("timeline_car", None) if "timeline_car" in st.session_state else None

        jc1, jc2 = st.columns([3, 1])
        with jc1:
            default_idx = car_options.index(auto_car) if auto_car and auto_car in car_options else 0
            selected_car = st.selectbox(
                "차종 선택",
                car_options,
                index=default_idx,
                label_visibility="collapsed",
            )
        with jc2:
            jira_btn = st.button("🔍 조회", type="primary", use_container_width=True)

        # 타임라인 클릭 시 자동 조회
        auto_run = bool(auto_car and auto_car in car_options and has_cf)
        if jira_btn or auto_run:
            if not has_cf:
                st.warning("사이드바에서 인증 파일(이메일·토큰)과 Base URL을 설정하세요.")
            else:
                jql = "project=ADE ORDER BY updated DESC"
                if selected_car != "전체":
                    jql = f'project=ADE AND text~"{selected_car}" ORDER BY updated DESC'
                with st.spinner("JIRA 조회 중..."):
                    # 필드명 사전 먼저 로드
                    field_map = jira_get_fields(cf_email, cf_token, jira_base)
                    issues, err = jira_search(cf_email, cf_token, jira_base, jql)
                st.session_state.jira_issues = issues
                if err:
                    st.error(err)
                elif issues:
                    st.success(f"{len(issues)}건 조회됨")
                    if auto_run:
                        st.info("JIRA 탭을 클릭하여 결과를 확인하세요.")
                else:
                    st.info("검색 결과가 없습니다.")

        if st.session_state.jira_issues:
            field_map = st.session_state.get("jira_field_map", {})

            # 요약 테이블
            summary_rows = []
            for issue in st.session_state.jira_issues:
                f = issue.get("fields", {})
                summary_rows.append({
                    "Key":    issue.get("key", ""),
                    "제목":   f.get("summary", ""),
                    "상태":   field_val_str(f.get("status")),
                    "담당자": field_val_str(f.get("assignee")),
                    "유형":   field_val_str(f.get("issuetype")),
                })
            st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

            # 티켓 상세
            st.caption("▼ 티켓 상세 (클릭하여 펼치기)")
            for issue in st.session_state.jira_issues:
                f   = issue.get("fields", {}) or {}
                key = issue.get("key", "")

                with st.expander(f"{key}: {f.get('summary', '')}"):
                    # 표준 필드
                    std_labels = {
                        "status": "상태", "issuetype": "유형", "priority": "우선순위",
                        "assignee": "담당자", "reporter": "보고자",
                        "created": "생성일", "updated": "수정일", "duedate": "마감일",
                    }
                    sc1, sc2 = st.columns(2)
                    for i, (fid, label) in enumerate(std_labels.items()):
                        val = field_val_str(f.get(fid))
                        if val:
                            (sc1 if i % 2 == 0 else sc2).markdown(f"**{label}:** {val}")

                    # 커스텀 필드 (단계, 점검유형, 시스템 등)
                    custom_items = []
                    for fid, fval in f.items():
                        if not fid.startswith("customfield_") or fval is None:
                            continue
                        fname = field_map.get(fid, fid)
                        vstr  = field_val_str(fval)
                        if vstr:
                            custom_items.append((fname, vstr))

                    if custom_items:
                        st.markdown("---")
                        st.markdown("**커스텀 필드**")
                        cc1, cc2 = st.columns(2)
                        for i, (fname, vstr) in enumerate(custom_items):
                            (cc1 if i % 2 == 0 else cc2).markdown(f"**{fname}:** {vstr}")

                    # 설명
                    desc = f.get("description")
                    if desc:
                        st.markdown("---")
                        st.markdown("**설명**")
                        desc_text = desc if isinstance(desc, str) else json.dumps(desc, ensure_ascii=False)
                        st.text(desc_text[:800])

                    st.markdown("---")
                    # 파일 첨부 — 드롭 허용, 저장만 disable
                    st.markdown("**📎 Attachment**")
                    attached = st.file_uploader(
                        "파일을 드래그하세요",
                        key=f"attach_{key}",
                        accept_multiple_files=True,
                    )
                    st.button(
                        "저장",
                        key=f"save_{key}",
                        disabled=True,
                        use_container_width=False,
                    )
                    # 스펙 원문 제한 메시지
                    st.warning(f"⚠ 저장 기능 제한 안내\n\n{SAVE_LIMIT_MSG}")
        else:
            st.caption("차종을 선택하고 조회 버튼을 클릭하세요.")
