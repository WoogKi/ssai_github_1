# app/sims/nlq/nlq_goods.py

from __future__ import annotations

from typing import Any, Dict
import re
import uuid

import pandas as pd

from app.services.rddbc040_service import search_goods_full, get_goods_detail_full
from app.sims.nlq.master_source_limit import resolve_chat_source_limit
from app.ui.chat_middleware import push_sims_result_to_chat
from app.services.utils import apply_labels
from app.sims.views.master_advanced_filters import (
    build_master_query_condition,
    build_master_llm_summary,
)

_Q_PHYSIC = re.compile(r"(?:제품코드|상품코드)\s*([0-9A-Za-z]{3,})")
_Q_INSU = re.compile(r"(?:보험코드)\s*([0-9A-Za-z]{4,})")
_Q_BAR = re.compile(r"(?:바코드)\s*([0-9A-Za-z\-]{6,})")
_Q_NAME = re.compile(
    r"(?:제품명|상품명)\s*(?:은|는|이|가|을|를)?\s*"
    r"(.+?)"
    r"(?=\s*(?:제품|상품)?\s*(?:조회|검색|목록|찾아줘|찾아봐줘|찾아봐|찾아|보여줘|알려줘|해줘|$))"
)

_GROUP_LABEL_PATTERNS = r"(?:제품그룹명|제품그룹|품목그룹명|품목그룹|그룹명)"
_DI_LABEL_PATTERNS = r"(?:구분명|제품구분명|제품구분|구분)"
_CLASS_LABEL_PATTERNS = r"(?:제품분류명|제품분류|품목분류명|품목분류|분류명|카테고리)"

_Q_GROUP_NAME = re.compile(rf"{_GROUP_LABEL_PATTERNS}\s*([^\s,?.!]+)")
_Q_DI_NAME = re.compile(rf"{_DI_LABEL_PATTERNS}\s*([^\s,?.!]+)")
_Q_CLASS_NAME = re.compile(rf"{_CLASS_LABEL_PATTERNS}\s*([^\s,?.!]+)")
_Q_MOD_USER = re.compile(r"(?:수정자명|수정자|수정한사람|수정한 사람|변경자)\s*([^\s,?.!]+)")
_Q_UNIT_PRICE = re.compile(r"(?:단가)\s*([0-9][0-9,]*(?:\s*(?:~|-)\s*[0-9][0-9,]*)?)")
_Q_FINAL_PRICE_DATE = re.compile(r"(?:최종단가변경일자)\s*([0-9]{6,8}(?:\s*(?:~|-)\s*[0-9]{6,8})?)")


_GOODS_STOP = {
    "제품", "상품", "조회", "검색", "목록", "찾아", "찾아줘", "찾아봐",
    "보여줘", "알려줘", "있어", "있나", "존재", "등록", "데이터",
    "제약사", "제조사", "제품명", "상품명", "제품코드", "상품코드",
    "보험코드", "바코드",
    "제품그룹명", "제품그룹", "품목그룹명", "품목그룹", "그룹명",
    "구분명", "제품구분명", "제품구분", "구분",
    "제품분류명", "제품분류", "품목분류명", "품목분류", "분류명", "카테고리",
    "수정자", "수정자명", "수정일자",
    "단가", "최종단가변경일자",
}

_GOODS_LIST_PREFER_COLS = [
    "제품코드", "보험코드", "제품명", "제약사명",
    "제품그룹명", "구분명", "제품플래그명", "함량명", "제품분류명",
    "규격", "단위",
    "계산단위",
    "보험수가변경일자", "보험가격", "보험단가",
    "이전보험수가변경일자", "이전보험가격", "이전보험단가",
    "최종단가변경일자", "단가",
    "특수관리제품코드", "특수관리제품",
    "바코드1", "바코드2", "바코드3", "바코드4", "바코드5",
    "사용구분", "삭제/사용여부",
    "등록자", "등록일자", "수정자", "수정일자",
]

_GOODS_DETAIL_PREFER_COLS = [
    "제품코드", "보험코드", "제품명", "출력명", "약어명", "제약사명",
    "제품그룹명", "구분명", "제품플래그명", "함량명", "제품분류명",
    "규격", "단위",
    "계산단위",
    "보험수가변경일자", "보험가격", "보험단가",
    "이전보험수가변경일자", "이전보험가격", "이전보험단가",
    "최종단가변경일자", "단가",
    "특수관리제품코드", "특수관리제품",
    "바코드1", "바코드2", "바코드3", "바코드4", "바코드5",
    "사용구분", "삭제/사용여부",
    "등록자", "등록일자", "수정자", "수정일자",
]

def _ensure_df(obj: Any) -> pd.DataFrame:
    if obj is None:
        return pd.DataFrame()
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    try:
        return pd.DataFrame(obj)
    except Exception:
        return pd.DataFrame()


def _strip_tail_josa(s: str) -> str:
    s = (s or "").strip()
    for j in ("의", "은", "는", "이", "가", "을", "를", "과", "와", "에서", "으로", "로", "만"):
        if s.endswith(j) and len(s) > len(j):
            return s[:-len(j)].strip()
    return s


def _strip_tail_request_words(s: str) -> str:
    s = (s or "").strip()
    for _ in range(4):
        s2 = re.sub(r"(?:조회|검색|찾아줘|찾아봐줘|찾아봐|찾아|보여줘|알려줘|해줘)$", "", s).strip()
        if s2 == s:
            break
        s = s2
    return s


def _norm_kw(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", "", s)
    s = _strip_tail_request_words(s)
    s = _strip_tail_josa(s)
    return s.strip()


def _clean_goods_token(s: str) -> str:
    s = _norm_kw(s)
    if not s or s in _GOODS_STOP:
        return ""
    return s

def _clean_goods_value(s: str) -> str:
    """
    제품명/명칭 조건값 후처리.
    예:
    - '아티반 제품 조회' -> '아티반'
    - '완창가나다 제품 조회' -> '완창가나다'
    """
    s = str(s or "").strip()

    s = re.sub(
        r"\s*(?:제품|상품)?\s*"
        r"(?:조회|검색|목록|찾아줘|찾아봐줘|찾아봐|찾아|보여줘|알려줘|해줘|있어|있나|있는지|존재|여부|확인)\s*$",
        "",
        s,
    ).strip()

    s = re.sub(r"\s*(?:제품|상품)\s*$", "", s).strip()

    return _clean_goods_token(s)


def _extract_vendor_keyword(txt: str) -> str:
    m = re.search(r"(?:제약사명|제약사|제조사명|제조사)\s*([^\s,?.!]+)", txt)
    if m:
        v = _clean_goods_token(m.group(1) or "")
        if v:
            return v

    m = re.search(r"([^\s,?.!]+)\s*(?:제약사|제조사)", txt)
    if m:
        v = _clean_goods_token(m.group(1) or "")
        if v:
            return v

    return ""

def _extract_labeled_keyword(txt: str, label_patterns: str) -> str:
    m = re.search(rf"{label_patterns}\s*([^\s,?.!]+)", txt)
    if m:
        v = _clean_goods_token(m.group(1) or "")
        if v:
            return v

    m = re.search(rf"([^\s,?.!]+)\s*{label_patterns}", txt)
    if m:
        v = _clean_goods_token(m.group(1) or "")
        if v:
            return v

    return ""

def _extract_add_user_name(txt: str) -> str:
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
        s = _clean_goods_token(s)
        return s

    # 1) 명시 라벨형
    explicit_patterns = [
        r"(?:등록자명|등록자|작성자)\s*([^\s,?.!]+)",
        r"([^\s,?.!]+)\s*(?:등록자명|등록자|작성자)",
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

    # 2) 서술형
    action_patterns = [
        r"([^\s,?.!~\-]+?)(?:이|가)?\s*등록한\s*(?:제품|상품)",
        r"([^\s,?.!~\-]+?)(?:이|가)?\s*작성한\s*(?:제품|상품)",
        r"([^\s,?.!~\-]+?)(?:이|가)?\s*등록한\s*것",
        r"([^\s,?.!~\-]+?)(?:이|가)?\s*작성한\s*것",
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

    return ""


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
        s = re.sub(r"(?:인)$", "", s).strip()   # 예: 관리자인 -> 관리자
        s = _clean_goods_token(s)
        return s

    # 1) 명시 라벨형
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

    # 2) 서술형
    action_patterns = [
        r"([^\s,?.!~\-]+?)(?:이|가)?\s*수정한\s*(?:제품|상품)",
        r"([^\s,?.!~\-]+?)(?:이|가)?\s*변경한\s*(?:제품|상품)",
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

    return ""

def _date_token_to_yyyymmdd(token: str) -> str:
    """
    YYYY, YYYYMM, YYYYMMDD, YYYY-MM-DD, YYYY.MM.DD, YYYY/MM/DD 형태를 정리한다.
    """
    s = str(token or "").strip()
    digits = re.sub(r"\D", "", s)

    if len(digits) == 4:
        return digits
    if len(digits) == 6:
        return digits
    if len(digits) >= 8:
        return digits[:8]
    return ""


def _expand_date_range_token(a: str, b: str = "") -> tuple[str, str]:
    """
    2025      -> 202501 ~ 202512
    202501    -> 202501 ~ 202501
    20250101  -> 20250101 ~ 20250101
    """
    a = _date_token_to_yyyymmdd(a)
    b = _date_token_to_yyyymmdd(b)

    if a and not b:
        if len(a) == 4:
            return f"{a}01", f"{a}12"
        return a, a

    if a and b:
        if len(a) == 4 and len(b) == 4:
            return f"{a}01", f"{b}12"
        return a, b

    return "", ""
# '최종단가변경일자'는 등록일자 해석 대상에서 제외
def _extract_add_date_range(txt: str) -> tuple[str, str]:
    cleaned = str(txt or "")
    cleaned = cleaned.replace("최종단가변경일자", " ")

    if not any(k in cleaned for k in ("등록일자", "등록일", "등록", "작성일자", "작성일", "작성")):
        return "", ""

    # 라벨 뒤 날짜 범위: 등록일자 2025-01-01 ~ 2025-12-31
    m = re.search(
        r"(?:등록일자|등록일|작성일자|작성일)\s*"
        r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)"
        r"\s*(?:~|부터|에서|\-)\s*"
        r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)",
        cleaned,
    )
    if m:
        return _expand_date_range_token(m.group(1), m.group(2))

    # 날짜 범위 뒤 등록: 2025-01-01 ~ 2025-12-31 등록
    m = re.search(
        r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)"
        r"\s*(?:~|부터|에서|\-)\s*"
        r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)"
        r"\s*(?:등록|등록된|작성|작성된)",
        cleaned,
    )
    if m:
        return _expand_date_range_token(m.group(1), m.group(2))

    # 단일: 등록일자 2025 / 등록일자 202501 / 등록일자 2025-01-01
    m = re.search(
        r"(?:등록일자|등록일|작성일자|작성일)\s*"
        r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)",
        cleaned,
    )
    if m:
        return _expand_date_range_token(m.group(1))

    # 단일 날짜 뒤 등록
    m = re.search(
        r"([0-9]{4}(?:[-./]?[0-9]{2})?(?:[-./]?[0-9]{2})?)"
        r"\s*(?:등록|등록된|작성|작성된)",
        cleaned,
    )
    if m:
        return _expand_date_range_token(m.group(1))

    return "", ""

# '최종단가변경일자'는 수정일자 해석 대상에서 제외
def _extract_mod_date_range(txt: str) -> tuple[str, str]:
    cleaned = str(txt or "")
    cleaned = cleaned.replace("최종단가변경일자", " ")

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
        r"\s*(?:수정|수정된|변경|변경된)",
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
        r"\s*(?:수정|수정된|변경|변경된)",
        cleaned,
    )
    if m:
        return _expand_date_range_token(m.group(1))

    return "", ""

def _extract_unit_price_kw(txt: str) -> str:
    m = _Q_UNIT_PRICE.search(txt)
    if m:
        return str(m.group(1) or "").replace(" ", "").strip()
    return ""


def _extract_final_price_date_kw(txt: str) -> str:
    m = _Q_FINAL_PRICE_DATE.search(txt)
    if m:
        return str(m.group(1) or "").replace(" ", "").strip()
    return ""


def _extract_name_keyword(txt: str) -> str:
    # 구조화 속성 질의면 제품명으로 재해석하지 않음
    if any(
        k in txt
        for k in (
            "제품그룹명", "제품그룹", "품목그룹명", "품목그룹", "그룹명",
            "구분명", "제품구분명", "제품구분", "구분",
            "제품분류명", "제품분류", "품목분류명", "품목분류", "분류명", "카테고리",
            "등록자", "등록자명", "등록일자", "등록일",
            "등록한", "등록된", "작성자", "작성일자", "작성일", "작성한", "작성된",
            "수정자", "수정자명", "수정일자",
            "수정일", "수정", "변경일자", "변경일", "변경자",
            "단가", "최종단가변경일자",
        )
    ):
        return ""

    m = _Q_NAME.search(txt)
    if m:
        v = _clean_goods_value(m.group(1) or "")
        if v:
            return v

    if not any(k in txt for k in ("제약사", "제조사")):
        m = re.search(
            r"([^\s,?.!]+)\s*(?:제품|상품)(?:\s*(?:조회|검색|목록|보여줘|알려줘|찾아줘))?$",
            txt,
        )
        if m:
            v = _clean_goods_token(m.group(1) or "")
            if v:
                return v

    return ""


def _build_goods_display_df(df: pd.DataFrame, *, detail: bool = False) -> pd.DataFrame:
    work = _ensure_df(df)
    if work.empty:
        return work

    def _pick_col(df_src: pd.DataFrame, candidates: list[str]) -> str:
        for c in candidates:
            if c in df_src.columns:
                return c
        return ""

    def _norm_series(sr: pd.Series) -> pd.Series:
        return (
            sr.fillna("")
            .astype(str)
            .replace({"None": "", "nan": "", "<NA>": ""})
            .str.strip()
        )

    def _fmt_char8_date(sr: pd.Series) -> pd.Series:
        s = (
            sr.fillna("")
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
            .replace({
                "": None,
                "0": None,
                "00000000": None,
                "19000101": None,
                "20010101": None,
                "99999999": None,
                "None": None,
                "nan": None,
                "<NA>": None,
            })
        )
        dtv = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
        return dtv.dt.strftime("%Y-%m-%d").fillna("")

    def _fmt_datetime(sr: pd.Series) -> pd.Series:
        s = (
            sr.fillna("")
            .astype(str)
            .str.strip()
            .replace({
                "": None,
                "None": None,
                "nan": None,
                "<NA>": None,
            })
        )
        dtv = pd.to_datetime(s, errors="coerce")
        return dtv.dt.strftime("%Y-%m-%d %H:%M:%S").fillna("")

    out = work.copy()

    # 이름 컬럼 우선, 코드 컬럼은 fallback
    add_name_col = _pick_col(out, ["__raw_add_user_nm", "add_user_nm", "등록자명", "등록자"])
    add_code_col = _pick_col(out, ["등록자코드", "Rd04_Add_Cd"])
    mod_name_col = _pick_col(out, ["__raw_mod_user_nm", "mod_user_nm", "수정자명", "수정자"])
    mod_code_col = _pick_col(out, ["수정자코드", "Rd04_Mod_Cd"])

    if add_name_col:
        out["등록자"] = _norm_series(out[add_name_col])
    elif add_code_col:
        out["등록자"] = _norm_series(out[add_code_col])
    else:
        out["등록자"] = ""

    if mod_name_col:
        out["수정자"] = _norm_series(out[mod_name_col])
    elif mod_code_col:
        out["수정자"] = _norm_series(out[mod_code_col])
    else:
        out["수정자"] = ""

    if "등록일자" not in out.columns and "Rd04_Add_Date" in out.columns:
        out["등록일자"] = out["Rd04_Add_Date"]
    if "수정일자" not in out.columns and "Rd04_Mod_Date" in out.columns:
        out["수정일자"] = out["Rd04_Mod_Date"]
    for col in ["보험수가변경일자", "이전보험수가변경일자", "최종단가변경일자", "등록일자", "수정일자"]:
        if col in out.columns:
            out[col] = _fmt_char8_date(out[col])


    if "계산단위" in out.columns:
        num = pd.to_numeric(out["계산단위"], errors="coerce")
        non_na = num.dropna()
        if not non_na.empty and ((non_na % 1) == 0).all():
            out["계산단위"] = num.round(0).astype("Int64")
        else:
            out["계산단위"] = num.round(3)

    for col in ["보험가격", "보험단가", "이전보험가격", "이전보험단가", "단가"]:
        if col in out.columns:
            num = pd.to_numeric(out[col], errors="coerce").round(0)
            out[col] = num.astype("Int64")

    if detail:
        preferred = [
            "제품코드", "보험코드", "제품명", "출력명", "약어명", "제약사명",
            "제품그룹명", "구분명", "제품플래그명", "함량명", "제품분류명",
            "규격", "단위",
            "계산단위",
            "보험수가변경일자", "보험가격", "보험단가",
            "이전보험수가변경일자", "이전보험가격", "이전보험단가",
            "최종단가변경일자", "단가",
            "특수관리제품코드", "특수관리제품",
            "바코드1", "바코드2", "바코드3", "바코드4", "바코드5",
            "사용구분", "삭제/사용여부",
            "등록자", "등록일자", "수정자", "수정일자",
                ]
    else:
        preferred = [
            "제품코드", "보험코드", "제품명", "제약사명",
            "제품그룹명", "구분명", "제품플래그명", "함량명", "제품분류명",
            "규격", "단위",
            "계산단위",
            "보험수가변경일자", "보험가격", "보험단가",
            "이전보험수가변경일자", "이전보험가격", "이전보험단가",
            "최종단가변경일자", "단가",
            "특수관리제품코드", "특수관리제품",
            "바코드1", "바코드2", "바코드3", "바코드4", "바코드5",
            "사용구분", "삭제/사용여부",
            "등록자", "등록일자", "수정자", "수정일자",
        ]

    preferred = [c for c in preferred if c in out.columns]
    return out[preferred].copy() if preferred else out.copy()

def _build_goods_params(
    *,
    physic_cd: str = "",
    keyword: str = "",
    insu_cd: str = "",
    barcode: str = "",
    ven_nm_kw: str = "",
    group_name_kw: str = "",
    di_name_kw: str = "",
    physic_gu_name_kw: str = "",
    add_user_nm_kw: str = "",
    add_date_from: str = "",
    add_date_to: str = "",
    mod_user_nm_kw: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",
    unit_price_kw: str = "",
    final_price_date_kw: str = "",
    only_use: bool = True,
    top: int = 2000,
) -> Dict[str, Any]:
    return {
        "제품코드": physic_cd,
        "제품명": keyword,
        "보험코드": insu_cd,
        "바코드": barcode,
        "제약사명": ven_nm_kw,
        "제품그룹명": group_name_kw,
        "구분명": di_name_kw,
        "제품분류명": physic_gu_name_kw,
        "등록자": add_user_nm_kw,
        "등록일자": (
            add_date_from
            if (add_date_from and add_date_from == add_date_to)
            else f"{add_date_from}~{add_date_to}"
            if (add_date_from or add_date_to)
            else ""
        ),
        "수정자": mod_user_nm_kw,
        "수정일자": (
            mod_date_from
            if (mod_date_from and mod_date_from == mod_date_to)
            else f"{mod_date_from}~{mod_date_to}"
            if (mod_date_from or mod_date_to)
            else ""
        ),
        "단가": unit_price_kw,
        "최종단가변경일자": final_price_date_kw,
        "사용만": bool(only_use),
        "TopN": int(top),
    }

def _build_goods_query_summary(
    *,
    physic_cd: str = "",
    keyword: str = "",
    insu_cd: str = "",
    barcode: str = "",
    ven_nm_kw: str = "",
    group_name_kw: str = "",
    di_name_kw: str = "",
    physic_gu_name_kw: str = "",
    add_user_nm_kw: str = "",
    add_date_from: str = "",
    add_date_to: str = "",
    mod_user_nm_kw: str = "",
    mod_date_from: str = "",
    mod_date_to: str = "",
    unit_price_kw: str = "",
    final_price_date_kw: str = "",
) -> str:
    parts = []

    if physic_cd:
        parts.append(f"제품코드 {physic_cd}")
    if keyword:
        parts.append(f"제품명 {keyword}")
    if insu_cd:
        parts.append(f"보험코드 {insu_cd}")
    if barcode:
        parts.append(f"바코드 {barcode}")
    if ven_nm_kw:
        parts.append(f"제약사명 {ven_nm_kw}")
    if group_name_kw:
        parts.append(f"제품그룹명 {group_name_kw}")
    if di_name_kw:
        parts.append(f"구분명 {di_name_kw}")
    if physic_gu_name_kw:
        parts.append(f"제품분류명 {physic_gu_name_kw}")

    if add_user_nm_kw:
        parts.append(f"등록자 {add_user_nm_kw}")
    if add_date_from or add_date_to:
        if add_date_from and add_date_from == add_date_to:
            parts.append(f"등록일자 {add_date_from}")
        else:
            parts.append(f"등록일자 {add_date_from or ''}~{add_date_to or ''}")

    if mod_user_nm_kw:
        parts.append(f"수정자 {mod_user_nm_kw}")
    if mod_date_from or mod_date_to:
        if mod_date_from and mod_date_from == mod_date_to:
            parts.append(f"수정일자 {mod_date_from}")
        else:
            parts.append(f"수정일자 {mod_date_from or ''}~{mod_date_to or ''}")

    if unit_price_kw:
        parts.append(f"단가 {unit_price_kw}")
    if final_price_date_kw:
        parts.append(f"최종단가변경일자 {final_price_date_kw}")

    return " / ".join(parts)

def _build_goods_query_condition(params: Dict[str, Any], total: int, display_count: int) -> str:
    params2 = dict(params or {})

    # _build_goods_params()는 등록일자를 "202501~202512" 문자열로 넣으므로
    # 공용 date_range 조건 생성을 위해 From/To도 별도로 보강한다.
    if "등록일자From" not in params2:
        add_date = str(params2.get("등록일자") or "")
        if "~" in add_date:
            a, b = add_date.split("~", 1)
            params2["등록일자From"] = a.strip()
            params2["등록일자To"] = b.strip()
        elif add_date:
            params2["등록일자From"] = add_date.strip()
            params2["등록일자To"] = add_date.strip()

    if "수정일자From" not in params2:
        mod_date = str(params2.get("수정일자") or "")
        if "~" in mod_date:
            a, b = mod_date.split("~", 1)
            params2["수정일자From"] = a.strip()
            params2["수정일자To"] = b.strip()
        elif mod_date:
            params2["수정일자From"] = mod_date.strip()
            params2["수정일자To"] = mod_date.strip()

    field_specs = [
        ("text", "제품코드", "제품코드"),
        ("text", "제품명", "제품명"),
        ("text", "보험코드", "보험코드"),
        ("text", "바코드", "바코드"),
        ("text", "제약사명", "제약사명"),
        ("text", "제품그룹명", "제품그룹명"),
        ("text", "구분명", "구분명"),
        ("text", "제품분류명", "제품분류명"),
        ("text", "등록자", "등록자"),
        ("date_range", "등록일자", "등록일자From", "등록일자To"),
        ("text", "수정자", "수정자"),
        ("date_range", "수정일자", "수정일자From", "수정일자To"),
        ("text", "단가", "단가"),
        ("text", "최종단가변경일자", "최종단가변경일자"),
    ]

    return build_master_query_condition(
        params2,
        total=total,
        display_count=display_count,
        field_specs=field_specs,
        active_key="사용만",
        active_text="사용구분 사용중",
    )


def _split_condition_and_note(query_condition: str) -> tuple[str, str]:
    lines = [x.strip() for x in str(query_condition or "").splitlines() if x.strip()]
    if not lines:
        return "전체", ""
    return lines[0], "\n".join(lines[1:]).strip()


def _build_goods_master_llm_summary(
    df_all_display: pd.DataFrame,
    *,
    query_condition: str,
    total: int,
    display_count: int,
) -> dict[str, Any]:
    return build_master_llm_summary(
        df_all_display,
        master_name="제품마스터",
        query_condition=query_condition,
        total=total,
        display_count=display_count,
        count_specs=[
            ("제품그룹별 상위", "제품그룹명", "group_top"),
            ("제품구분별 상위", "구분명", "di_top"),
            ("제품플래그별 상위", "제품플래그명", "flag_top"),
            ("함량별 상위", "함량명", "cons_top"),
            ("제품분류별 상위", "제품분류명", "class_top"),
            ("제약사별 상위", "제약사명", "maker_top"),
            ("특수관리제품별 상위", "특수관리제품", "special_top"),
            ("사용구분별 상위", "사용구분", "use_top"),
            ("등록자별 상위", "등록자", "add_user_top"),
            ("수정자별 상위", "수정자", "mod_user_top"),
        ],
        year_specs=[
            ("등록연도별 상위", "등록일자", "add_year_top"),
            ("수정연도별 상위", "수정일자", "mod_year_top"),
        ],
        top_n=10,
        answer_rule="화면에는 일부만 표시될 수 있지만, 위 집계는 전체 조회결과 기준입니다.",
    )


# SIMS 결과 푸시 공통 함수
# SIMS 결과 푸시 시 일관된 메시지 포맷과 메타데이터를 포함하도록 함
# 결과 유형에 따라 텍스트 또는 테이블 형태로 결과를 포맷팅하여 푸시
def _push_goods_result(
    *,
    txt: str,
    title: str,
    action: str,
    df: pd.DataFrame,
    df_display: pd.DataFrame,
    params_out: Dict[str, Any],
    query_summary: str,
    source: str,
) -> bool:
    df_full = _ensure_df(df)
    df_display_all = _ensure_df(df_display)

    total = int(len(df_full))
    try:
        source_limit = int(params_out.get("TopN") or 0)
    except (TypeError, ValueError):
        source_limit = 0
    source_limit_hit = source_limit > 0 and total >= source_limit
    display_limit = 500
    df_display_limited = df_display_all.head(display_limit).copy()
    display_count = int(len(df_display_limited))

    query_condition = _build_goods_query_condition(params_out, total, display_count)
    condition_text, note = _split_condition_and_note(query_condition)

    goods_master_summary = _build_goods_master_llm_summary(
        df_display_all,
        query_condition=query_condition,
        total=total,
        display_count=display_count,
    )
    llm_summary_md = str(goods_master_summary.get("llm_summary_md") or "")

    result_message = f"{title} {total:,}건" if total > 0 else "해당 조회조건의 자료가 없습니다."
    summary_basis = "전체 조회결과 기준"
    if source_limit_hit:
        result_message = f"{title} {total:,}건 (조회 상한 도달: 전체 건수 미확인)"
        summary_basis = "조회 상한 내 결과 기준"

    result = {
        "final": True,
        "type": "table",
        "title": title,
        "action": action,
        "params": params_out,
        "df": df_full,
        "df_display": df_display_limited,
        "records": df_display_limited.to_dict(orient="records") if not df_display_limited.empty else [],
        "columns": list(df_display_limited.columns) if not df_display_limited.empty else [],
        "message": result_message,
        "meta": {
            "nlq": True,
            "master_nlq": True,
            "domain": "goods",
            "nlq_query": txt,
            "_force_push": True,
            "_nlq_nonce": str(uuid.uuid4()),

            "row_count": total,
            "row_count_total": total,
            "display_row_count": display_count,
            "show_n": display_count,
            "source_limit": source_limit,
            "source_limit_hit": source_limit_hit,

            "source": source,
            "query_summary": condition_text,
            "condition": condition_text,
            "summary_md": note,

            "analysis_type": "goods_master",
            "llm_summary_kind": "goods_master_summary",
            "llm_summary_md": llm_summary_md,
            "goods_master_summary": goods_master_summary,
            "analysis_row_count": total,
            "row_count_total_for_analysis": total,
            "summary_basis": summary_basis,
            "field_notes": (
                f"제품 마스터 분석은 {summary_basis} 집계요약을 우선 근거로 답합니다. "
                "화면 표시는 일부 행으로 제한될 수 있습니다."
            ),
        },
    }

    push_sims_result_to_chat(result, action)
    return True


# 텍스트 결과 푸시 공통 함수
# SIMS 결과 푸시 시 일관된 메시지 포맷과 메타데이터를 포함하도록 함
# 텍스트 결과는 주로 조회 결과가 없거나 간단한 메시지를 전달할 때 사용
def _push_goods_text(
    *,
    txt: str,
    title: str,
    action: str,
    params_out: Dict[str, Any],
    query_summary: str,
    source: str,
    message: str,
) -> bool:
    condition_text = str(query_summary or "").strip() or "전체"
    summary_line = f"조회조건: {condition_text}"
    note = "조회결과: 0건"
    display_message = f"{message}\n\n{summary_line}\n{note}"

    result = {
        "final": True,
        "type": "text",
        "title": title,
        "action": action,
        "params": params_out,
        "data": display_message,
        "message": display_message,
        "meta": {
            "nlq": True,
            "master_nlq": True,
            "domain": "goods",
            "nlq_query": txt,
            "_force_push": True,
            "_nlq_nonce": str(uuid.uuid4()),

            "row_count": 0,
            "row_count_total": 0,
            "display_row_count": 0,
            "show_n": 0,
            "source": source,
            "query_summary": condition_text,
            "condition": condition_text,
            "summary_md": note,
            "analysis_type": "goods_master",
            "summary_basis": "전체 조회결과 기준",

        },
    }

    push_sims_result_to_chat(result, action)
    return True

def try_handle_goods_nlq(
    txt: str,
    *,
    room: Dict[str, Any],
    session_state: Dict[str, Any],
    make_ts,
    next_seq,
    logger,
) -> bool:
    t = (txt or "").strip()
    if not t:
        return False


    def _apply_add_filters(df_src: pd.DataFrame, add_user_kw: str, add_from: str, add_to: str, *, detail: bool = False) -> pd.DataFrame:
        raw = _ensure_df(df_src)
        if raw.empty:
            return raw

        disp = _build_goods_display_df(raw, detail=detail)
        if disp.empty:
            return raw.iloc[0:0].copy()

        keep_mask = pd.Series(True, index=disp.index)

        if add_user_kw:
            target = re.sub(r"\s+", "", str(add_user_kw or "").strip())
            if "등록자" in disp.columns:
                vals = (
                    disp["등록자"]
                    .fillna("")
                    .astype(str)
                    .str.strip()
                    .str.replace(r"\s+", "", regex=True)
                )
                keep_mask &= vals.str.contains(re.escape(target), na=False)
            else:
                keep_mask &= False

        if add_from or add_to:
            if "등록일자" in disp.columns:
                digits = (
                    disp["등록일자"]
                    .fillna("")
                    .astype(str)
                    .str.strip()
                    .str.replace(r"\D", "", regex=True)
                )

                if add_from and add_to:
                    if len(add_from) == 6 and len(add_to) == 6:
                        ym = digits.str[:6]
                        keep_mask &= (ym >= add_from) & (ym <= add_to)
                    else:
                        keep_mask &= (digits >= add_from) & (digits <= add_to)
                else:
                    base = add_from or add_to
                    if len(base) == 6:
                        keep_mask &= digits.str[:6].eq(base)
                    else:
                        keep_mask &= digits.eq(base)
            else:
                keep_mask &= False

        keep_index = disp.index[keep_mask.fillna(False)]
        if len(keep_index) == 0:
            return raw.iloc[0:0].copy()

        return raw.loc[keep_index].copy()

    # SIMS 등록/존재 여부 질문도 제품명 조회로 흡수
    if any(k in t.lower() for k in ("sims",)) and any(k in t for k in ("데이터", "등록", "있어", "있나", "존재", "여부", "확인")):
        q = re.sub(r"[?？!.]", " ", t)
        q = re.sub(
            r"(sims|SIMS|데이터|등록|있어|있나|있는지|존재|여부|확인|알려줘|알려\s*줘|검색|조회)",
            " ",
            q,
            flags=re.IGNORECASE,
        )
        q = re.sub(r"\s+", " ", q).strip()
        keyword = _clean_goods_token(q)

        if 1 < len(keyword) <= 20:

            top = resolve_chat_source_limit(2000)
            df = search_goods_full(
                top=top,
                keyword=keyword,
                only_use=True,
                with_count=False,
                order_numeric=False,
            )

            if df is None:
                df = pd.DataFrame()
            elif not df.empty:
                df = apply_labels(df, "rddbc040")

            df_display = _build_goods_display_df(df, detail=False)
            params_out = _build_goods_params(keyword=keyword, only_use=True, top=top)
            query_summary = _build_goods_query_summary(keyword=keyword)

            if df_display.empty:
                return _push_goods_text(
                    txt=txt,
                    title="제품코드 목록 (0건)",
                    action="제품코드 목록",
                    params_out=params_out,
                    query_summary=query_summary,
                    source="제품코드마스터(Rddbc040)",
                    message="해당 조회조건의 자료가 없습니다.",
                )

            else:
                _push_goods_result(
                    txt=txt,
                    title="제품코드 목록",
                    action="제품코드 목록",
                    df=df,
                    df_display=df_display,
                    params_out=params_out,
                    query_summary=query_summary,
                    source="제품코드마스터(Rddbc040)",
                )

            logger.info("[nlq.goods] handled sims-exists keyword=%r rows=%s", keyword, len(df_display))
            return True

    if not any(
        k in t
        for k in (
            "제품", "상품", "보험", "바코드", "제약사", "제조사",
            "RDDBC040", "rddbc040", "제품코드", "보험코드",
            "제품그룹명", "제품그룹", "품목그룹명", "품목그룹", "그룹명",
            "구분명", "제품구분명", "제품구분",
            "제품분류명", "제품분류", "품목분류명", "품목분류", "분류명", "카테고리",
            "등록자", "등록자명", "등록일자", "등록일", "등록한", "등록된",
            "작성자", "작성일자", "작성일", "작성한", "작성된",
            "수정자", "수정자명", "수정일자",
            "단가", "최종단가변경일자",
        )
    ):
        return False

    physic_cd = ""
    insu_cd = ""
    barcode = ""
    ven_nm_kw = ""
    keyword = ""

    group_name_kw = ""
    di_name_kw = ""
    physic_gu_name_kw = ""

    add_user_nm_kw = ""
    add_date_from = ""
    add_date_to = ""

    mod_user_nm_kw = ""
    mod_date_from = ""
    mod_date_to = ""

    unit_price_kw = ""
    final_price_date_kw = ""

    m = _Q_PHYSIC.search(t)
    if m:
        physic_cd = _clean_goods_token(m.group(1) or "")

    m = _Q_INSU.search(t)
    if m:
        insu_cd = _clean_goods_token(m.group(1) or "")

    m = _Q_BAR.search(t)
    if m:
        barcode = _clean_goods_token(m.group(1) or "")

    ven_nm_kw = _extract_vendor_keyword(t)

    group_name_kw = _extract_labeled_keyword(t, _GROUP_LABEL_PATTERNS)
    di_name_kw = _extract_labeled_keyword(t, _DI_LABEL_PATTERNS)
    physic_gu_name_kw = _extract_labeled_keyword(t, _CLASS_LABEL_PATTERNS)

    add_user_nm_kw = _extract_add_user_name(t)
    add_date_from, add_date_to = _extract_add_date_range(t)

    mod_user_nm_kw = _extract_mod_user_name(t)
    mod_date_from, mod_date_to = _extract_mod_date_range(t)

    unit_price_kw = _extract_unit_price_kw(t)
    final_price_date_kw = _extract_final_price_date_kw(t)

    if (
        physic_cd or insu_cd or barcode
        or ven_nm_kw
        or group_name_kw or di_name_kw or physic_gu_name_kw
        or add_user_nm_kw or add_date_from or add_date_to
        or mod_user_nm_kw or mod_date_from or mod_date_to
        or unit_price_kw or final_price_date_kw
        or any(k in t for k in ("등록자", "등록자명", "등록일자", "등록일", "등록한", "등록된", "작성자", "작성일자", "작성일", "작성한", "작성된"))
    ):
        keyword = ""
    else:
        keyword = _extract_name_keyword(t)



    if not any([
        physic_cd, insu_cd, barcode, ven_nm_kw, keyword,
        group_name_kw, di_name_kw, physic_gu_name_kw,
        add_user_nm_kw, add_date_from, add_date_to,
        mod_user_nm_kw, mod_date_from, mod_date_to,
        unit_price_kw, final_price_date_kw,
    ]):
        residual = re.sub(
            r"(알려줘|조회|검색|찾아줘|찾아봐줘|찾아봐|찾아|보여줘|어떤|있어|있는지|목록|"
            r"제약사|제조사|제품|상품|보험코드|제품코드|바코드|"
            r"제품그룹명|제품그룹|품목그룹명|품목그룹|그룹명|"
            r"구분명|제품구분명|제품구분|구분|"
            r"제품분류명|제품분류|품목분류명|품목분류|분류명|카테고리|"
            r"등록자명|등록자|등록일자|등록일|등록한|등록된|"
            r"작성자|작성일자|작성일|작성한|작성된|"
            r"수정자명|수정자|수정일자|단가|최종단가변경일자)",
            " ",
            t,
        )
        residual = re.sub(r"\s+", " ", residual).strip()
        keyword = _clean_goods_token(residual)

    only_use = True
    if unit_price_kw or final_price_date_kw:
        only_use = False

    action_cap = None
    if add_user_nm_kw or add_date_from or add_date_to:
        # 등록자/등록일자는 서비스 SQL 조건으로 전달한다.
        top = 2000

    elif group_name_kw or di_name_kw or physic_gu_name_kw:
        # 제품그룹/구분/분류 wide filter의 기존 성능 보호 상한.
        top = 1000
        action_cap = 1000

    elif mod_user_nm_kw or mod_date_from or mod_date_to:
        # 수정자/수정일자 wide filter의 기존 성능 보호 상한.
        top = 1000
        action_cap = 1000
    else:
        top = 2000

    top = resolve_chat_source_limit(top, action_cap=action_cap)

    params_out = _build_goods_params(
        physic_cd=physic_cd,
        keyword=keyword,
        insu_cd=insu_cd,
        barcode=barcode,
        ven_nm_kw=ven_nm_kw,
        group_name_kw=group_name_kw,
        di_name_kw=di_name_kw,
        physic_gu_name_kw=physic_gu_name_kw,
        add_user_nm_kw=add_user_nm_kw,
        add_date_from=add_date_from,
        add_date_to=add_date_to,
        mod_user_nm_kw=mod_user_nm_kw,
        mod_date_from=mod_date_from,
        mod_date_to=mod_date_to,
        only_use=only_use,
        unit_price_kw=unit_price_kw,
        final_price_date_kw=final_price_date_kw,
        top=top,
    )
    query_summary = _build_goods_query_summary(
        physic_cd=physic_cd,
        keyword=keyword,
        insu_cd=insu_cd,
        barcode=barcode,
        ven_nm_kw=ven_nm_kw,
        group_name_kw=group_name_kw,
        di_name_kw=di_name_kw,
        physic_gu_name_kw=physic_gu_name_kw,
        add_user_nm_kw=add_user_nm_kw,
        add_date_from=add_date_from,
        add_date_to=add_date_to,
        mod_user_nm_kw=mod_user_nm_kw,
        mod_date_from=mod_date_from,
        mod_date_to=mod_date_to,
        unit_price_kw=unit_price_kw,
        final_price_date_kw=final_price_date_kw,
    )
    if physic_cd and len(physic_cd) <= 6:
        df = get_goods_detail_full(physic_cd=physic_cd)
        if df is None:
            df = pd.DataFrame()
        elif not df.empty:
            df = apply_labels(df, "rddbc040")

        df = _apply_add_filters(df, add_user_nm_kw, add_date_from, add_date_to, detail=True)
        df_display = _build_goods_display_df(df, detail=True)

        if df_display.empty:
        
            return _push_goods_text(
                txt=txt,
                title="제품코드 목록 (0건)",
                action="제품코드 목록",
                params_out=params_out,
                query_summary=query_summary,
                source="제품코드마스터(Rddbc040)",
                message="해당 조회조건의 자료가 없습니다.",
            )        
        else:
            _push_goods_result(
                txt=txt,
                title=f"제품코드 상세 ({physic_cd})",
                action=f"제품코드 상세 ({physic_cd})",
                df=df,
                df_display=df_display,
                params_out=params_out,
                query_summary=query_summary or f"제품코드 {physic_cd}",
                source="제품코드마스터(Rddbc040)",
            )

        logger.info("[nlq.goods] handled physic_cd=%r rows=%s", physic_cd, len(df_display))
        return True

    df = search_goods_full(
        top=top,
        keyword=keyword,
        physic_cd=physic_cd,
        insu_cd=insu_cd,
        barcode=barcode,
        ven_nm_kw=ven_nm_kw,
        group_name_kw=group_name_kw,
        di_name_kw=di_name_kw,
        physic_gu_name_kw=physic_gu_name_kw,
        add_user_nm_kw=add_user_nm_kw,
        add_date_from=add_date_from,
        add_date_to=add_date_to,
        mod_user_nm_kw=mod_user_nm_kw,
        mod_date_from=mod_date_from,
        mod_date_to=mod_date_to,
        only_use=only_use,
        unit_price_kw=unit_price_kw,
        final_price_date_kw=final_price_date_kw,
        with_count=False,
        order_numeric=False,
    )

    if df is None:
        df = pd.DataFrame()
    elif not df.empty:
        df = apply_labels(df, "rddbc040")

    df = _apply_add_filters(df, add_user_nm_kw, add_date_from, add_date_to, detail=False)
    df_display = _build_goods_display_df(df, detail=False)

    if df_display.empty:
        return _push_goods_text(
            txt=txt,
            title="제품코드 목록 (0건)",
            action="제품코드 목록",
            params_out=params_out,
            query_summary=query_summary,
            source="제품코드마스터(Rddbc040)",
            message="해당 조회조건의 자료가 없습니다.",
        )
    else:
        _push_goods_result(
            txt=txt,
            title="제품코드 목록",
            action="제품코드 목록",
            df=df,
            df_display=df_display,
            params_out=params_out,
            query_summary=query_summary,
            source="제품코드마스터(Rddbc040)",
        )
        logger.info("[nlq.goods] handled list rows=%s", len(df_display))
        return True
