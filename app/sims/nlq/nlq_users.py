# app/sims/nlq/nlq_users.py
# 사용자
# 2026/06/17


from __future__ import annotations

from typing import Any, Dict, Optional, Callable
import re
import uuid

import pandas as pd

_Q_DEPT_COUNT = re.compile(
    r"(부서별\s*사용자\s*수|부서별\s*사용자수|부서별\s*인원\s*수|부서별\s*인원수|부서\s*인원\s*수|부서\s*인원수)",
    re.IGNORECASE,
)

_TAIL_JOSA_RE = re.compile(r"(이|가|을|를|은|는|의|도|만|과|와)$")
_QUOTE_CHARS = "\"'“”‘’"

_USER_MASTER_ATTR_WORDS = (
    "사용자",
    "사용자목록",
    "사용자 목록",
    "사용자코드",
    "사용자명",
    "사용자ID",
    "사용자 아이디",
    "아이디",
    "사번",
    "부서명",
    "부서",
    "직책",
    "영업지역",
    "재고위치",
    "수정자",
    "수정자명",
    "수정일자"

)

_USER_LIST_VERBS = (
    "조회", "검색", "목록", "찾아", "찾아줘", "찾아봐", "보여줘", "알려줘",
)

_USER_MASTER_STOP = {
    "조회", "검색", "찾아", "찾아줘", "찾아봐", "보여줘", "알려줘",
    "목록", "사용자", "사용자목록", "사용자목록+", "부서명",
    "직책", "영업지역", "재고위치", "사용자명", "사용자ID", "사번",
    "인", "수정", "수정한", "수정자", "수정자명", "수정일자",
}


def _ensure_df(obj: Any) -> pd.DataFrame:
    if obj is None:
        return pd.DataFrame()
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    try:
        return pd.DataFrame(obj)
    except Exception:
        return pd.DataFrame()


def _norm_kw(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", "", s)
    return s


def _strip_wrapping_quotes(s: str) -> str:
    s = (s or "").strip()
    while len(s) >= 2 and (s[0] in _QUOTE_CHARS) and (s[-1] in _QUOTE_CHARS):
        s = s[1:-1].strip()
    s = s.replace("“", "").replace("”", "").replace("‘", "").replace("’", "")
    return s.strip()


def _strip_tail_request_words(s: str) -> str:
    s = (s or "").strip()
    s = _strip_wrapping_quotes(s)
    for _ in range(4):
        s2 = re.sub(r"(?:검색|조회|찾아줘|찾아봐줘|찾아봐|찾아|보여줘|알려줘|해줘)$", "", s).strip()
        if s2 == s:
            break
        s = s2
    return s


def _strip_tail_josa(s: str) -> str:
    s = _strip_wrapping_quotes((s or "").strip())
    if len(s) >= 2:
        s = _TAIL_JOSA_RE.sub("", s)
    return s.strip()


def _clean_master_token(s: str) -> str:
    s = _strip_tail_request_words(_strip_tail_josa(s or ""))
    s = _norm_kw(s)
    if not s or s in _USER_MASTER_STOP:
        return ""
    return s


def _extract_quoted_or_token(txt: str, label_patterns: str) -> str | None:
    m = re.search(
        rf"{label_patterns}\s*(?:에|이|가|은|는)?\s*([^\s,?.!]+?)(?:이|가|을|를|은|는)?\s*(?:포함|들어간|있는|인|같은)",
        txt,
    )
    if m:
        v = _clean_master_token(m.group(1) or "")
        return v or None

    m = re.search(
        rf"{label_patterns}\s*(?:에|이|가|은|는)?\s*([^\s,?.!]+?)(?:이|가|을|를|은|는)?\s*(?:(?:사용자(?:명|코드)?|사용자목록|사용자\s*목록)\s*)?(?:조회|검색|찾아|찾아줘|찾아봐|보여줘|알려줘)?\s*$",
        txt,
    )
    if m:
        v = _clean_master_token(m.group(1) or "")
        return v or None

    return None


def _has_user_master_anchor(txt: str) -> bool:
    t = (txt or "").strip()
    return any(k in t for k in _USER_MASTER_ATTR_WORDS)


def _has_user_list_intent(txt: str) -> bool:
    t = (txt or "").strip()
    return any(k in t for k in _USER_LIST_VERBS)


def _pick_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _resolve_code_from_options(df: pd.DataFrame, keyword: str) -> tuple[str, str]:
    work = _ensure_df(df)
    if work.empty:
        return "", ""

    code_col = _pick_col(work, ["Rd01_Tcode", "항목코드", "상세코드"])
    name_col = _pick_col(work, ["Rd01_Hnm", "한글명", "코드명"])
    if not code_col or not name_col:
        return "", ""

    kw = _norm_kw(keyword)
    if not kw:
        return "", ""

    for _, row in work.iterrows():
        nm = _norm_kw(str(row[name_col] or ""))
        if nm == kw:
            return str(row[code_col] or "").strip(), str(row[name_col] or "").strip()

    for _, row in work.iterrows():
        nm = _norm_kw(str(row[name_col] or ""))
        if kw and kw in nm:
            return str(row[code_col] or "").strip(), str(row[name_col] or "").strip()

    return "", ""


def _parse_dept_count_from_text(txt: str) -> Optional[pd.DataFrame]:
    if not txt:
        return None
    if "부서별" not in txt or "인원" not in txt:
        return None

    m = re.search(r"부서명\s*,\s*인원수\s*\n(.+)", txt, flags=re.DOTALL)
    if not m:
        return None
    tail = m.group(1)

    rows = []
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            break
        if "," not in line:
            continue
        a, b = line.split(",", 1)
        dept = a.strip()
        num = re.sub(r"[^\d]", "", b)
        if dept and num.isdigit():
            rows.append((dept, int(num)))

    if not rows:
        return None

    return pd.DataFrame(rows, columns=["부서명", "인원수"])


def _compute_dept_count_from_records(data: Dict[str, Any]) -> Optional[pd.DataFrame]:
    if not isinstance(data, dict):
        return None
    cols = data.get("columns") or []
    recs = data.get("records") or []
    if not isinstance(recs, list) or not recs:
        return None

    dept_col = None
    for c in ["부서명", "DeptName", "department_name", "부서"]:
        if c in cols:
            dept_col = c
            break
    if not dept_col:
        return None

    if len(recs) < 100:
        return None

    df = pd.DataFrame(recs, columns=cols)
    if dept_col not in df.columns:
        return None

    out = (
        df[dept_col]
        .astype(str)
        .replace({"": None, "nan": None})
        .dropna()
        .value_counts()
        .reset_index()
    )
    out.columns = ["부서명", "인원수"]
    return out


def _compute_dept_count_from_db(logger, top: int = 5000) -> pd.DataFrame:
    from app.services import rddbc060_service as U

    df = U.list_users_full(top=top)
    dept_col = None
    for c in ["부서명", "DeptName", "department_name", "Rd06_Department"]:
        if c in df.columns:
            dept_col = c
            break
    if not dept_col:
        raise ValueError(f"부서 컬럼을 찾지 못했습니다. columns={list(df.columns)[:30]}")

    out = (
        df[dept_col]
        .astype(str)
        .str.strip()
        .replace({"": None, "nan": None})
        .dropna()
        .value_counts()
        .reset_index()
    )
    out.columns = ["부서명", "인원수"]
    return out


def _build_user_query_summary(
    *,
    user_cd: str = "",
    user_id: str = "",
    sabun: str = "",
    user_nm: str = "",
    dept_nm: str = "",
    duty_nm: str = "",
    district_nm: str = "",
    stock_nm: str = "",
    mod_user_nm: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",
    keyword: str = "",
) -> str:
    parts = []
    if user_cd:
        parts.append(f"사용자코드 {user_cd}")
    if user_id:
        parts.append(f"사용자ID {user_id}")
    if sabun:
        parts.append(f"사번 {sabun}")
    if user_nm:
        parts.append(f"사용자명 {user_nm}")
    if dept_nm:
        parts.append(f"부서명 {dept_nm}")
    if duty_nm:
        parts.append(f"직책 {duty_nm}")
    if district_nm:
        parts.append(f"영업지역 {district_nm}")
    if stock_nm:
        parts.append(f"재고위치 {stock_nm}")
    if mod_user_nm:
        parts.append(f"수정자 {mod_user_nm}")
    if mod_date_from or mod_date_to:
        if mod_date_from and mod_date_from == mod_date_to:
            parts.append(f"수정일자 {mod_date_from}")
        else:
            parts.append(f"수정일자 {mod_date_from or ''}~{mod_date_to or ''}")
    if keyword:
        parts.append(f"통합검색 {keyword}")
    return " / ".join(parts)

def _user_summary_line(query_summary: str) -> str:
    qs = str(query_summary or "").strip()
    return f"조회조건: {qs}" if qs else "조회조건: 전체"

def _push_result_payload(
    *,
    room: Dict[str, Any],
    session_state: Dict[str, Any],
    result: Dict[str, Any],
    action: str,
    logger,
) -> bool:
    try:
        from app.ui.chat_middleware import push_sims_result_to_chat
        push_sims_result_to_chat(result, action)
        session_state["__scroll_to_msg"] = (
            session_state.get("__sims_last_msg_id") or session_state.get("__scroll_to_msg")
        )
        return True
    except Exception:
        logger.exception("[nlq.users] chat_middleware push failed")

    try:
        from app.ui.chat_bridge import push_sims_result_to_chat
        push_sims_result_to_chat(result, action)
        session_state["__scroll_to_msg"] = (
            session_state.get("__sims_last_msg_id") or session_state.get("__scroll_to_msg")
        )
        return True
    except Exception:
        logger.exception("[nlq.users] chat_bridge push failed")

    room.setdefault("messages", []).append({
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": str(result.get("message") or result.get("data") or action),
    })
    return True


def _push_user_text(
    *,
    room: Dict[str, Any],
    session_state: Dict[str, Any],
    txt: str,
    message: str,
    query_summary: str,
    params: Dict[str, Any],
    logger,
) -> bool:
    summary_line = _user_summary_line(query_summary)
    display_message = f"{message}\n\n{summary_line}"

    result = {
        "final": True,
        "type": "text",
        "title": "사용자목록 + 부서명",
        "action": "사용자목록 + 부서명",
        "params": params,
        "data": display_message,
        "message": display_message,
        "meta": {
            "nlq": True,
            "master_nlq": True,
            "domain": "users",
            "source": "사용자마스터(Rddbc060)",
            "nlq_query": txt,
            "_force_push": True,
            "_nlq_nonce": str(uuid.uuid4()),
            "row_count": 0,
            "row_count_total": 0,
            "query_summary": query_summary,
            "summary_md": summary_line,
        },
    }
    return _push_result_payload(
        room=room,
        session_state=session_state,
        result=result,
        action="사용자목록 + 부서명",
        logger=logger,
    )

def _push_table_result(
    *,
    room: Dict[str, Any],
    session_state: Dict[str, Any],
    df: pd.DataFrame,
    df_display: pd.DataFrame,
    title: str,
    action: str,
    txt: str,
    params: Dict[str, Any],
    query_summary: str,
    logger,
) -> bool:
    df_full = _ensure_df(df)
    df_display_all = _ensure_df(df_display)

    total = int(len(df_full)) if not df_full.empty else int(len(df_display_all))
    show_n = min(total, 500)
    df_show = df_display_all.head(show_n).copy()

    condition_text = str(query_summary or "").strip() or "전체"
    note = (
        f"조회결과: **{total:,}건** (전부 표시)"
        if show_n >= total
        else f"조회결과: **{total:,}건** (표시는 상위 {show_n:,}건)"
    )

    extra_meta: Dict[str, Any] = {}

    if action == "사용자목록 + 부서명":
        try:
            from app.sims.views import users as users_view

            params2 = dict(params or {})
            params2.setdefault("등록일자From", params2.get("등록일자From", ""))
            params2.setdefault("등록일자To", params2.get("등록일자To", ""))
            params2.setdefault("수정일자From", params2.get("수정일자From", ""))
            params2.setdefault("수정일자To", params2.get("수정일자To", ""))

            query_condition = users_view._build_users_query_condition(
                params2,
                total,
                show_n,
            )
            condition_text, note2 = users_view._split_condition_and_note(query_condition)
            if note2:
                note = note2

            users_master_summary = users_view._build_users_master_llm_summary(
                df_display_all,
                query_condition=query_condition,
                total=total,
                display_count=show_n,
            )
            llm_summary_md = str(users_master_summary.get("llm_summary_md") or "")

            extra_meta.update({
                "analysis_type": "users_master",
                "llm_summary_kind": "users_master_summary",
                "llm_summary_md": llm_summary_md,
                "users_master_summary": users_master_summary,
                "analysis_row_count": total,
                "row_count_total_for_analysis": total,
                "summary_basis": "전체 조회결과 기준",
                "field_notes": (
                    "사용자 마스터 분석은 전체 조회결과 기준 집계요약을 우선 근거로 답합니다. "
                    "화면 표시는 일부 행으로 제한될 수 있습니다."
                ),
            })
        except Exception:
            logger.exception("[nlq.users] users master summary build failed")

    result = {
        "final": True,
        "type": "table",
        "title": title,
        "action": action,
        "params": params,
        "columns": list(df_show.columns),
        "df": df_full if not df_full.empty else df_show,
        "df_display": df_show,
        "records": df_show.to_dict(orient="records"),
        "message": f"{title} {total:,}건",
        "meta": {
            "nlq": True,
            "master_nlq": True,
            "domain": "users",
            "source": "사용자마스터(Rddbc060)",
            "nlq_query": txt,
            "_force_push": True,
            "_nlq_nonce": str(uuid.uuid4()),
            "row_count": total,
            "row_count_total": total,
            "display_row_count": show_n,
            "show_n": show_n,
            "condition": condition_text,
            "query_summary": condition_text,
            "summary_md": note,
            "note": note,
            **extra_meta,
        },
    }

    return _push_result_payload(
        room=room,
        session_state=session_state,
        result=result,
        action=action,
        logger=logger,
    )

def _extract_user_cd(txt: str) -> str | None:
    if "사용자코드" not in txt:
        return None
    return _extract_quoted_or_token(txt, r"(?:사용자코드)")


def _extract_user_id(txt: str) -> str | None:
    if not any(k in txt for k in ("사용자ID", "사용자 아이디", "아이디")):
        return None
    return _extract_quoted_or_token(txt, r"(?:사용자ID|사용자\s*아이디|아이디)")


def _extract_sabun(txt: str) -> str | None:
    if "사번" not in txt:
        return None
    return _extract_quoted_or_token(txt, r"(?:사번)")


def _extract_user_name(txt: str) -> str | None:
    if "사용자명" in txt:
        return _extract_quoted_or_token(txt, r"(?:사용자명)")

    if "사용자" not in txt:
        return None

    # 구조화 속성 질의면 사용자명으로 재해석하지 않음
    if any(k in txt for k in (
        "사용자코드",
        "사용자ID",
        "사용자 아이디",
        "아이디",
        "사번",
        "부서명",
        "부서",
        "직책",
        "영업지역",
        "재고위치",
        "수정자",
        "수정자명",
        "수정일자",
    )):
        return None

    # "관리자가 수정한 사용자 조회" 같은 문장은 사용자명이 아님
    if re.search(r"[^\s,?.!]+(?:이|가)?\s*수정한\s*사용자", txt):
        return None

    # "<키워드> 사용자"
    m = re.search(r"([^\s,?.!]+)\s+사용자(?:\s*목록|\s*조회|\s*검색|\s*찾아줘|\s*보여줘)?", txt)
    if m:
        v = _clean_master_token(m.group(1) or "")
        return v or None

    # "사용자 <키워드>"
    m = re.search(r"사용자\s+([^\s,?.!]+)", txt)
    if m:
        v = _clean_master_token(m.group(1) or "")
        return v or None

    return None

def _extract_dept_name(txt: str) -> str | None:
    if not any(k in txt for k in ("부서명", "부서")):
        return None
    return _extract_quoted_or_token(txt, r"(?:부서명|부서)")


def _extract_duty_name(txt: str) -> str | None:
    if "직책" not in txt:
        return None
    return _extract_quoted_or_token(txt, r"(?:직책)")


def _extract_district_name(txt: str) -> str | None:
    if "영업지역" not in txt:
        return None
    return _extract_quoted_or_token(txt, r"(?:영업지역)")


def _extract_stock_name(txt: str) -> str | None:
    if "재고위치" not in txt:
        return None
    return _extract_quoted_or_token(txt, r"(?:재고위치)")

def _extract_mod_user_name(txt: str) -> str | None:
    # 1) 명시형: "수정자 관리자 사용자 조회"
    if any(k in txt for k in ("수정자", "수정자명")):
        v = _extract_quoted_or_token(txt, r"(?:수정자명|수정자)")
        if v:
            return v

    # 2) 자연형: "관리자가 수정한 사용자 조회"
    m = re.search(r"([^\s,?.!]+?)(?:이|가)?\s*수정한\s*사용자", txt)
    if m:
        v = _clean_master_token(m.group(1) or "")
        return v or None

    return None

def _date_token_digits(token: str) -> str:
    s = str(token or "").strip()
    return re.sub(r"\D", "", s)


def _month_end_yyyymmdd(ym: str) -> str:
    """
    ym: YYYYMM
    return: YYYYMMDD month-end
    """
    ym = _date_token_digits(ym)
    if len(ym) != 6:
        return ""

    try:
        p = pd.Period(ym, freq="M")
        return p.end_time.strftime("%Y%m%d")
    except Exception:
        # fallback
        y = ym[:4]
        m = ym[4:6]
        if m in {"01", "03", "05", "07", "08", "10", "12"}:
            return f"{ym}31"
        if m in {"04", "06", "09", "11"}:
            return f"{ym}30"
        if m == "02":
            yy = int(y)
            leap = (yy % 4 == 0 and yy % 100 != 0) or (yy % 400 == 0)
            return f"{ym}{'29' if leap else '28'}"
    return ""


def _date_token_bounds(token: str) -> tuple[str, str]:
    """
    2025       -> 20250101 ~ 20251231
    202501     -> 20250101 ~ 20250131
    20250101   -> 20250101 ~ 20250101
    2025-01-01 -> 20250101 ~ 20250101
    """
    d = _date_token_digits(token)

    if len(d) == 4:
        return f"{d}0101", f"{d}1231"

    if len(d) == 6:
        return f"{d}01", _month_end_yyyymmdd(d)

    if len(d) >= 8:
        x = d[:8]
        return x, x

    return "", ""


def _expand_date_range_token(a: str, b: str = "") -> tuple[str, str]:
    a_from, a_to = _date_token_bounds(a)
    if not b:
        return a_from, a_to

    b_from, b_to = _date_token_bounds(b)
    if a_from and b_to:
        return a_from, b_to

    return a_from, a_to


def _extract_mod_date_range(txt: str) -> tuple[str, str]:
    cleaned = str(txt or "")

    if not any(k in cleaned for k in ("수정일자", "수정일", "수정", "변경일자", "변경일", "변경")):
        return "", ""

    m = re.search(
        r"(?:수정일자|수정일|변경일자|변경일)\s*"
        r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)"
        r"\s*(?:~|부터|에서|\-)\s*"
        r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)",
        cleaned,
    )
    if m:
        return _expand_date_range_token(m.group(1), m.group(2))

    m = re.search(
        r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)"
        r"\s*(?:~|부터|에서|\-)\s*"
        r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)"
        r"\s*(?:수정|수정된|수정한|변경|변경된|변경한)",
        cleaned,
    )
    if m:
        return _expand_date_range_token(m.group(1), m.group(2))

    m = re.search(
        r"(?:수정일자|수정일|변경일자|변경일)\s*"
        r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)",
        cleaned,
    )
    if m:
        return _expand_date_range_token(m.group(1))

    m = re.search(
        r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)"
        r"\s*(?:수정|수정된|수정한|변경|변경된|변경한)",
        cleaned,
    )
    if m:
        return _expand_date_range_token(m.group(1))

    return "", ""

def try_handle_users_nlq(
    txt: str,
    *,
    room: Dict[str, Any],
    session_state: Dict[str, Any],
    make_ts: Callable[[], str],
    next_seq: Callable[[], int],
    logger,
) -> bool:
    t = (txt or "").strip()
    if not t:
        return False

    def _extract_user_name_local(text: str) -> str | None:
        if "사용자명" in text:
            return _extract_quoted_or_token(text, r"(?:사용자명)")

        if "사용자" not in text:
            return None

        if any(k in text for k in (
            "사용자코드",
            "사용자ID",
            "사용자 아이디",
            "아이디",
            "사번",
            "부서명",
            "부서",
            "직책",
            "영업지역",
            "재고위치",
            "등록자",
            "등록자명",
            "등록일자",
            "수정자",
            "수정자명",
            "수정일자",
            "최근 입사자",
            "최근입사자",
        )):
            return None

        if re.search(r"[^\s,?.!]+(?:이|가)?\s*(?:등록한|수정한)\s*사용자", text):
            return None

        m = re.search(r"([^\s,?.!]+)\s+사용자(?:\s*목록|\s*조회|\s*검색|\s*찾아줘|\s*보여줘)?", text)
        if m:
            v = _clean_master_token(m.group(1) or "")
            return v or None

        m = re.search(r"사용자\s+([^\s,?.!]+)", text)
        if m:
            v = _clean_master_token(m.group(1) or "")
            return v or None

        return None

    def _extract_add_user_name_local(text: str) -> str | None:
        if any(k in text for k in ("등록자", "등록자명")):
            v = _extract_quoted_or_token(text, r"(?:등록자명|등록자)")
            if v:
                return v

        m = re.search(r"([^\s,?.!]+?)(?:이|가)?\s*등록한\s*사용자", text)
        if m:
            v = _clean_master_token(m.group(1) or "")
            return v or None

        return None

    def _extract_add_date_range_local(text: str) -> tuple[str, str]:
        cleaned = str(text or "")

        if not any(k in cleaned for k in ("등록일자", "등록일", "등록", "작성일자", "작성일", "작성")):
            return "", ""

        m = re.search(
            r"(?:등록일자|등록일|작성일자|작성일)\s*"
            r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)"
            r"\s*(?:~|부터|에서|\-)\s*"
            r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)",
            cleaned,
        )
        if m:
            return _expand_date_range_token(m.group(1), m.group(2))

        m = re.search(
            r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)"
            r"\s*(?:~|부터|에서|\-)\s*"
            r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)"
            r"\s*(?:등록|등록된|작성|작성된)",
            cleaned,
        )
        if m:
            return _expand_date_range_token(m.group(1), m.group(2))

        m = re.search(
            r"(?:등록일자|등록일|작성일자|작성일)\s*"
            r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)",
            cleaned,
        )
        if m:
            return _expand_date_range_token(m.group(1))

        m = re.search(
            r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)"
            r"\s*(?:등록|등록된|작성|작성된)",
            cleaned,
        )
        if m:
            return _expand_date_range_token(m.group(1))

        return "", ""

    def _is_recent_hires_intent_local(text: str) -> bool:
        s = re.sub(r"\s+", "", str(text or ""))
        return any(
            k in s
            for k in (
                "최근입사자",
                "최근입사자조회",
                "최근입사자보여줘",
                "최근입사자알려줘",
                "최근입사자찾아줘",
            )
        )
    
    def _extract_recent_base_date_local(text: str):
        default_date = (pd.Timestamp.today().normalize() - pd.Timedelta(days=30)).date()

        pats = [
            r"기준일자\s*(20\d{6}|20\d{4})",
            r"(20\d{6}|20\d{4})\s*(?:이후|부터)",
            r"최근\s*입사자\s*(20\d{6}|20\d{4})",
            r"최근입사자\s*(20\d{6}|20\d{4})",
        ]

        for pat in pats:
            m = re.search(pat, text)
            if not m:
                continue
            digits = re.sub(r"[^\d]", "", m.group(1))
            try:
                if len(digits) == 8:
                    return pd.to_datetime(digits, format="%Y%m%d", errors="coerce").date()
                if len(digits) == 6:
                    return pd.to_datetime(digits + "01", format="%Y%m%d", errors="coerce").date()
            except Exception:
                pass

        return default_date

    def _parse_char8_date_series_local(sr: pd.Series) -> pd.Series:
        if sr is None or len(sr) == 0:
            return pd.Series(dtype="datetime64[ns]")

        s = (
            sr.fillna("")
            .astype(str)
            .str.strip()
            .replace(
                {
                    "": None,
                    "0": None,
                    "00000000": None,
                    "19000101": None,
                    "20010101": None,
                    "99999999": None,
                    "None": None,
                    "nan": None,
                    "<NA>": None,
                }
            )
        )
        return pd.to_datetime(s, format="%Y%m%d", errors="coerce")

    def _parse_any_datetime_series_local(sr: pd.Series) -> pd.Series:
        if sr is None or len(sr) == 0:
            return pd.Series(dtype="datetime64[ns]")
        s = sr.fillna("").astype(str).str.strip().replace({"": None, "None": None, "nan": None, "<NA>": None})
        return pd.to_datetime(s, errors="coerce")

    def _format_char8_date_series_local(sr: pd.Series) -> pd.Series:
        dt = _parse_char8_date_series_local(sr)
        out = dt.dt.strftime("%Y-%m-%d")
        return out.fillna("")

    def _apply_name_filter_local(
        df_src: pd.DataFrame,
        df_view: pd.DataFrame,
        col_name: str,
        kw: str,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        if not kw:
            return df_src, df_view

        kw_norm = _norm_kw(kw)
        if not kw_norm:
            return df_src, df_view

        target = df_view if col_name in df_view.columns else df_src
        if col_name not in target.columns:
            return df_src, df_view

        s = target[col_name].astype(str).str.replace(" ", "", regex=False)
        mask = s.str.contains(kw_norm, na=False)
        idx = target.index[mask]
        return df_src.loc[idx].copy(), df_view.loc[idx].copy()

    def _norm_series_local(sr: pd.Series) -> pd.Series:
        return (
            sr.fillna("")
            .astype(str)
            .replace({"None": "", "nan": "", "<NA>": ""})
            .str.strip()
        )

    def _apply_date_filter_local(
        df_src: pd.DataFrame,
        df_view: pd.DataFrame,
        col_name: str,
        date_from: str,
        date_to: str,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """
        등록일자/수정일자 후필터.
        핵심:
        - df_src는 원본 YYYYMMDD일 수도 있고
        - df_view는 YYYY-MM-DD 표시형일 수도 있으므로
        - 반드시 숫자만 추출해서 YYYYMMDD 기준으로 비교한다.
        """
        if not date_from and not date_to:
            return df_src, df_view

        if col_name in df_src.columns:
            target = df_src
        elif col_name in df_view.columns:
            target = df_view
        else:
            return df_src.iloc[0:0].copy(), df_view.iloc[0:0].copy()

        f, t2 = _expand_date_range_token(date_from, date_to)

        if not f and not t2:
            return df_src, df_view

        s = (
            _norm_series_local(target[col_name])
            .str.replace(r"\D", "", regex=True)
            .str[:8]
        )

        mask = pd.Series(True, index=target.index)

        # 날짜 조건이 있는 경우 빈 날짜는 제외
        mask &= s.str.len().ge(8)

        if f:
            mask &= s >= f
        if t2:
            mask &= s <= t2

        idx = target.index[mask.fillna(False)]
        return df_src.loc[idx].copy(), df_view.loc[idx].copy()
    

    def _build_query_summary_local(
        *,
        user_cd: str = "",
        user_id: str = "",
        sabun: str = "",
        user_nm: str = "",
        dept_nm: str = "",
        duty_nm: str = "",
        district_nm: str = "",
        stock_nm: str = "",
        add_user_nm: str = "",
        add_date_from: str = "",
        add_date_to: str = "",
        mod_user_nm: str = "",
        mod_date_from: str = "",
        mod_date_to: str = "",
        keyword: str = "",
        recent_base_date: str = "",
    ) -> str:
        parts = []

        if recent_base_date:
            parts.append(f"최근 입사자 기준일자 {recent_base_date}")
        if user_cd:
            parts.append(f"사용자코드 {user_cd}")
        if user_id:
            parts.append(f"사용자ID {user_id}")
        if sabun:
            parts.append(f"사번 {sabun}")
        if user_nm:
            parts.append(f"사용자명 {user_nm}")
        if dept_nm:
            parts.append(f"부서명 {dept_nm}")
        if duty_nm:
            parts.append(f"직책 {duty_nm}")
        if district_nm:
            parts.append(f"영업지역 {district_nm}")
        if stock_nm:
            parts.append(f"재고위치 {stock_nm}")
        if add_user_nm:
            parts.append(f"등록자 {add_user_nm}")
        if add_date_from or add_date_to:
            if add_date_from and add_date_from == add_date_to:
                parts.append(f"등록일자 {add_date_from}")
            else:
                parts.append(f"등록일자 {add_date_from or ''}~{add_date_to or ''}")
        if mod_user_nm:
            parts.append(f"수정자 {mod_user_nm}")
        if mod_date_from or mod_date_to:
            if mod_date_from and mod_date_from == mod_date_to:
                parts.append(f"수정일자 {mod_date_from}")
            else:
                parts.append(f"수정일자 {mod_date_from or ''}~{mod_date_to or ''}")
        if keyword:
            parts.append(f"통합검색 {keyword}")

        return " / ".join(parts)

    # ------------------------------------------------------------------
    # 1) 부서별 사용자 수
    # ------------------------------------------------------------------
    if _Q_DEPT_COUNT.search(t):
        pack = None
        try:
            from app.ui.chat_bridge import get_sims_context_data
            try:
                pack = get_sims_context_data(max_age_sec=86400)
            except TypeError:
                pack = get_sims_context_data()
        except Exception:
            pack = None

        if isinstance(pack, dict):
            preview = pack.get("preview") or pack.get("summary_text") or ""
            data = pack.get("data") if isinstance(pack.get("data"), dict) else pack
            meta = (data.get("meta") or {}) if isinstance(data, dict) else {}
            meta_summary = meta.get("summary_text") or ""
            txt_blob = "\n".join([str(preview), str(meta_summary)])

            df_a = _parse_dept_count_from_text(txt_blob)
            if df_a is not None and not df_a.empty:
                return _push_table_result(
                    room=room,
                    session_state=session_state,
                    df=df_a,
                    df_display=df_a,
                    title="부서별 사용자 수",
                    action="부서별 사용자 수",
                    txt=txt,
                    params={},
                    query_summary="부서별 사용자 수",
                    logger=logger,
                )

            if isinstance(data, dict):
                df_a2 = _compute_dept_count_from_records(data)
                if df_a2 is not None and not df_a2.empty:
                    return _push_table_result(
                        room=room,
                        session_state=session_state,
                        df=df_a2,
                        df_display=df_a2,
                        title="부서별 사용자 수",
                        action="부서별 사용자 수",
                        txt=txt,
                        params={},
                        query_summary="부서별 사용자 수",
                        logger=logger,
                    )

        try:
            out = _compute_dept_count_from_db(logger, top=5000)
            return _push_table_result(
                room=room,
                session_state=session_state,
                df=out,
                df_display=out,
                title="부서별 사용자 수",
                action="부서별 사용자 수",
                txt=txt,
                params={"TopN": 5000},
                query_summary="부서별 사용자 수",
                logger=logger,
            )
        except Exception:
            logger.exception("[nlq.users] db aggregation failed")
            return False

    # ------------------------------------------------------------------
    # 2) 사용자 마스터 / 최근 입사자
    # ------------------------------------------------------------------
    recent_hires_intent = _is_recent_hires_intent_local(t)
    user_anchor = (
        recent_hires_intent
        or _has_user_master_anchor(t)
        or any(k in t for k in ("등록자", "등록자명", "등록일자"))
        or bool(re.search(r"[^\s,?.!]+(?:이|가)?\s*등록한\s*사용자", t))
    )

    if not user_anchor:
        return False

    user_cd = _extract_user_cd(t) or ""
    user_id = _extract_user_id(t) or ""
    sabun = _extract_sabun(t) or ""
    user_nm = _extract_user_name_local(t) or ""
    dept_nm = _extract_dept_name(t) or ""
    duty_nm = _extract_duty_name(t) or ""
    district_nm = _extract_district_name(t) or ""
    stock_nm = _extract_stock_name(t) or ""
    add_user_nm = _extract_add_user_name_local(t) or ""
    add_date_from, add_date_to = _extract_add_date_range_local(t)
    mod_user_nm = _extract_mod_user_name(t) or ""
    mod_date_from, mod_date_to = _extract_mod_date_range(t)

    if not any([
        user_cd, user_id, sabun, user_nm,
        dept_nm, duty_nm, district_nm, stock_nm,
        add_user_nm, add_date_from, add_date_to,
        mod_user_nm, mod_date_from, mod_date_to,
    ]) and not _has_user_list_intent(t) and not recent_hires_intent:
        return False

    try:
        from app.services import rddbc060_service as U
        from app.sims.views import users as users_view
    except Exception:
        logger.exception("[nlq.users] import failed")
        return False

    try:
        dept_cd, dept_nm_resolved = _resolve_code_from_options(U.list_department_codes(top=500), dept_nm) if dept_nm else ("", "")
        duty_cd, duty_nm_resolved = _resolve_code_from_options(U.list_duty_codes(top=500), duty_nm) if duty_nm else ("", "")
        district_cd, district_nm_resolved = _resolve_code_from_options(U.list_district_codes(top=500), district_nm) if district_nm else ("", "")
        stock_cd, stock_nm_resolved = _resolve_code_from_options(U.list_stock_codes(top=500), stock_nm) if stock_nm else ("", "")

        unresolved_keywords = []
        if dept_nm and not dept_cd:
            unresolved_keywords.append(dept_nm)
        if duty_nm and not duty_cd:
            unresolved_keywords.append(duty_nm)
        if district_nm and not district_cd:
            unresolved_keywords.append(district_nm)
        if stock_nm and not stock_cd:
            unresolved_keywords.append(stock_nm)

        keyword = " ".join(unresolved_keywords).strip()

        top_n = 5000 if any([
            add_user_nm,
            add_date_from,
            add_date_to,
            mod_user_nm,
            mod_date_from,
            mod_date_to,
            recent_hires_intent,
        ]) else 2000

        logger.info(
            "[nlq.users] extracted user_cd=%r user_id=%r sabun=%r user_nm=%r dept=%r duty=%r district=%r stock=%r add_user=%r add_date=%r~%r mod_user=%r mod_date=%r~%r recent=%r",
            user_cd,
            user_id,
            sabun,
            user_nm,
            dept_nm or dept_nm_resolved,
            duty_nm or duty_nm_resolved,
            district_nm or district_nm_resolved,
            stock_nm or stock_nm_resolved,
            add_user_nm,
            add_date_from,
            add_date_to,
            mod_user_nm,
            mod_date_from,
            mod_date_to,
            recent_hires_intent,
        )

        # --------------------------------------------------------------
        # 2-A) 최근 입사자 NLQ
        # --------------------------------------------------------------
        if recent_hires_intent:
            base_date = _extract_recent_base_date_local(t)
            only_active_recent = bool("사용중만" in t)

            df_raw = U.search_users_full(
                top=5000,
                only_active=only_active_recent,
                user_cd=user_cd,
                user_id=user_id,
                sabun=sabun,
                user_nm=user_nm,
                department="",
                duty="",
                district="",
                stock_cd="",
                add_user_nm=add_user_nm,
                add_date_from=add_date_from,
                add_date_to=add_date_to,
                mod_user_nm="",
                mod_date_from="",
                mod_date_to="",
                keyword=keyword,
            )

            df = users_view._prepare_users_df(_ensure_df(df_raw))

            if df.empty:
                return _push_user_text(
                    room=room,
                    session_state=session_state,
                    txt=txt,
                    message="해당 조회조건의 자료가 없습니다.",
                    query_summary=_build_query_summary_local(
                        user_cd=user_cd,
                        user_id=user_id,
                        sabun=sabun,
                        user_nm=user_nm,
                        dept_nm=dept_nm_resolved or dept_nm,
                        duty_nm=duty_nm_resolved or duty_nm,
                        district_nm=district_nm_resolved or district_nm,
                        stock_nm=stock_nm_resolved or stock_nm,
                        add_user_nm=add_user_nm,
                        add_date_from=add_date_from,
                        add_date_to=add_date_to,
                        recent_base_date=str(base_date),
                        keyword=keyword,
                    ),
                    params={
                        "기준일자": str(base_date),
                        "판정기준": "등록우선/수정대체",
                        "사용중만": only_active_recent,
                        "TopN": 5000,
                    },
                    logger=logger,
                )

            add_date_col = _pick_col(df, ["등록일자", "등록 일자", "Rd06_Add_Date"])
            mod_date_col = _pick_col(df, ["수정일자", "수정 일자", "Rd06_Mod_Date"])
            add_date_sr = _parse_char8_date_series_local(df[add_date_col]) if add_date_col else pd.Series(pd.NaT, index=df.index)
            mod_date_sr = _parse_char8_date_series_local(df[mod_date_col]) if mod_date_col else pd.Series(pd.NaT, index=df.index)

            effective_date = add_date_sr.combine_first(mod_date_sr)
            effective_ts = pd.to_datetime(effective_date, errors="coerce")

            basis = pd.Series("", index=df.index, dtype="object")
            basis.loc[add_date_sr.notna()] = "등록"
            basis.loc[add_date_sr.isna() & mod_date_sr.notna()] = "수정대체"

            mask_recent = effective_date.notna() & (effective_date.dt.date >= base_date)
            df = df.loc[mask_recent].copy()
            df_display = df.copy()

            df, df_display = _apply_name_filter_local(df, df_display, "부서명", dept_nm_resolved or dept_nm)
            df, df_display = _apply_name_filter_local(df, df_display, "직책", duty_nm_resolved or duty_nm)
            df, df_display = _apply_name_filter_local(df, df_display, "영업지역", district_nm_resolved or district_nm)
            df, df_display = _apply_name_filter_local(df, df_display, "재고위치", stock_nm_resolved or stock_nm)
            df, df_display = _apply_name_filter_local(df, df_display, "등록자", add_user_nm)
            df, df_display = _apply_name_filter_local(df, df_display, "수정자", mod_user_nm)
            df, df_display = _apply_date_filter_local(df, df_display, "등록일자", add_date_from, add_date_to)
            df, df_display = _apply_date_filter_local(df, df_display, "수정일자", mod_date_from, mod_date_to)

            params = {
                "기준일자": str(base_date),
                "판정기준": "등록우선/수정대체",
                "사용중만": only_active_recent,
                "사용자코드": user_cd,
                "사용자ID": user_id,
                "사번": sabun,
                "사용자명": user_nm,
                "부서명": dept_nm_resolved or dept_nm,
                "부서코드": dept_cd,
                "직책": duty_nm_resolved or duty_nm,
                "직책코드": duty_cd,
                "영업지역": district_nm_resolved or district_nm,
                "영업지역코드": district_cd,
                "재고위치": stock_nm_resolved or stock_nm,
                "재고위치코드": stock_cd,
                "등록자": add_user_nm,
                "등록일자": add_date_from if (add_date_from and add_date_from == add_date_to) else f"{add_date_from}~{add_date_to}" if (add_date_from or add_date_to) else "",
                "등록일자From": add_date_from,
                "등록일자To": add_date_to,
                "수정자": mod_user_nm,
                "수정일자": mod_date_from if (mod_date_from and mod_date_from == mod_date_to) else f"{mod_date_from}~{mod_date_to}" if (mod_date_from or mod_date_to) else "",
                "수정일자From": mod_date_from,
                "수정일자To": mod_date_to,

                "통합검색": keyword,
                "TopN": 5000,
            }

            query_summary = _build_query_summary_local(
                user_cd=user_cd,
                user_id=user_id,
                sabun=sabun,
                user_nm=user_nm,
                dept_nm=dept_nm_resolved or dept_nm,
                duty_nm=duty_nm_resolved or duty_nm,
                district_nm=district_nm_resolved or district_nm,
                stock_nm=stock_nm_resolved or stock_nm,
                add_user_nm=add_user_nm,
                add_date_from=add_date_from,
                add_date_to=add_date_to,
                mod_user_nm=mod_user_nm,
                mod_date_from=mod_date_from,
                mod_date_to=mod_date_to,
                keyword=keyword,
                recent_base_date=str(base_date),
            )

            if df.empty:
                return _push_user_text(
                    room=room,
                    session_state=session_state,
                    txt=txt,
                    message="해당 조회조건의 자료가 없습니다.",
                    query_summary=query_summary,
                    params=params,
                    logger=logger,
                )

            df["판정기준구분"] = basis.loc[df.index]
            df["판정기준일자"] = effective_date.loc[df.index].dt.strftime("%Y-%m-%d")
            df["판정기준일시"] = effective_ts.loc[df.index].dt.strftime("%Y-%m-%d %H:%M:%S")
            df["_sort_ts"] = effective_ts.loc[df.index]
            df = df.sort_values("_sort_ts", ascending=False, na_position="last").drop(columns=["_sort_ts"])

            display_order = [
                "사용자코드",
                "사용자ID",
                "사번",
                "사용자명",
                "부서명",
                "직책",
                "영업지역",
                "재고위치",
                "삭제여부",
                "판정기준구분",
                "판정기준일자",
                "판정기준일시",
                "등록일자",
                "수정일자",
            ]
            display_cols = [c for c in display_order if c in df.columns]
            df_display = df[display_cols].copy()

            for col in ("등록일자", "수정일자"):
                if col in df_display.columns:
                    df_display[col] = _format_char8_date_series_local(df_display[col])

            return _push_table_result(
                room=room,
                session_state=session_state,
                df=df,
                df_display=df_display,
                title="최근 입사자",
                action="최근 입사자",
                txt=txt,
                params=params,
                query_summary=query_summary or "최근 입사자",
                logger=logger,
            )

        # --------------------------------------------------------------
        # 2-B) 일반 사용자 조회 NLQ
        # --------------------------------------------------------------
        df_raw = U.search_users_full(
            top=top_n,
            only_active=True,
            user_cd=user_cd,
            user_id=user_id,
            sabun=sabun,
            user_nm=user_nm,
            department=dept_cd,
            duty=duty_cd,
            district=district_cd,
            stock_cd=stock_cd,
            add_user_nm=add_user_nm,
            add_date_from=add_date_from,
            add_date_to=add_date_to,
            mod_user_nm=mod_user_nm,
            mod_date_from=mod_date_from,
            mod_date_to=mod_date_to,
            keyword=keyword,
        )

        df_raw = _ensure_df(df_raw)
        logger.info("[nlq.users] rows=%s", len(df_raw))

        params = {
            "사용자코드": user_cd,
            "사용자ID": user_id,
            "사번": sabun,
            "사용자명": user_nm,
            "부서명": dept_nm_resolved or dept_nm,
            "부서코드": dept_cd,
            "직책": duty_nm_resolved or duty_nm,
            "직책코드": duty_cd,
            "영업지역": district_nm_resolved or district_nm,
            "영업지역코드": district_cd,
            "재고위치": stock_nm_resolved or stock_nm,
            "재고위치코드": stock_cd,
            "등록자": add_user_nm,
            "등록일자": add_date_from if (add_date_from and add_date_from == add_date_to) else f"{add_date_from}~{add_date_to}" if (add_date_from or add_date_to) else "",
            "등록일자From": add_date_from,
            "등록일자To": add_date_to,
            "수정자": mod_user_nm,
            "수정일자": mod_date_from if (mod_date_from and mod_date_from == mod_date_to) else f"{mod_date_from}~{mod_date_to}" if (mod_date_from or mod_date_to) else "",
            "수정일자From": mod_date_from,
            "수정일자To": mod_date_to,
            "통합검색": keyword,
            "사용중만": True,
            "TopN": top_n,
        }

        query_summary = _build_query_summary_local(
            user_cd=user_cd,
            user_id=user_id,
            sabun=sabun,
            user_nm=user_nm,
            dept_nm=dept_nm_resolved or dept_nm,
            duty_nm=duty_nm_resolved or duty_nm,
            district_nm=district_nm_resolved or district_nm,
            stock_nm=stock_nm_resolved or stock_nm,
            add_user_nm=add_user_nm,
            add_date_from=add_date_from,
            add_date_to=add_date_to,
            mod_user_nm=mod_user_nm,
            mod_date_from=mod_date_from,
            mod_date_to=mod_date_to,
            keyword=keyword,
        )

        if df_raw.empty:
            return _push_user_text(
                room=room,
                session_state=session_state,
                txt=txt,
                message="해당 조회조건의 자료가 없습니다.",
                query_summary=query_summary,
                params=params,
                logger=logger,
            )

        df = users_view._prepare_users_df(df_raw)
        df_display = users_view._build_user_list_view(df)

        df, df_display = _apply_name_filter_local(df, df_display, "부서명", dept_nm_resolved or dept_nm)
        df, df_display = _apply_name_filter_local(df, df_display, "직책", duty_nm_resolved or duty_nm)
        df, df_display = _apply_name_filter_local(df, df_display, "영업지역", district_nm_resolved or district_nm)
        df, df_display = _apply_name_filter_local(df, df_display, "재고위치", stock_nm_resolved or stock_nm)
        df, df_display = _apply_name_filter_local(df, df_display, "등록자", add_user_nm)
        df, df_display = _apply_date_filter_local(df, df_display, "등록일자", add_date_from, add_date_to)
        df, df_display = _apply_date_filter_local(df, df_display, "수정일자", mod_date_from, mod_date_to)

        if df_display is None or len(df_display) == 0:
            return _push_user_text(
                room=room,
                session_state=session_state,
                txt=txt,
                message="해당 조회조건의 자료가 없습니다.",
                query_summary=query_summary,
                params=params,
                logger=logger,
            )

        return _push_table_result(
            room=room,
            session_state=session_state,
            df=df,
            df_display=df_display,
            title="사용자목록 + 부서명",
            action="사용자목록 + 부서명",
            txt=txt,
            params=params,
            query_summary=query_summary or "사용자 조회",
            logger=logger,
        )

    except Exception:
        logger.exception("[nlq.users] user master query failed")
        return _push_user_text(
            room=room,
            session_state=session_state,
            txt=txt,
            message="사용자 조회 중 오류가 발생했습니다. 조회조건을 다시 확인해 주세요.",
            query_summary="사용자 조회",
            params={},
            logger=logger,
        )