# app/sims/nlq/nlq_codes.py
# 업무코드 관련 자연어 질의 처리 2026-03-31

# app/sims/nlq/nlq_codes.py

from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
import re
import uuid

import pandas as pd

from app.ui.chat_middleware import push_sims_result_to_chat
from app.sims.views.codes import (
    _service_search,
    _service_find_group_codes_by_kind_name,
    _merge_group_results,
    _build_code_master_view,
    _build_group_result_view,
    _build_codes_query_condition,
    _split_condition_and_note,
    _build_codes_master_llm_summary,
)

_Q_GCODE = re.compile(r"(?:그룹코드|gcode)\s*([0-9A-Za-z]{4})", re.IGNORECASE)
_Q_TCODE = re.compile(r"(?:상세코드|항목코드|tcode)\s*([0-9A-Za-z]{1,6})", re.IGNORECASE)

_KIND_LABEL_PATTERNS = r"(?:코드종류명|코드종류|그룹명)"
_HNM_LABEL_PATTERNS = r"(?:한글명|코드명|이름)"
_ENM_LABEL_PATTERNS = r"(?:영문명|영문이름)"
_SNM_LABEL_PATTERNS = r"(?:약칭|짧은이름|짧은 이름)"
_OTHER_LABEL_PATTERNS = r"(?:설명|비고|메모|기타|기타1|기타2|기타3)"

_Q_KIND_NAME = re.compile(rf"{_KIND_LABEL_PATTERNS}\s*([^\s,?.!]+)")
_Q_HNM = re.compile(rf"{_HNM_LABEL_PATTERNS}\s*([^\s,?.!]+)")
_Q_ENM = re.compile(rf"{_ENM_LABEL_PATTERNS}\s*([^\s,?.!]+)")
_Q_SNM = re.compile(rf"{_SNM_LABEL_PATTERNS}\s*([^\s,?.!]+)")
_Q_OTHER = re.compile(rf"{_OTHER_LABEL_PATTERNS}\s*([^\s,?.!]+)")
_Q_MOD_USER = re.compile(r"(?:수정자명|수정자|변경자|수정한사람|수정한 사람)\s*([^\s,?.!]+)")

_CODES_STOP = {
    "업무코드", "코드", "코드명", "조회", "검색", "목록", "보여줘", "알려줘",
    "찾아줘", "찾아", "검색해줘", "마스터",
    "그룹코드", "상세코드", "항목코드", "코드종류", "코드종류명", "그룹명",
    "한글명", "영문명", "영문이름", "약칭", "짧은이름", "짧은", "기타",
    "설명", "비고", "메모",
    "수정자", "수정자명", "변경자", "수정일자", "수정일", "변경일자", "변경일",
    "사용중", "사용중만", "상세", "그룹",
}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _strip_tail_josa(s: str) -> str:
    s = _clean_text(s)
    for j in ("의", "은", "는", "이", "가", "을", "를", "과", "와", "에서", "으로", "로", "만"):
        if s.endswith(j) and len(s) > len(j):
            return s[:-len(j)].strip()
    return s


def _strip_tail_request_words(s: str) -> str:
    s = _clean_text(s)
    for _ in range(4):
        s2 = re.sub(
            r"(?:조회|검색|목록|보여줘|알려줘|찾아줘|찾아봐줘|찾아봐|찾아|검색해줘)$",
            "",
            s,
        ).strip()
        if s2 == s:
            break
        s = s2
    return s


def _norm_kw(s: str) -> str:
    s = _clean_text(s)
    s = _strip_tail_request_words(s)
    s = _strip_tail_josa(s)
    return s.strip()


def _clean_code_token(s: str) -> str:
    s = _norm_kw(s)
    if not s:
        return ""
    if s in _CODES_STOP:
        return ""
    return s


def _extract_labeled_keyword(txt: str, label_patterns: str) -> str:
    m = re.search(rf"{label_patterns}\s*([^\s,?.!]+)", txt)
    if m:
        v = _clean_code_token(m.group(1) or "")
        if v:
            return v

    m = re.search(rf"([^\s,?.!]+)\s*{label_patterns}", txt)
    if m:
        v = _clean_code_token(m.group(1) or "")
        if v:
            return v

    return ""


def _extract_gcode(txt: str) -> str:
    m = _Q_GCODE.search(txt)
    if m:
        return _clean_text(m.group(1))

    m = re.search(r"([0-9A-Za-z]{4})\s*(?:그룹코드)", txt, flags=re.IGNORECASE)
    if m:
        return _clean_text(m.group(1))

    return ""


def _extract_tcode(txt: str) -> str:
    m = _Q_TCODE.search(txt)
    if m:
        return _clean_text(m.group(1))

    m = re.search(r"([0-9A-Za-z]{1,6})\s*(?:상세코드|항목코드)", txt, flags=re.IGNORECASE)
    if m:
        return _clean_text(m.group(1))

    return ""


def _extract_kind_name(txt: str) -> str:
    # 1) 정식 별칭: 코드종류명 / 코드종류 / 그룹코드명 / 그룹명
    label_patterns = r"(?:코드종류명|코드종류|그룹코드명|그룹명)"

    m = re.search(rf"{label_patterns}\s*([^\s,?.!]+)", txt)
    if m:
        v = _clean_code_token(m.group(1) or "")
        if v:
            return v

    m = re.search(rf"([^\s,?.!]+)\s*{label_patterns}", txt)
    if m:
        v = _clean_code_token(m.group(1) or "")
        if v:
            return v

    # 2) 예외 허용: "그룹코드 거래처 조회"
    #    - 실제 그룹코드(예: 0005)가 없을 때만
    #    - 그룹코드 뒤의 값을 코드종류명 별칭으로 해석
    if not _extract_gcode(txt):
        m = re.search(r"(?:그룹코드)\s*([^\s,?.!]+)", txt)
        if m:
            raw = _clean_text(m.group(1) or "")
            v = _clean_code_token(raw)
            if v:
                return v

        m = re.search(r"([^\s,?.!]+)\s*(?:그룹코드)", txt)
        if m:
            raw = _clean_text(m.group(1) or "")
            v = _clean_code_token(raw)
            if v:
                return v

    return ""

def _extract_hnm_kw(txt: str) -> str:
    return _extract_labeled_keyword(txt, _HNM_LABEL_PATTERNS)


def _extract_enm_kw(txt: str) -> str:
    return _extract_labeled_keyword(txt, _ENM_LABEL_PATTERNS)


def _extract_snm_kw(txt: str) -> str:
    return _extract_labeled_keyword(txt, _SNM_LABEL_PATTERNS)


def _extract_other_kw(txt: str) -> str:
    return _extract_labeled_keyword(txt, _OTHER_LABEL_PATTERNS)


def _digits_only(value: Optional[str]) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _last_day_of_month(yyyymm: str) -> str:
    first = datetime.strptime(yyyymm + "01", "%Y%m%d")
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1, day=1)
    else:
        next_first = first.replace(month=first.month + 1, day=1)
    return (next_first - timedelta(days=1)).strftime("%Y%m%d")


def _norm_date_from(value: Optional[str]) -> str:
    digits = _digits_only(value)
    if len(digits) == 8:
        return digits
    if len(digits) == 6:
        return digits + "01"
    if len(digits) == 4:
        return digits + "0101"
    return ""


def _norm_date_to(value: Optional[str]) -> str:
    digits = _digits_only(value)
    if len(digits) == 8:
        return digits
    if len(digits) == 6:
        return _last_day_of_month(digits)
    if len(digits) == 4:
        return digits + "1231"
    return ""


def _expand_date_range_token(a: str, b: str = "") -> tuple[str, str]:
    a_from = _norm_date_from(a)
    a_to = _norm_date_to(a)

    if not b:
        return a_from, a_to

    b_to = _norm_date_to(b)
    if a_from and b_to:
        return a_from, b_to

    return a_from, a_to


def _extract_mod_user_name(txt: str) -> str:
    def _looks_like_date_or_range_token(s: str) -> bool:
        s = str(s or "").strip()
        if not s:
            return False
        compact = re.sub(r"[\s./]", "", s)
        if re.fullmatch(r"[0-9]{6}", compact):
            return True
        if re.fullmatch(r"[0-9]{8}", compact):
            return True
        if re.fullmatch(r"[0-9]{6}[~\-][0-9]{6}", compact):
            return True
        if re.fullmatch(r"[0-9]{8}[~\-][0-9]{8}", compact):
            return True
        return False

    def _normalize_user_kw(raw: str) -> str:
        s = str(raw or "").strip()
        s = re.sub(r"(?:인)$", "", s).strip()
        return _clean_code_token(s)

    explicit_patterns = [
        r"(?:수정자명|수정자|변경자)\s*([^\s,?.!]+)",
        r"([^\s,?.!]+)\s*(?:수정자명|수정자|변경자)",
    ]
    for pat in explicit_patterns:
        m = re.search(pat, txt)
        if not m:
            continue
        raw = str(m.group(1) or "").strip()
        if _looks_like_date_or_range_token(raw):
            continue
        v = _normalize_user_kw(raw)
        if v:
            return v

    action_patterns = [
        r"([^\s,?.!~\-]+?)(?:이|가)?\s*수정한\s*(?:업무코드|코드)",
        r"([^\s,?.!~\-]+?)(?:이|가)?\s*변경한\s*(?:업무코드|코드)",
        r"([^\s,?.!~\-]+?)(?:이|가)?\s*수정한\s*것",
        r"([^\s,?.!~\-]+?)(?:이|가)?\s*변경한\s*것",
    ]
    for pat in action_patterns:
        m = re.search(pat, txt)
        if not m:
            continue
        raw = str(m.group(1) or "").strip()
        if _looks_like_date_or_range_token(raw):
            continue
        v = _normalize_user_kw(raw)
        if v:
            return v

    m = _Q_MOD_USER.search(txt)
    if m:
        raw = str(m.group(1) or "").strip()
        if not _looks_like_date_or_range_token(raw):
            v = _normalize_user_kw(raw)
            if v:
                return v

    return ""


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

def _extract_add_date_range(txt: str) -> tuple[str, str]:
    cleaned = str(txt or "")

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
        r"\s*(?:등록|등록된|등록한|작성|작성된|작성한)",
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
        r"\s*(?:등록|등록된|등록한|작성|작성된|작성한)",
        cleaned,
    )
    if m:
        return _expand_date_range_token(m.group(1))

    return "", ""


def _pick_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _contains_mask(series: pd.Series, keyword: str) -> pd.Series:
    kw = _clean_text(keyword)
    if not kw:
        return pd.Series([True] * len(series), index=series.index)
    return series.fillna("").astype(str).str.contains(kw, case=False, na=False)


def _series_to_date_digits(sr: pd.Series) -> pd.Series:
    return (
        sr.fillna("")
        .astype(str)
        .str.replace(r"[^0-9]", "", regex=True)
        .str[:8]
    )


def _apply_local_filters(
    df: pd.DataFrame,
    *,
    mod_user_nm_kw: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",
) -> pd.DataFrame:
    out = df.copy()

    if mod_user_nm_kw:
        user_col = _pick_col(
            out,
            ["__raw_mod_user_nm", "수정자명", "수정자", "mod_user_nm", "Rd01_Mod_Cd"],
        )
        if user_col:
            out = out[_contains_mask(out[user_col], mod_user_nm_kw)]

    date_from = _norm_date_from(mod_date_from)
    date_to = _norm_date_to(mod_date_to)

    if date_from or date_to:
        date_col = _pick_col(
            out,
            ["__raw_mod_date", "수정일자", "Rd01_Mod_Date"],
        )
        if date_col:
            s = _series_to_date_digits(out[date_col])
            if date_from:
                out = out[s >= date_from]
                s = _series_to_date_digits(out[date_col])
            if date_to:
                out = out[s <= date_to]

    return out.reset_index(drop=True)


def _extract_keyword(txt: str) -> str:
    if any(
        k in txt
        for k in (
            "그룹코드", "상세코드", "항목코드", "코드종류", "코드종류명",
            "한글명", "코드명", "이름",
            "영문명", "영문이름", "약칭",
            "기타", "기타1", "기타2", "기타3", "설명", "비고", "메모",
            "등록자", "등록자명", "등록일자", "등록일", "등록한",
            "수정자", "수정자명", "변경자", "수정일자", "수정일", "변경일자", "변경일",
        )
    ):
        return ""
    
    m = re.search(r"(?:코드명|업무코드)\s*([^\n]+)", txt)
    if m:
        v = _clean_code_token(m.group(1) or "")
        if v:
            return v

    residual = re.sub(
        r"(업무코드|코드명|코드|조회|검색|목록|보여줘|알려줘|찾아줘|찾아봐줘|찾아봐|찾아|마스터)",
        " ",
        txt,
    )
    residual = re.sub(r"\s+", " ", residual).strip()
    return _clean_code_token(residual)


def _build_code_params(
    *,
    gcode: str = "",
    tcode: str = "",
    keyword: str = "",
    kind_name_kw: str = "",
    hnm_kw: str = "",
    enm_kw: str = "",
    snm_kw: str = "",
    other_kw: str = "",
    add_user_nm_kw: str = "",
    add_date_from: str = "",
    add_date_to: str = "",
    mod_user_nm_kw: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",
    only_active: bool = True,
    top: int = 2000,
) -> Dict[str, Any]:
    return {
        "그룹코드": gcode,
        "상세코드": tcode,
        "통합검색": keyword,
        "코드종류명": kind_name_kw,
        "한글명": hnm_kw,
        "영문명": enm_kw,
        "약칭": snm_kw,
        "기타": other_kw,
        "기타/설명": other_kw,
        "등록자": add_user_nm_kw,
        "등록일자": (
            add_date_from
            if (add_date_from and add_date_from == add_date_to)
            else f"{add_date_from}~{add_date_to}" if (add_date_from or add_date_to) else ""
        ),
        "등록일자From": add_date_from,
        "등록일자To": add_date_to,
        "수정자": mod_user_nm_kw,
        "수정일자": (
            mod_date_from
            if (mod_date_from and mod_date_from == mod_date_to)
            else f"{mod_date_from}~{mod_date_to}" if (mod_date_from or mod_date_to) else ""
        ),
        "수정일자From": mod_date_from,
        "수정일자To": mod_date_to,
        "사용중만": bool(only_active),
        "TopN": int(top),
    }

def _build_group_params(
    *,
    gcode: str = "",
    kind_name_kw: str = "",
    add_user_nm_kw: str = "",
    add_date_from: str = "",
    add_date_to: str = "",
    mod_user_nm_kw: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",
    only_active: bool = True,
    top: int = 2000,
) -> Dict[str, Any]:
    return {
        "그룹코드": gcode,
        "코드종류명": kind_name_kw,
        "등록자": add_user_nm_kw,
        "등록일자": (
            add_date_from
            if (add_date_from and add_date_from == add_date_to)
            else f"{add_date_from}~{add_date_to}" if (add_date_from or add_date_to) else ""
        ),
        "등록일자From": add_date_from,
        "등록일자To": add_date_to,
        "수정자": mod_user_nm_kw,
        "수정일자": (
            mod_date_from
            if (mod_date_from and mod_date_from == mod_date_to)
            else f"{mod_date_from}~{mod_date_to}" if (mod_date_from or mod_date_to) else ""
        ),
        "수정일자From": mod_date_from,
        "수정일자To": mod_date_to,
        "사용중만": bool(only_active),
        "TopN": int(top),
    }

def _build_code_query_summary(
    *,
    gcode: str = "",
    tcode: str = "",
    keyword: str = "",
    kind_name_kw: str = "",
    hnm_kw: str = "",
    enm_kw: str = "",
    snm_kw: str = "",
    other_kw: str = "",
    add_user_nm_kw: str = "",
    add_date_from: str = "",
    add_date_to: str = "",    
    mod_user_nm_kw: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",
) -> str:
    parts: List[str] = []
    if gcode:
        parts.append(f"그룹코드 {gcode}")
    if tcode:
        parts.append(f"상세코드 {tcode}")
    if keyword:
        parts.append(f"통합검색 {keyword}")
    if kind_name_kw:
        parts.append(f"코드종류명 {kind_name_kw}")
    if hnm_kw:
        parts.append(f"한글명 {hnm_kw}")
    if enm_kw:
        parts.append(f"영문명 {enm_kw}")
    if snm_kw:
        parts.append(f"약칭 {snm_kw}")
    if other_kw:
        parts.append(f"기타/설명 {other_kw}")

    if add_user_nm_kw:
        parts.append(f"등록자 {add_user_nm_kw}")

    if add_date_from and add_date_to:
        if add_date_from == add_date_to:
            parts.append(f"등록일자 {add_date_from}")
        else:
            parts.append(f"등록일자 {add_date_from}~{add_date_to}")
    elif add_date_from:
        parts.append(f"등록일자 {add_date_from}")
    elif add_date_to:
        parts.append(f"등록일자 {add_date_to}")

    if mod_user_nm_kw:
        parts.append(f"수정자 {mod_user_nm_kw}")


    if mod_date_from and mod_date_to:
        if mod_date_from == mod_date_to:
            parts.append(f"수정일자 {mod_date_from}")
        else:
            parts.append(f"수정일자 {mod_date_from}~{mod_date_to}")
    elif mod_date_from:
        parts.append(f"수정일자 {mod_date_from}")
    elif mod_date_to:
        parts.append(f"수정일자 {mod_date_to}")
    return " / ".join(parts)

def _codes_summary_line(query_summary: str) -> str:
    qs = str(query_summary or "").strip()
    return f"조회조건: {qs}" if qs else "조회조건: 전체"

def _build_group_query_summary(
    *,
    gcode: str = "",
    kind_name_kw: str = "",
    add_user_nm_kw: str = "",
    add_date_from: str = "",
    add_date_to: str = "",    
    mod_user_nm_kw: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",
) -> str:
    parts: List[str] = []
    if gcode:
        parts.append(f"그룹코드 {gcode}")
    if kind_name_kw:
        parts.append(f"코드종류명 {kind_name_kw}")

    if add_user_nm_kw:
        parts.append(f"등록자 {add_user_nm_kw}")

    if add_date_from and add_date_to:
        if add_date_from == add_date_to:
            parts.append(f"등록일자 {add_date_from}")
        else:
            parts.append(f"등록일자 {add_date_from}~{add_date_to}")
    elif add_date_from:
        parts.append(f"등록일자 {add_date_from}")
    elif add_date_to:
        parts.append(f"등록일자 {add_date_to}")

    if mod_user_nm_kw:
        parts.append(f"수정자 {mod_user_nm_kw}")

    if mod_date_from and mod_date_to:
        if mod_date_from == mod_date_to:
            parts.append(f"수정일자 {mod_date_from}")
        else:
            parts.append(f"수정일자 {mod_date_from}~{mod_date_to}")
    elif mod_date_from:
        parts.append(f"수정일자 {mod_date_from}")
    elif mod_date_to:
        parts.append(f"수정일자 {mod_date_to}")
    return " / ".join(parts)


def _push_codes_result(
    *,
    txt: str,
    title: str,
    action: str,
    df: pd.DataFrame,
    df_display: pd.DataFrame,
    params_out: Dict[str, Any],
    query_summary: str,
    source: str,
    extra_meta: Dict[str, Any] | None = None,
) -> None:
    df_full = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    df_display_all = df_display.copy() if isinstance(df_display, pd.DataFrame) else pd.DataFrame()

    total = int(len(df_full)) if not df_full.empty else int(len(df_display_all))
    show_n = min(total, 500)
    df_show = df_display_all.head(show_n).copy()

    try:
        query_condition = _build_codes_query_condition(params_out, total, show_n)
        condition_text, note = _split_condition_and_note(query_condition)
        codes_master_summary = _build_codes_master_llm_summary(
            df_display_all,
            query_condition=query_condition,
            total=total,
            display_count=show_n,
        )
        llm_summary_md = str(codes_master_summary.get("llm_summary_md") or "")
    except Exception:
        condition_text = query_summary or "전체"
        note = (
            f"조회결과: **{total:,}건** (전부 표시)"
            if show_n >= total
            else f"조회결과: **{total:,}건** (표시는 상위 {show_n:,}건)"
        )
        codes_master_summary = {}
        llm_summary_md = ""

    meta = {
        "nlq": True,
        "master_nlq": True,
        "route": "master",
        "canonical_action": action,
        "domain": "codes",
        "source": source,
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
        "analysis_type": "codes_master",
        "llm_summary_kind": "codes_master_summary",
        "llm_summary_md": llm_summary_md,
        "codes_master_summary": codes_master_summary,
        "analysis_row_count": total,
        "row_count_total_for_analysis": total,
        "summary_basis": "전체 조회결과 기준",
        "field_notes": (
            "업무코드 마스터 분석은 전체 조회결과 기준 집계요약을 우선 근거로 답합니다. "
            "화면 표시는 일부 행으로 제한될 수 있습니다."
        ),
    }
    if extra_meta:
        meta.update(extra_meta)

    result = {
        "final": True,
        "type": "table",
        "title": title,
        "action": action,
        "params": params_out,
        "df": df_full,
        "df_display": df_show,
        "records": df_show.to_dict(orient="records") if not df_show.empty else [],
        "columns": list(df_show.columns) if not df_show.empty else [],
        "message": f"{title} {total:,}건" if total > 0 else "해당 조회조건의 자료가 없습니다.",
        "meta": meta,
    }
    push_sims_result_to_chat(result, action)

def _push_codes_text(
    *,
    txt: str,
    title: str,
    action: str,
    params_out: Dict[str, Any],
    query_summary: str,
    source: str,
    message: str,
    logger=None,
    extra_meta: Dict[str, Any] | None = None,
) -> bool:
    summary_line = _codes_summary_line(query_summary)
    display_message = f"{message}\n\n{summary_line}"

    meta = {
        "nlq": True,
        "master_nlq": True,
        "route": "master",
        "canonical_action": action,
        "domain": "codes",
        "source": source,
        "nlq_query": txt,
        "_force_push": True,
        "_nlq_nonce": str(uuid.uuid4()),
        "row_count": 0,
        "row_count_total": 0,
        "condition": query_summary or "전체",
        "query_summary": query_summary,
        "summary_md": summary_line,
    }
    if extra_meta:
        meta.update(extra_meta)

    result = {
        "final": True,
        "type": "text",
        "title": title,
        "action": action,
        "params": params_out,
        "data": display_message,
        "message": display_message,
        "meta": meta,
    }
    push_sims_result_to_chat(result, action)

    if logger is not None:
        try:
            logger.info("[nlq.codes] text result pushed action=%r message=%r", action, display_message)
        except Exception:
            pass
    return True

def try_handle_codes_nlq(
    txt: str,
    *,
    room: Dict[str, Any],
    session_state: Dict[str, Any],
    make_ts,
    next_seq,
    logger,
) -> bool:
    t = _clean_text(txt)
    if not t:
        return False
    
    # 제품/거래처/사용자 코드와 혼동되는 문장은 업무코드 NLQ에서 제외
    if any(k in t for k in ("제품코드", "보험코드", "거래처코드", "사용자코드")):
        return False

    # "업무코드"가 없어도 코드종류명/그룹코드/상세코드 같은 명시 조건이면 처리
    code_anchor_words = (
        "업무코드",
        "코드마스터",
        "그룹코드",
        "상세코드",
        "항목코드",
        "코드종류",
        "코드종류명",
        "코드명",
        "한글명",
    )

    if not any(k in t for k in code_anchor_words):
        return False

    signals = (
        "업무코드", "그룹코드", "상세코드", "항목코드",
        "코드종류", "코드종류명", "코드명", "한글명", "영문명", "약칭",
        "기타", "기타1", "기타2", "기타3", "설명", "비고", "메모",
        "등록자", "등록자명", "등록일자", "등록일", "등록한",
        "수정자", "수정자명", "변경자", "수정일자", "수정일", "변경일자", "변경일",
    )
    if not any(k in t for k in signals):
        return False

# 값 없는 속성명 질의인지 여부 판단 (예: "업무코드 한글명 인")
# "인"은 "~인", "~하는", "~한" 등의 형태로 자연어 질의에서 속성명 뒤에 붙어서 값이 없음을 나타내는 조사입니다.

    def _strip_attr_tail_in(s: str) -> str:
        s = _clean_text(s)
        if len(s) >= 2 and s.endswith("인"):
            s = s[:-1].strip()
        return s

    def _extract_add_user_name_local(text: str) -> str:
        def _looks_like_date_or_range_token(s: str) -> bool:
            s = str(s or "").strip()
            if not s:
                return False
            compact = re.sub(r"[\s./]", "", s)
            if re.fullmatch(r"[0-9]{6}", compact):
                return True
            if re.fullmatch(r"[0-9]{8}", compact):
                return True
            if re.fullmatch(r"[0-9]{6}[~\-][0-9]{6}", compact):
                return True
            if re.fullmatch(r"[0-9]{8}[~\-][0-9]{8}", compact):
                return True
            return False

        def _normalize_user_kw(raw: str) -> str:
            s = str(raw or "").strip()
            s = re.sub(r"(?:인)$", "", s).strip()
            return _clean_code_token(s)

        explicit_patterns = [
            r"(?:등록자명|등록자)\s*([^\s,?.!]+)",
            r"([^\s,?.!]+)\s*(?:등록자명|등록자)",
        ]
        for pat in explicit_patterns:
            m = re.search(pat, text)
            if not m:
                continue
            raw = str(m.group(1) or "").strip()
            if _looks_like_date_or_range_token(raw):
                continue
            v = _normalize_user_kw(raw)
            if v:
                return v

        action_patterns = [
            r"([^\s,?.!~\-]+?)(?:이|가)?\s*등록한\s*(?:업무코드|코드)",
            r"([^\s,?.!~\-]+?)(?:이|가)?\s*등록한\s*것",
        ]
        for pat in action_patterns:
            m = re.search(pat, text)
            if not m:
                continue
            raw = str(m.group(1) or "").strip()
            if _looks_like_date_or_range_token(raw):
                continue
            v = _normalize_user_kw(raw)
            if v:
                return v

        return ""

    def _extract_add_date_range_local(text: str) -> tuple[str, str]:
        return _extract_add_date_range(text)
    
    def _apply_add_local_filters(
        df: pd.DataFrame,
        *,
        add_user_nm_kw: str = "",
        add_date_from: str = "",
        add_date_to: str = "",
    ) -> pd.DataFrame:
        out = df.copy()

        if add_user_nm_kw:
            user_col = _pick_col(
                out,
                ["__raw_add_user_nm", "등록자", "등록자명", "add_user_nm", "Rd01_Add_Cd"],
            )
            if user_col:
                out = out[_contains_mask(out[user_col], add_user_nm_kw)]

        date_from = _norm_date_from(add_date_from)
        date_to = _norm_date_to(add_date_to)

        if date_from or date_to:
            date_col = _pick_col(
                out,
                ["__raw_add_date", "등록일자", "Rd01_Add_Date"],
            )
            if date_col:
                s = _series_to_date_digits(out[date_col])
                if date_from:
                    out = out[s >= date_from]
                    s = _series_to_date_digits(out[date_col])
                if date_to:
                    out = out[s <= date_to]

        return out.reset_index(drop=True)

    def _ensure_query_summary(summary: str, params_out: Dict[str, Any]) -> str:
        summary = _clean_text(summary)
        if summary:
            return summary

        parts = []
        for k in (
            "그룹코드",
            "상세코드",
            "통합검색",
            "코드종류명",
            "한글명",
            "영문명",
            "약칭",
            "기타/설명",
            "등록자",
            "등록일자",
            "수정자",
            "수정일자",
        ):
            v = _clean_text(params_out.get(k))
            if v:
                parts.append(f"{k} {v}")

        parts.append(f"사용중만 {bool(params_out.get('사용중만', True))}")
        parts.append(f"TopN {int(params_out.get('TopN', 2000))}")
        return " / ".join(parts)
    
    def _has_attr_word_without_value(text: str) -> bool:
        s = str(text or "").strip()

        checks = [
            ("한글명", hnm_kw),
            ("코드종류명", kind_name_kw),
            ("코드종류", kind_name_kw),
            ("영문명", enm_kw),
            ("영문이름", enm_kw),
            ("약칭", snm_kw),
            ("짧은이름", snm_kw),
            ("짧은 이름", snm_kw),
            ("기타", other_kw),
            ("기타1", other_kw),
            ("기타2", other_kw),
            ("기타3", other_kw),
            ("설명", other_kw),
            ("비고", other_kw),
            ("메모", other_kw),
            ("등록자", add_user_nm_kw),
            ("등록자명", add_user_nm_kw),
            ("등록일자", add_date_from or add_date_to),
            ("등록일", add_date_from or add_date_to),
            ("수정자", mod_user_nm_kw),
            ("수정자명", mod_user_nm_kw),
            ("변경자", mod_user_nm_kw),
            ("수정일자", mod_date_from or mod_date_to),
            ("수정일", mod_date_from or mod_date_to),
            ("변경일자", mod_date_from or mod_date_to),
            ("변경일", mod_date_from or mod_date_to),
        ]

        for label, value in checks:
            if label in s and not str(value or "").strip():
                return True
        return False


    gcode = _extract_gcode(t)
    tcode = _extract_tcode(t)
    kind_name_kw = _strip_attr_tail_in(_extract_kind_name(t))
    hnm_kw = _strip_attr_tail_in(_extract_hnm_kw(t))
    enm_kw = _strip_attr_tail_in(_extract_enm_kw(t))
    snm_kw = _strip_attr_tail_in(_extract_snm_kw(t))
    other_kw = _strip_attr_tail_in(_extract_other_kw(t))
    add_user_nm_kw = _extract_add_user_name_local(t)
    add_date_from, add_date_to = _extract_add_date_range_local(t)
    mod_user_nm_kw = _strip_attr_tail_in(_extract_mod_user_name(t))
    mod_date_from, mod_date_to = _extract_mod_date_range(t)
    keyword = _strip_attr_tail_in(_extract_keyword(t))

    # "한글명 코드종류 조회", "코드명 코드종류 조회", "이름 코드종류 조회"
    # -> literal value "코드종류" 로 해석
    if not hnm_kw and re.search(r"(?:한글명|코드명|이름)\s*코드종류(?:\s|$)", t):
        hnm_kw = "코드종류"
        keyword = ""

    only_active = True
    top = 5000 if any([
        add_user_nm_kw,
        add_date_from,
        add_date_to,
        mod_user_nm_kw,
        mod_date_from,
        mod_date_to,
    ]) else 2000


    # "업무코드 코드종류 조회", "업무코드 코드종류만 조회"
    # -> 무조건 코드종류 사전(9999) 조회로 직행
    code_kind_dictionary_intent = bool(
        re.search(r"업무코드\s*코드종류(?:만)?\s*조회", t)
        or re.search(r"업무코드\s*코드종류\s*(?:사전|목록)", t)
    )

    if code_kind_dictionary_intent and not any([
        tcode,
        hnm_kw, enm_kw, snm_kw, other_kw,
        add_user_nm_kw, add_date_from, add_date_to,
        mod_user_nm_kw, mod_date_from, mod_date_to,
    ]):
        try:
            df = _service_search(
                gcode="9999",
                tcode="",
                keyword="",
                kind_name_kw="",
                hnm_kw="",
                enm_kw="",
                snm_kw="",
                other_kw="",
                only_active=only_active,
                top=top,
            )
            if df is None:
                df = pd.DataFrame()

            df_display = _build_code_master_view(df)

            if df_display.empty:
                return _push_codes_text(
                    txt=txt,
                    title="코드종류 사전 (0건)",
                    action="코드종류 사전",
                    params_out={"그룹코드": "9999", "사용중만": True, "TopN": top},
                    query_summary="코드종류 사전",
                    source="업무코드마스터(Rddbc010)",
                    message="해당 조회조건의 자료가 없습니다.",
                    logger=logger,
                )

            _push_codes_result(
                txt=txt,
                title="코드종류 사전",
                action="코드종류 사전",
                df=df,
                df_display=df_display,
                params_out={"그룹코드": "9999", "사용중만": True, "TopN": top},
                query_summary="코드종류 사전",
                source="업무코드마스터(Rddbc010)",
            )
            logger.info("[nlq.codes] handled code-kind-dictionary rows=%s", len(df_display))
            return True

        except Exception:
            logger.exception("[nlq.codes] code-kind-dictionary failed")
            return _push_codes_text(
                txt=txt,
                title="코드종류 사전 오류",
                action="코드종류 사전",
                params_out={"그룹코드": "9999", "사용중만": True, "TopN": top},
                query_summary="코드종류 사전",
                source="업무코드마스터(Rddbc010)",
                message="업무코드 코드종류 조회 중 오류가 발생했습니다. 조회조건을 다시 확인해 주세요.",
                logger=logger,
            )
        
    if not add_user_nm_kw and "등록한" in t:
        m = re.search(r"([^\s,?.!~\-]+?)(?:이|가)?\s*등록한", t)
        if m:
            add_user_nm_kw = _clean_code_token(m.group(1) or "")

    if add_user_nm_kw:
        keyword = ""

    # 값 없는 속성명 질의는 전체조회로 보내지 말고 안내문으로 종료
    if (
        not gcode
        and not tcode
        and not keyword
        and not kind_name_kw
        and not hnm_kw
        and not enm_kw
        and not snm_kw
        and not other_kw
        and not mod_user_nm_kw
        and not mod_date_from
        and not mod_date_to
        and _has_attr_word_without_value(t)
    ):
        return _push_codes_text(
            txt=txt,
            title="코드명 검색",
            action="코드명 검색",
            params_out={
                "그룹코드": "",
                "상세코드": "",
                "통합검색": "",
                "코드종류명": "",
                "한글명": "",
                "영문명": "",
                "약칭": "",
                "기타/설명": "",
                "수정자": "",
                "수정일자": "",
                "사용중만": True,
                "TopN": 2000,
            },
            query_summary="값 없는 속성명 질의",
            source="업무코드마스터(Rddbc010)",
            message="조회값이 없습니다. 예: 업무코드 한글명 배송 조회 / 업무코드 코드종류명 부서 조회 / 업무코드 수정일자 202503 조회",
            logger=logger,
        )

    explicit_group_mode = any(
        k in t for k in (
            "그룹코드조회", "그룹코드 조회", "그룹별 코드", "그룹별 상세코드",
            "상세코드 목록", "해당 그룹의 상세코드",
        )
    )

    # 업무코드 + 조회 신호만 있고 실제 조건이 없으면 전체조회하지 않는다.
        
    if not any([
        gcode, tcode, keyword,
        kind_name_kw, hnm_kw, enm_kw, snm_kw, other_kw,
        add_user_nm_kw, add_date_from, add_date_to,
        mod_user_nm_kw, mod_date_from, mod_date_to,
    ]):

        return _push_codes_text(
            txt=txt,
            title="코드명 검색",
            action="코드명 검색",
            params_out={
                "그룹코드": "",
                "상세코드": "",
                "통합검색": "",
                "코드종류명": "",
                "한글명": "",
                "영문명": "",
                "약칭": "",
                "기타/설명": "",
                "수정자": "",
                "수정일자": "",
                "사용중만": True,
                "TopN": 2000,
            },
            query_summary="조건 없음",
            source="업무코드마스터(Rddbc010)",
            message="조회조건을 함께 입력해 주세요. 예: 업무코드 한글명 배송 조회 / 업무코드 그룹코드 0005 조회 / 업무코드 수정자 관리자 조회",
            logger=logger,
        )

    group_mode = explicit_group_mode or (
        not tcode
        and not keyword
        and not hnm_kw
        and not enm_kw
        and not snm_kw
        and not other_kw
        and (bool(gcode) or bool(kind_name_kw))
    )

    if group_mode:
        params_out = _build_group_params(
            gcode=gcode,
            kind_name_kw=kind_name_kw,
            add_user_nm_kw=add_user_nm_kw,
            add_date_from=add_date_from,
            add_date_to=add_date_to,
            mod_user_nm_kw=mod_user_nm_kw,
            mod_date_from=mod_date_from,
            mod_date_to=mod_date_to,
            only_active=only_active,
            top=top,
        )
                
        query_summary = _ensure_query_summary(
            _build_group_query_summary(
                gcode=gcode,
                kind_name_kw=kind_name_kw,
                add_user_nm_kw=add_user_nm_kw,
                add_date_from=add_date_from,
                add_date_to=add_date_to,
                mod_user_nm_kw=mod_user_nm_kw,
                mod_date_from=mod_date_from,
                mod_date_to=mod_date_to,
            ),
            params_out,
        )

        try:            
            resolved_group_codes: List[str] = []
            if gcode:
                resolved_group_codes = [gcode]
            else:
                matched_kinds_df = _service_find_group_codes_by_kind_name(
                    kind_name=kind_name_kw,
                    top=max(50, top),
                    only_active=only_active,
                )

                if matched_kinds_df is None or matched_kinds_df.empty or "그룹코드" not in matched_kinds_df.columns:
                    return _push_codes_text(
                        txt=txt,
                        title="그룹코드별 상세코드 조회 (0건)",
                        action="그룹코드조회",
                        params_out=params_out,
                        query_summary=query_summary,
                        source="업무코드마스터(Rddbc010)",
                        message="해당 조회조건의 자료가 없습니다.",
                        logger=logger,
                        extra_meta={"해석그룹코드목록": []},
                    )

                resolved_group_codes = (
                    matched_kinds_df["그룹코드"]
                    .fillna("")
                    .astype(str)
                    .str.strip()
                    .replace("", pd.NA)
                    .dropna()
                    .unique()
                    .tolist()
                )

            df = _merge_group_results(
                resolved_group_codes,
                only_active=only_active,
                top=top,
            )
            if df is None:
                df = pd.DataFrame()

            df = _apply_add_local_filters(
                df,
                add_user_nm_kw=add_user_nm_kw,
                add_date_from=add_date_from,
                add_date_to=add_date_to,
            )

            df = _apply_local_filters(
                df,
                mod_user_nm_kw=mod_user_nm_kw,
                mod_date_from=mod_date_from,
                mod_date_to=mod_date_to,
            )

            df_display = _build_group_result_view(df)

            if df_display.empty:
                return _push_codes_text(
                    txt=txt,
                    title="그룹코드별 상세코드 조회 (0건)",
                    action="그룹코드조회",
                    params_out=params_out,
                    query_summary=query_summary,
                    source="업무코드마스터(Rddbc010)",
                    message="해당 조회조건의 자료가 없습니다.",
                    logger=logger,
                    extra_meta={"해석그룹코드목록": resolved_group_codes},
                )

            _push_codes_result(
                txt=txt,
                title="그룹코드별 상세코드 조회",
                action="그룹코드조회",
                df=df,
                df_display=df_display,
                params_out=params_out,
                query_summary=query_summary,
                source="업무코드마스터(Rddbc010)",
                extra_meta={"해석그룹코드목록": resolved_group_codes},
            )
            logger.info("[nlq.codes] handled group rows=%s resolved=%s", len(df_display), resolved_group_codes)
            return True

        except Exception:
            logger.exception("[nlq.codes] group-mode failed")
            return _push_codes_text(
                txt=txt,
                title="그룹코드조회 오류",
                action="그룹코드조회",
                params_out=params_out,
                query_summary=query_summary,
                source="업무코드마스터(Rddbc010)",
                message="업무코드 NLQ 처리 중 오류가 발생했습니다. 조회조건을 다시 확인해 주세요.",
                logger=logger,
            )
#   상세코드 조회 모드
#  상세코드 조회 모드는 그룹코드가 명시적으로 있거나, 코드종류명이 있지만 상세코드명이나 통합검색 키워드 등이 없는 경우로 정의합니다.

    params_out = _build_code_params(
        gcode=gcode,
        tcode=tcode,
        keyword=keyword,
        kind_name_kw=kind_name_kw,
        hnm_kw=hnm_kw,
        enm_kw=enm_kw,
        snm_kw=snm_kw,
        other_kw=other_kw,
        add_user_nm_kw=add_user_nm_kw,
        add_date_from=add_date_from,
        add_date_to=add_date_to,        
        mod_user_nm_kw=mod_user_nm_kw,
        mod_date_from=mod_date_from,
        mod_date_to=mod_date_to,
        only_active=only_active,
        top=top,
    )

    query_summary = _ensure_query_summary(
        _build_code_query_summary(
            gcode=gcode,
            tcode=tcode,
            keyword=keyword,
            kind_name_kw=kind_name_kw,
            hnm_kw=hnm_kw,
            enm_kw=enm_kw,
            snm_kw=snm_kw,
            other_kw=other_kw,
            add_user_nm_kw=add_user_nm_kw,
            add_date_from=add_date_from,
            add_date_to=add_date_to,            
            mod_user_nm_kw=mod_user_nm_kw,
            mod_date_from=mod_date_from,
            mod_date_to=mod_date_to,
        ),
        params_out,
    )

    try:
        df = _service_search(
            gcode=gcode,
            tcode=tcode,
            keyword=keyword,
            kind_name_kw=kind_name_kw,
            hnm_kw=hnm_kw,
            enm_kw=enm_kw,
            snm_kw=snm_kw,
            other_kw=other_kw,
            add_user_nm_kw=add_user_nm_kw,
            add_date_from=add_date_from,
            add_date_to=add_date_to,
            mod_user_nm_kw=mod_user_nm_kw,
            mod_date_from=mod_date_from,
            mod_date_to=mod_date_to,
            only_active=only_active,
            top=top,
        )

        if df is None:
            df = pd.DataFrame()

        df = _apply_add_local_filters(
            df,
            add_user_nm_kw=add_user_nm_kw,
            add_date_from=add_date_from,
            add_date_to=add_date_to,
        )

        df = _apply_local_filters(
            df,
            mod_user_nm_kw=mod_user_nm_kw,
            mod_date_from=mod_date_from,
            mod_date_to=mod_date_to,
        )

        df_display = _build_code_master_view(df)

        if df_display.empty:
            return _push_codes_text(
                txt=txt,
                title="코드명 검색 (0건)",
                action="코드명 검색",
                params_out=params_out,
                query_summary=query_summary,
                source="업무코드마스터(Rddbc010)",
                message="해당 조회조건의 자료가 없습니다.",
                logger=logger,
            )

        _push_codes_result(
            txt=txt,
            title="코드명 검색",
            action="코드명 검색",
            df=df,
            df_display=df_display,
            params_out=params_out,
            query_summary=query_summary,
            source="업무코드마스터(Rddbc010)",
        )
        logger.info("[nlq.codes] handled master rows=%s", len(df_display))
        return True

    except Exception:
        logger.exception("[nlq.codes] master-mode failed")
        return _push_codes_text(
            txt=txt,
            title="코드명 검색 오류",
            action="코드명 검색",
            params_out=params_out,
            query_summary=query_summary,
            source="업무코드마스터(Rddbc010)",
            message="업무코드 NLQ 처리 중 오류가 발생했습니다. 조회조건을 다시 확인해 주세요.",
            logger=logger,
        )
