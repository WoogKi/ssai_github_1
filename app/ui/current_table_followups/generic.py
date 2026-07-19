# app/ui/current_table_followups/generic.py
# 현재표 generic/마스터류 후속분석
# Create 2026-06-17

from __future__ import annotations

import re
from typing import Any, Callable

import pandas as pd


def _compact(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "").strip())


def _clean_group_value(v: Any) -> str:
    try:
        if pd.isna(v):
            return "(미지정)"
    except Exception:
        pass
    s = str(v or "").strip()
    if s in {"", "None", "none", "NONE", "nan", "NaN", "NAN", "<NA>", "NaT", "NULL", "null"}:
        return "(미지정)"
    return s




def _norm_col_name(value: Any) -> str:
    """컬럼명 비교용 정규화: 공백/구분자 제거, 영문 소문자."""
    return re.sub(r"[\s_\-./()\[\]{}·　]+", "", str(value or "").strip()).lower()



def _norm_numeric_query(value: Any) -> str:
    """숫자 조건 비교용 정규화: 컬럼명 매칭용 구분자는 제거하되 소수점은 보존한다."""
    return re.sub(r"[\s_\-/()\[\]{}·　]+", "", str(value or "").strip()).lower()

def _find_col_loose(
    df: pd.DataFrame,
    *,
    exact: tuple[str, ...] = (),
    include_any: tuple[str, ...] = (),
    include_all: tuple[str, ...] = (),
    exclude_any: tuple[str, ...] = (),
) -> str:
    """
    현재표 마스터류 컬럼 탐색 보강.

    기존 helpers['find_col']이 못 잡는 경우를 대비해
    공백/전각공백/괄호/언더스코어 차이를 제거한 뒤 다시 찾는다.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return ""

    cols = [str(c) for c in df.columns]
    norm_pairs = [(_norm_col_name(c), c) for c in cols]

    # 1) 정규화 exact
    exact_norms = [_norm_col_name(x) for x in exact if str(x or "").strip()]
    for target in exact_norms:
        for cn, c in norm_pairs:
            if cn == target:
                return c

    exclude_norms = [_norm_col_name(x) for x in exclude_any if str(x or "").strip()]
    include_all_norms = [_norm_col_name(x) for x in include_all if str(x or "").strip()]
    include_any_norms = [_norm_col_name(x) for x in include_any if str(x or "").strip()]

    # 2) include_all 우선
    if include_all_norms:
        for cn, c in norm_pairs:
            if exclude_norms and any(x in cn for x in exclude_norms):
                continue
            if all(x in cn for x in include_all_norms):
                return c

    # 3) include_any
    if include_any_norms:
        for cn, c in norm_pairs:
            if exclude_norms and any(x in cn for x in exclude_norms):
                continue
            if any(x in cn for x in include_any_norms):
                return c

    return ""


def _find_col_with_helper(
    df: pd.DataFrame,
    find_col: Callable[..., str],
    *,
    exact: tuple[str, ...] = (),
    include_any: tuple[str, ...] = (),
    include_all: tuple[str, ...] = (),
    exclude_any: tuple[str, ...] = (),
) -> str:
    """공통 find_col 호출 후 실패하면 loose finder로 재시도."""
    col = ""
    try:
        col = find_col(
            df,
            exact=exact,
            include_any=include_any,
            exclude_any=exclude_any,
        )
    except Exception:
        col = ""

    if col:
        return col

    return _find_col_loose(
        df,
        exact=exact,
        include_any=include_any,
        include_all=include_all,
        exclude_any=exclude_any,
    )

def _distinct_count_summary(
    df: pd.DataFrame,
    *,
    group_col: str,
    distinct_col: str | None,
    count_label: str,
) -> pd.DataFrame:
    """
    마스터류 현재표 후속분석용 group count.

    - 거래처/제품/사용자 마스터는 금액·수량 컬럼이 없을 수 있으므로
      건수/고유건수 중심으로 집계한다.
    - distinct_col이 있으면 고유건수, 없으면 행수 기준이다.
    """
    if not isinstance(df, pd.DataFrame) or df.empty or group_col not in df.columns:
        return pd.DataFrame()

    work = df.copy()
    work[group_col] = work[group_col].map(_clean_group_value)

    g = work.groupby(group_col, dropna=False)

    out = pd.DataFrame({
        group_col: g.size().index.astype(str),
        "행수": g.size().values,
    })

    if distinct_col and distinct_col in work.columns:
        out[count_label] = g[distinct_col].nunique(dropna=True).values
    else:
        out[count_label] = out["행수"]

    out = out.sort_values([count_label, "행수", group_col], ascending=[False, False, True]).reset_index(drop=True)
    out.insert(0, "순번", range(1, len(out) + 1))
    return out


def _apply_top_if_requested(out: pd.DataFrame, compact_query: str, top_n: int) -> pd.DataFrame:
    if not isinstance(out, pd.DataFrame) or out.empty:
        return out
    if top_n and any(w in compact_query for w in ("TOP", "top", "상위")):
        out = out.head(int(top_n)).copy()
        if "순번" in out.columns:
            out["순번"] = range(1, len(out) + 1)
    return out


def _main_columns_first(out: pd.DataFrame, cols: tuple[str, ...]) -> pd.DataFrame:
    if not isinstance(out, pd.DataFrame) or out.empty:
        return out
    front = [c for c in cols if c in out.columns]
    return out[front + [c for c in out.columns if c not in front]]


# ---------------------------------------------------------------------
# 현재표 공통 컬럼 필터
# ---------------------------------------------------------------------
_COMMON_FILTER_DETAIL_WORDS = (
    "상세", "상세히", "상세하게", "상세표",
    "목록", "리스트", "보여", "보여줘", "보여주세요",
    "조회", "검색", "찾아", "찾아줘", "필터", "추출", "걸러", "만",
)

_COMMON_FILTER_VALUE_SUFFIXES = (
    "상세하게보여주세요", "상세하게보여줘", "상세히보여주세요", "상세히보여줘",
    "상세표만들어줘", "상세표만들어", "목록으로보여줘", "목록보여줘",
    "리스트보여줘", "보여주세요", "보여줘요", "보여줘", "알려주세요", "알려줘",
    "조회해주세요", "조회해줘", "찾아주세요", "찾아줘", "검색해주세요", "검색해줘",
    "상세하게", "상세히", "상세표", "상세", "목록", "리스트", "필터", "추출", "걸러줘",
    "보여", "조회", "검색", "찾아", "알려", "으로", "만", "인것", "인거", "인자료", "인데이터",
)

_COMMON_FILTER_SKIP_COLUMNS = {
    "순번", "행수", "건수", "품목수", "제품수", "거래처수", "매입처수", "재고적용처수",
    "집계건수", "총집계건수", "row_count", "rows",
}


def _normalize_common_filter_query(query: str) -> str:
    t = str(query or "").strip()
    if not t:
        return ""
    t = re.sub(r"현\s*제\s*표", "현재표", t)
    t = re.sub(r"현재\s*표", "현재표", t)
    return re.sub(r"\s+", " ", t).strip()


def _common_filter_body(query: str) -> str:
    compact_query = _compact(_normalize_common_filter_query(query))
    for anchor in ("현재표", "현재조회결과", "현재결과"):
        compact_query = compact_query.replace(anchor, "")
    return compact_query


def _has_common_filter_intent(query: str) -> bool:
    normalized = _normalize_common_filter_query(query)
    compact_query = _compact(normalized)
    if not any(anchor in compact_query for anchor in ("현재표", "현재조회결과", "현재결과")):
        # dispatcher가 현재표 질문만 태우지만, 방어적으로 앵커를 확인한다.
        return False
    body = _common_filter_body(normalized)
    if any(w in body for w in _COMMON_FILTER_DETAIL_WORDS):
        return True
    return bool(re.search(r"\d", body)) and any(
        w in body
        for w in ("이상", "이하", "초과", "미만", "같다", "동일", "다르다", ">=", "<=", ">", "<", "==", "!=")
    )


def _column_filter_aliases(col: str) -> list[str]:
    """현재표 컬럼 필터용 컬럼명 alias 목록."""
    raw = str(col or "").strip()
    norm = _norm_col_name(raw)
    aliases: list[str] = []

    def _add(value: str) -> None:
        v = _norm_col_name(value)
        if len(v) >= 2 and v not in aliases:
            aliases.append(v)

    _add(norm)

    # '제조사명 한미', '거래처명 대학약국'이 기본이다.
    # 다만 사용자가 '제조사 한미', '거래처 대학약국'처럼 '명'을 빼는 경우를 보조한다.
    if raw.endswith("명") and len(raw) >= 3:
        _add(raw[:-1])

    # 코드 컬럼은 값 필터 대상이 될 수 있으므로 '코드' 제거 alias는 만들지 않는다.
    # '제품명'의 alias '제품'은 너무 넓지만, 현재표 필터 질문에서는 유용하다.
    if raw in {"제품명", "품목명", "상품명"}:
        _add("제품")
        _add("품목")
        _add("상품")
    if raw == "규격":
        _add("제품규격")

    alias_map = {
        "제약사명": ("제약사", "제조사", "제조사명"),
        "제조사명": ("제조사", "제약사", "제약사명"),
        "추세판정": ("판정", "추세", "추세판정"),
        "분석자료원": ("자료원", "분석자료원"),
        "기간구분": ("기간", "기간구분"),
        "당월 진척률": ("당월진척률", "진척률"),
        "평가월 진척률": ("평가월진척률", "달성률", "진척률"),
        "총매출액": ("매출액", "총매출", "매출"),
        "부족예상수량": ("부족제품수", "부족수량", "제품부족수량", "부족예상수량"),
        "배정부족예상수량": ("부족제품수", "부족수량", "제품부족수량", "부족예상수량"),
    }
    for extra in alias_map.get(raw, ()):
        _add(extra)

    # 숫자 조건에서는 사용자가 "현재재고수량" 대신 "재고수량"처럼
    # 업무식 짧은 명칭을 쓰는 경우가 많다. 단, 너무 과한 alias는 오탐을 만들 수 있으므로
    # 자주 쓰는 접두어만 보조 alias로 둔다.
    for prefix in ("현재", "기말", "실", "장부", "총"):
        if raw.startswith(prefix) and len(raw) > len(prefix) + 1:
            _add(raw[len(prefix):])

    return aliases


def _first_series_for_column(df: pd.DataFrame, col: str) -> pd.Series:
    """중복 컬럼명이 있어도 첫 번째 실제 컬럼 Series만 사용한다."""
    try:
        if not isinstance(df, pd.DataFrame) or col not in df.columns:
            return pd.Series(dtype="object")
        idx = list(df.columns).index(col)
        sr = df.iloc[:, idx]
        if isinstance(sr, pd.Series):
            return sr
    except Exception:
        pass
    try:
        value = df[col]
        if isinstance(value, pd.DataFrame):
            return value.iloc[:, 0]
        return value
    except Exception:
        return pd.Series(dtype="object")


def _strip_common_filter_value(value: str) -> str:
    """컬럼명 뒤에 붙은 값에서 실행어/조사/구분자를 제거한다."""
    v = _norm_col_name(value)
    if not v:
        return ""

    v = re.sub(r"^[=:：은는이가을를의]+", "", v).strip()

    changed = True
    while changed and v:
        changed = False
        for suffix in sorted(_COMMON_FILTER_VALUE_SUFFIXES, key=len, reverse=True):
            sx = _norm_col_name(suffix)
            if sx and v.endswith(sx):
                v = v[: -len(sx)].strip()
                changed = True
                break

    v = re.sub(r"^[=:：은는이가을를의]+", "", v).strip()
    v = re.sub(r"[?？!！,.。]+$", "", v).strip()
    return v


def _looks_like_numeric_condition_value(value: str) -> bool:
    """문자 필터가 숫자 비교 조건을 가로채지 않게 판정한다."""
    v = _compact(value)
    if any(w in v for w in ("음수", "마이너스")):
        return True
    if not re.search(r"\d", v):
        return False
    return any(w in v for w in ("이상", "이하", "초과", "미만", "크", "작", "많", "적", "보다", ">", "<", "="))


def _num_text_to_float(raw: str) -> tuple[float, str]:
    """
    숫자 조건의 기준값을 float로 변환한다.

    지원 예:
    - 0, 100, 1.5
    - 100만원 -> 1,000,000
    - 1억원   -> 100,000,000
    - 10만    -> 100,000

    수량/월수 단위(개, 정, 병, 건, 월, %)는 배율을 적용하지 않는다.
    """
    src = str(raw or "").strip()
    compact = src.replace(",", "")

    m = re.search(r"(-?\d+(?:\.\d+)?)", compact)
    if not m:
        return 0.0, "0"

    num_txt = m.group(1)
    try:
        value = float(num_txt)
    except Exception:
        value = 0.0

    tail = compact[m.end():]
    multiplier = 1.0

    # 금액성 단위만 배율 처리한다. '개월/월/건/개/%'는 값 그대로 둔다.
    if tail.startswith("억원") or tail.startswith("억"):
        multiplier = 100_000_000.0
    elif tail.startswith("천만원"):
        multiplier = 10_000_000.0
    elif tail.startswith("백만원"):
        multiplier = 1_000_000.0
    elif tail.startswith("십만원"):
        multiplier = 100_000.0
    elif tail.startswith("만원") or tail.startswith("만"):
        multiplier = 10_000.0
    elif tail.startswith("천원"):
        multiplier = 1_000.0

    threshold = value * multiplier

    unit_match = re.match(r"(억원|억|천만원|백만원|십만원|만원|만|천원|원|개|건|정|병|매|박스|box|BOX|월|개월|%|퍼센트)?", tail)
    unit = unit_match.group(1) if unit_match else ""
    label = f"{num_txt}{unit or ''}"
    return threshold, label


def _common_numeric_filter_op(text: str) -> tuple[str, float, str, str]:
    """
    공통 현재표 숫자 조건 해석.

    반환: (operator, threshold, operator_label, threshold_label)
    """
    compact = str(text or "").replace(" ", "")

    if any(w in compact for w in ("음수", "마이너스")):
        return "<", 0.0, "미만", "0"

    threshold, threshold_label = _num_text_to_float(compact)

    # >=, <= 같은 기호는 정규화 과정에서 사라지지 않게 원문 compact를 본다.
    if any(w in compact for w in ("이상", "크거나같", ">=")):
        return ">=", threshold, "이상", threshold_label

    if any(w in compact for w in ("초과", "보다큰", ">")):
        return ">", threshold, "초과", threshold_label

    if any(w in compact for w in ("이하", "작거나같", "<=")):
        return "<=", threshold, "이하", threshold_label

    if any(w in compact for w in ("미만", "보다작", "<")):
        return "<", threshold, "미만", threshold_label

    if any(w in compact for w in ("다르다", "다른", "!=", "<>")):
        return "!=", threshold, "다름", threshold_label

    if any(w in compact for w in ("같은", "같음", "동일", "인것", "인거", "=", "==")):
        return "==", threshold, "같음", threshold_label

    return "", threshold, "", threshold_label


def _find_common_numeric_filter(df: pd.DataFrame, query: str) -> tuple[str, str, float, str, str]:
    """
    '현재표 <숫자컬럼명> <숫자> 이상/이하/초과/미만' 형태를 찾아
    (컬럼, op, threshold, op_label, threshold_label)을 반환한다.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return "", "", 0.0, "", ""
    if not _has_common_filter_intent(query):
        return "", "", 0.0, "", ""

    q_norm = _norm_numeric_query(query)
    if not q_norm:
        return "", "", 0.0, "", ""

    candidates: list[tuple[int, str, str, int, str]] = []
    for col in [str(c) for c in df.columns]:
        if col in _COMMON_FILTER_SKIP_COLUMNS:
            continue

        for alias in _column_filter_aliases(col):
            pos = q_norm.find(alias)
            if pos < 0:
                continue
            tail = q_norm[pos + len(alias):]
            if tail.startswith("별"):
                continue
            op, threshold, op_label, threshold_label = _common_numeric_filter_op(tail)
            if not op:
                continue
            candidates.append((len(alias), col, alias, pos, tail))

    if not candidates:
        return "", "", 0.0, "", ""

    # 긴 컬럼명/alias 우선. 예: 1개월부족수량 > 부족수량 > 수량
    candidates.sort(key=lambda x: (x[0], -x[3]), reverse=True)

    for _, col, _alias, _pos, tail in candidates:
        op, threshold, op_label, threshold_label = _common_numeric_filter_op(tail)
        if op:
            return col, op, threshold, op_label, threshold_label

    return "", "", 0.0, "", ""


def _to_numeric_for_common_filter(sr: pd.Series) -> pd.Series:
    """현재표 숫자 조건 필터용 숫자 변환."""
    try:
        if pd.api.types.is_numeric_dtype(sr):
            return pd.to_numeric(sr, errors="coerce").fillna(0)
    except Exception:
        pass

    try:
        text = (
            sr.fillna("")
            .astype(str)
            .str.replace(",", "", regex=False)
            .str.replace("−", "-", regex=False)
            .str.replace("△", "-", regex=False)
            .str.replace("▲", "-", regex=False)
            .str.strip()
        )
        # 괄호 음수: (123) -> -123
        text = text.str.replace(r"^\(([-+]?[0-9.]+)\)$", r"-\1", regex=True)
        text = text.str.replace(r"[^0-9.\-+]", "", regex=True)
        return pd.to_numeric(text, errors="coerce").fillna(0)
    except Exception:
        return pd.Series([0.0] * len(sr), index=sr.index, dtype="float64")


def _apply_numeric_mask(nums: pd.Series, op: str, threshold: float) -> pd.Series:
    if op == ">=":
        return nums >= threshold
    if op == ">":
        return nums > threshold
    if op == "<=":
        return nums <= threshold
    if op == "<":
        return nums < threshold
    if op == "==":
        return nums == threshold
    if op == "!=":
        return nums != threshold
    return pd.Series([False] * len(nums), index=nums.index, dtype="bool")


def _fmt_threshold(value: float, label: str = "") -> str:
    label = str(label or "").strip()
    if label and label != "0":
        return label
    try:
        if abs(float(value) - int(float(value))) < 1e-9:
            return f"{int(float(value))}"
    except Exception:
        pass
    return f"{value:g}"


def _reorder_numeric_filter_columns(out: pd.DataFrame, filter_col: str) -> pd.DataFrame:
    if not isinstance(out, pd.DataFrame) or out.empty:
        return out
    front_candidates = [
        "제품코드", "제품명", "규격", "제조사코드", "제조사명",
        "제품그룹명", "제품구분명", "제품분류명",
        "거래처코드", "거래처명", "매입처코드", "매입처명",
        "재고기준", "재고년월", "재고위치", "재고위치명",
        filter_col,
    ]
    front: list[str] = []
    for c in front_candidates:
        if c in out.columns and c not in front:
            front.append(c)
    rest = [c for c in out.columns if c not in front]
    return out.loc[:, front + rest]


def _find_common_column_filter(df: pd.DataFrame, query: str) -> tuple[str, str]:
    """
    '현재표 <컬럼명> <값> 상세히 보여줘' 형태를 찾아
    (실제 컬럼명, 정규화된 필터값)을 반환한다.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return "", ""
    if not _has_common_filter_intent(query):
        return "", ""

    q_norm = _norm_col_name(query)
    if not q_norm:
        return "", ""

    candidates: list[tuple[int, str, str, int]] = []
    for col in [str(c) for c in df.columns]:
        if col in _COMMON_FILTER_SKIP_COLUMNS:
            continue

        for alias in _column_filter_aliases(col):
            pos = q_norm.find(alias)
            if pos < 0:
                continue

            # '<컬럼명>별 요약/분석'은 필터가 아니라 그룹 집계다.
            tail = q_norm[pos + len(alias):]
            if tail.startswith("별"):
                continue

            candidates.append((len(alias), col, alias, pos))

    if not candidates:
        return "", ""

    # 긴 컬럼명/alias를 우선한다. 예: 제품구분명 > 제품명 > 제품
    candidates.sort(key=lambda x: (x[0], -x[3]), reverse=True)

    for _, col, alias, pos in candidates:
        tail = q_norm[pos + len(alias):]
        value = _strip_common_filter_value(tail)
        if not value:
            continue
        if _looks_like_numeric_condition_value(value):
            continue
        if value in {"전체", "전부", "모두", "자료", "데이터"}:
            continue
        return col, value

    return "", ""


def _series_compact_for_filter(sr: pd.Series) -> pd.Series:
    try:
        return sr.fillna("").astype(str).map(_norm_col_name)
    except Exception:
        return pd.Series([""] * len(sr), index=sr.index, dtype="object")


def _display_filter_value(value: str) -> str:
    return str(value or "").strip() or "(빈값)"


def _available_common_filter_columns(df: pd.DataFrame, limit: int = 40) -> list[str]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []

    available: list[str] = []
    for col in [str(c) for c in df.columns]:
        if col in _COMMON_FILTER_SKIP_COLUMNS:
            continue
        if col not in available:
            available.append(col)
        if len(available) >= limit:
            break
    return available


def _extract_common_filter_candidate(query: str) -> str:
    if not _has_common_filter_intent(query):
        return ""

    body = _common_filter_body(query)
    if not body:
        return ""

    for suffix in sorted(_COMMON_FILTER_VALUE_SUFFIXES + _COMMON_FILTER_DETAIL_WORDS, key=len, reverse=True):
        sx = _norm_col_name(suffix)
        if sx and body.endswith(sx):
            body = body[: -len(sx)].strip()

    known_names = (
        "제조사명", "제조사",
        "제품그룹명", "제품그룹",
        "제품구분명", "제품구분",
        "제품분류명", "제품분류",
        "매입처명", "매입처",
        "거래처명", "거래처",
        "재고적용처명", "재고적용처",
        "재고위치명", "재고위치",
        "매출구분명", "매출구분",
        "제품코드", "품목코드", "제조사코드", "거래처코드",
        "제품명", "품목명", "상품명", "제품", "품목", "상품",
        "규격",
    )
    for name in known_names:
        n = _norm_col_name(name)
        if n and body.startswith(n):
            return name

    m = re.match(r"([가-힣A-Za-z0-9_]+)", body)
    return m.group(1) if m else ""


def _add_seq_column(out: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(out, pd.DataFrame):
        return out
    out = out.copy().reset_index(drop=True)
    if "순번" in out.columns:
        out = out.drop(columns=["순번"])
    out.insert(0, "순번", range(1, len(out) + 1))
    return out


def _drop_current_followup_detail_attrs(out: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(out, pd.DataFrame):
        return out
    for key in ("supplier_detail_key", "supplier_detail_rows", "excel_sheet_names"):
        try:
            out.attrs.pop(key, None)
        except Exception:
            pass
    return out


def _find_common_top_numeric_column(df: pd.DataFrame, query: str) -> str:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return ""
    if not any(w in _compact(query) for w in ("TOP", "top", "상위")):
        return ""

    body_norm = _norm_col_name(query)
    body_norm = re.sub(r"(top|상위)\d*", "", body_norm, flags=re.IGNORECASE)
    body_norm = re.sub(r"\d+", "", body_norm)
    if not body_norm:
        return ""

    candidates: list[tuple[int, str]] = []
    for col in [str(c) for c in df.columns]:
        if col in _COMMON_FILTER_SKIP_COLUMNS:
            continue
        nums = _to_numeric_for_common_filter(_first_series_for_column(df, col))
        if int(nums.notna().sum()) <= 0:
            continue
        for alias in _column_filter_aliases(col):
            alias_norm = _norm_col_name(alias)
            if alias_norm and alias_norm in body_norm:
                candidates.append((len(alias_norm), col))
                break

    if not candidates:
        return ""
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


# ---------------------------------------------------------------------
# 현재표 공통 컬럼 집계
# ---------------------------------------------------------------------
_COMMON_GROUP_WORDS = ("별", "기준", "집계", "분석", "현황", "요약")
_COMMON_GROUP_SUM_INCLUDE = (
    "공급가액",
    "세액",
    "총매출액",
    "매출액",
    "금액",
    "수량",
    "집계건수",
    "출고건수",
    "예상매출",
    "잔여예상",
)
_COMMON_GROUP_SUM_EXCLUDE = (
    "진척률",
    "달성률",
    "증감률",
    "평균",
    "단가",
    "완료월수",
    "커버월수",
    "비율",
)


def _normalize_common_group_query(query: str) -> str:
    t = _normalize_common_filter_query(query)
    return re.sub(r"\s+", "", t)


def _has_common_group_intent(query: str) -> bool:
    compact = _normalize_common_group_query(query)
    if not any(anchor in compact for anchor in ("현재표", "현재조회결과", "현재결과")):
        return False
    body = _common_filter_body(query)
    return any(w in body for w in ("별", "집계", "분석", "현황", "요약"))


def _group_aliases(col: str) -> list[str]:
    aliases = _column_filter_aliases(col)
    raw = str(col or "").strip()
    extra = {
        "제약사명": ("제약사", "제조사", "제조사명"),
        "제조사명": ("제조사", "제약사", "제약사명"),
        "추세판정": ("추세", "판정", "추세판정"),
        "분석자료원": ("자료원",),
        "기간구분": ("기간",),
    }.get(raw, ())
    for v in extra:
        n = _norm_col_name(v)
        if n and n not in aliases:
            aliases.append(n)
    return aliases


def _strip_group_words(value: str) -> str:
    out = _norm_col_name(value)
    for w in _COMMON_GROUP_WORDS:
        out = out.replace(_norm_col_name(w), "")
    return out


def _find_common_group_column(df: pd.DataFrame, query: str) -> str:
    if not isinstance(df, pd.DataFrame) or df.empty or not _has_common_group_intent(query):
        return ""

    compact = _normalize_common_group_query(query)
    body = _common_filter_body(query)
    body_norm = _norm_col_name(body)
    body_stripped = _strip_group_words(body)
    candidates: list[tuple[int, int, str]] = []

    for col in [str(c) for c in df.columns]:
        if col in _COMMON_FILTER_SKIP_COLUMNS:
            continue
        aliases = _group_aliases(col)
        col_norm = _norm_col_name(col)
        if body_norm == col_norm or body_stripped == col_norm:
            candidates.append((10_000, len(col_norm), col))
            continue
        for alias in aliases:
            if not alias:
                continue
            if alias in compact or alias in body_norm or alias == body_stripped:
                candidates.append((compact.find(alias) if alias in compact else 9999, len(alias), col))

    if not candidates:
        return ""
    candidates.sort(key=lambda x: (x[1], -x[0]), reverse=True)
    return candidates[0][2]


def _is_sum_candidate_column(col: str) -> bool:
    s = str(col or "").strip()
    if not s or s == "순번":
        return False
    if any(w in s for w in _COMMON_GROUP_SUM_EXCLUDE):
        return False
    return any(w in s for w in _COMMON_GROUP_SUM_INCLUDE)


def _distinct_label_for_group(df: pd.DataFrame) -> tuple[str, str]:
    for col, label in [
        ("제약사명", "제약사수"),
        ("제조사명", "제조사수"),
        ("제품명", "제품수"),
        ("제품코드", "제품수"),
        ("거래처명", "거래처수"),
        ("매입처명", "매입처수"),
    ]:
        if col in df.columns:
            return col, label
    return "", "고유값수"


def _trend_sort_key(value: Any) -> int:
    order = {
        "증가": 1,
        "신규/증가": 2,
        "안정": 3,
        "감소": 4,
        "반품주의": 5,
        "자료부족": 6,
        "비교자료 부족": 6,
        "미분류": 6,
        "(미지정)": 6,
    }
    return order.get(str(value or "").strip(), 99)


def _build_common_group_summary(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    work = df.copy()
    work[group_col] = work[group_col].map(_clean_group_value)
    if group_col == "추세판정":
        work[group_col] = work[group_col].replace({"(미지정)": "자료부족", "": "자료부족"})

    g = work.groupby(group_col, dropna=False)
    out = pd.DataFrame({group_col: g.size().index.astype(str), "행수": g.size().values})

    distinct_col, distinct_label = _distinct_label_for_group(work)
    if distinct_col and distinct_col in work.columns:
        out[distinct_label] = g[distinct_col].nunique(dropna=True).values
        total_basis = max(int(work[distinct_col].nunique(dropna=True)), 1)
        ratio_basis = out[distinct_label]
    else:
        out[distinct_label] = out["행수"]
        total_basis = max(int(len(work)), 1)
        ratio_basis = out["행수"]
    out["비율"] = pd.to_numeric(ratio_basis, errors="coerce").fillna(0) / total_basis * 100

    sum_cols: list[str] = []
    for col in [str(c) for c in work.columns]:
        if col == group_col or col in out.columns or col in _COMMON_FILTER_SKIP_COLUMNS:
            continue
        if not _is_sum_candidate_column(col):
            continue
        nums = _to_numeric_for_common_filter(work[col])
        if nums.notna().sum() <= 0:
            continue
        work[f"__sum_{len(sum_cols)}"] = nums
        out[col] = g[f"__sum_{len(sum_cols)}"].sum().values
        sum_cols.append(col)

    progress_pairs = [
        ("당월 현재매출", "당월 예상매출", "당월 진척률"),
        ("평가월 매출", "평가월 예상매출", "평가월 진척률"),
        ("월시점 실제매출", "월시점 예상매출", "월시점 달성률"),
    ]
    for actual_col, expected_col, progress_col in progress_pairs:
        if actual_col in work.columns and expected_col in work.columns:
            work["__actual_for_progress"] = _to_numeric_for_common_filter(work[actual_col])
            work["__expected_for_progress"] = _to_numeric_for_common_filter(work[expected_col])
            actual_sum = g["__actual_for_progress"].sum()
            expected_sum = g["__expected_for_progress"].sum()
            progress = actual_sum.divide(expected_sum.where(expected_sum != 0)).mul(100).fillna(0)
            out[progress_col] = out[group_col].map(progress.to_dict()).fillna(0).astype(float)
            break

    front = ["순번", group_col, distinct_label, "행수", "비율"]
    preferred = [
        "총매출공급가액",
        "총매출세액",
        "총매출액",
        "완료월총매출",
        "당월 현재매출",
        "평가월 매출",
        "당월 예상매출",
        "평가월 예상매출",
        "당월 잔여예상",
        "평가월 잔여예상",
        "당월 진척률",
        "평가월 진척률",
    ]
    if group_col == "추세판정":
        out["_sort"] = out[group_col].map(_trend_sort_key)
        out = out.sort_values(["_sort", group_col], ascending=[True, True]).drop(columns=["_sort"])
    else:
        out = out.sort_values([distinct_label, "행수", group_col], ascending=[False, False, True])
    out = _add_seq_column(out)
    order = [c for c in front if c in out.columns] + [c for c in preferred if c in out.columns and c not in front]
    rest = [c for c in out.columns if c not in order]
    return out.loc[:, order + rest]


def _select_common_group_top_metric(out: pd.DataFrame, query: str) -> tuple[str, str]:
    """차원별 TOP 정렬 기준을 선택한다."""
    if not isinstance(out, pd.DataFrame) or out.empty:
        return "", ""

    compact = _compact(query)

    def _first_existing(names: tuple[str, ...]) -> str:
        for name in names:
            if name in out.columns:
                return name
        return ""

    amount_cols = (
        "합계금액",
        "수불금액",
        "공급가액",
        "총매출공급가액",
        "총매출액",
        "매출액",
        "부족예상금액",
        "배정부족예상금액",
    )
    default_qty_cols = (
        "합계수량",
        "수불수량",
        "총수량",
        "집계수량",
    )
    explicit_qty_cols = default_qty_cols + (
        "출고수량",
        "입고수량",
        "재고수량",
        "부족예상수량",
        "배정부족예상수량",
    )
    count_cols = ("건수", "행수", "거래처수", "제품수", "품목수", "고유값수")

    if any(w in compact for w in ("건수", "행수")):
        col = _first_existing(count_cols)
        if col:
            return col, "건수"

    def _metric_label(col_name: str) -> str:
        if col_name in count_cols:
            return "건수"
        if col_name in amount_cols:
            return "금액"
        return "수량"

    # 정확한 지표명은 일반 단어("수량")보다 먼저 본다.
    explicit_metric_cols = tuple(
        sorted(
            set(amount_cols + explicit_qty_cols + count_cols),
            key=lambda name: len(_compact(name)),
            reverse=True,
        )
    )
    for col_name in explicit_metric_cols:
        if col_name in out.columns and _compact(col_name) in compact:
            return col_name, _metric_label(col_name)

    short_metric_aliases = (
        ("출고수량", ("출고",)),
        ("입고수량", ("입고",)),
        ("재고수량", ("재고",)),
        ("수불수량", ("수불",)),
        ("부족예상수량", ("부족수량",)),
        ("배정부족예상수량", ("배정부족수량",)),
    )
    for col_name, aliases in short_metric_aliases:
        if col_name in out.columns and any(alias in compact for alias in aliases):
            return col_name, "수량"

    if any(w in compact for w in ("수량",)):
        col = _first_existing(default_qty_cols)
        if col:
            return col, "수량"
        col = _first_existing(count_cols)
        if col:
            return col, "건수"

    if any(w in compact for w in ("금액", "매출", "공급가액", "합계")):
        col = _first_existing(amount_cols)
        if col:
            return col, "금액"

    col = _first_existing(amount_cols)
    if col:
        return col, "금액"
    col = _first_existing(default_qty_cols)
    if col:
        return col, "수량"
    col = _first_existing(count_cols)
    if col:
        return col, "건수"
    return "", ""


def handle_common_column_group_followup(
    *,
    df: pd.DataFrame,
    query: str,
    top_n: int,
    table_key: str,
    source_action: str,
    helpers: dict[str, Callable[..., Any]],
    log: Any,
) -> bool:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return False

    t = str(query or "").strip()
    group_col = _find_common_group_column(df, t)
    if not group_col:
        return False

    push_table = helpers.get("push_table")
    if not callable(push_table):
        return False

    out = _build_common_group_summary(df, group_col)
    has_top = any(w in _compact(t) for w in ("TOP", "top", "상위"))
    metric_col = ""
    metric_label = ""
    if has_top and top_n:
        metric_col, metric_label = _select_common_group_top_metric(out, t)
        if metric_col and metric_col in out.columns:
            nums = _to_numeric_for_common_filter(out[metric_col])
            out = out.assign(__sort_metric=nums)
            out = out.sort_values(
                ["__sort_metric", group_col],
                ascending=[False, True],
                kind="mergesort",
            ).drop(columns=["__sort_metric"])
        out = out.head(int(top_n)).copy()
        out = _add_seq_column(out)
    out = _drop_current_followup_detail_attrs(out)

    title = f"현재표 {group_col}별 TOP {top_n}" if has_top and top_n else f"현재표 {group_col}별 집계"
    try:
        log.info(
            "[chat.followup.generic_group] query=%r source_action=%r group_column=%r metric_column=%r source_rows=%s result_rows=%s table_key=%s",
            t,
            source_action,
            group_col,
            metric_col,
            len(df),
            len(out),
            table_key,
        )
    except Exception:
        pass

    return bool(push_table(
        title=title,
        action=title,
        df=out,
        query_summary=(
            f"현재표 / {group_col}별 TOP {top_n} · 기준: {metric_col or metric_label or '건수'} / 전체 {len(df):,}건 기준"
            if has_top and top_n
            else f"현재표 / {group_col}별 집계 / 전체 {len(df):,}건 기준"
        ),
        source_query=t,
        source_table_key=table_key,
        source_rows=len(df),
        display_limit=top_n if has_top else None,
        extra_meta={
            "group_column": group_col,
            "group_top_metric": metric_col,
            "source_row_count": int(len(df)),
        },
    ))


def handle_common_column_filter_followup(
    *,
    df: pd.DataFrame,
    query: str,
    top_n: int,
    table_key: str,
    source_action: str,
    helpers: dict[str, Callable[..., Any]],
    log: Any,
) -> bool:
    """
    현재표 공통 컬럼 필터.

    지원 예:
    - 현재표 제조사명 한미약품 상세히 보여줘
    - 현재표 제품구분명 전문 목록
    - 현재표 예상등급 상승예상 상세히 보여줘
    - 현재표 추세판정 감소 조회
    - 현재표 현재재고수량 0 이하 보여줘
    - 현재표 1개월부족수량 100 이상 보여줘
    - 현재표 총매출액 100만원 초과 보여줘

    주의:
    - LLM이 표를 직접 필터링하지 않는다.
    - 컬럼/값/숫자조건 의도만 규칙으로 해석하고, 실제 필터는 pandas로 수행한다.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return False

    t = str(query or "").strip()

    push_table = helpers.get("push_table")
    push_notice = helpers.get("push_notice")
    if not callable(push_table):
        return False

    # 0) 조건 없는 숫자 TOP: 현재표 배정부족예상금액 TOP 20
    if top_n and any(w in _compact(t) for w in ("TOP", "top", "상위")):
        try:
            if _find_common_group_column(df, t):
                return False
        except Exception:
            pass

    top_col = _find_common_top_numeric_column(df, t)
    if top_col and top_col in df.columns and top_n:
        try:
            nums = _to_numeric_for_common_filter(_first_series_for_column(df, top_col))
            out = df.copy()
            out[top_col] = nums.values
            out = out.sort_values(top_col, ascending=False).head(int(top_n)).copy()
            out = _reorder_numeric_filter_columns(out, top_col)
            out = _add_seq_column(out)
            out = _drop_current_followup_detail_attrs(out)
        except Exception:
            try:
                log.exception("[chat.followup_table] common numeric top failed col=%r top_n=%r", top_col, top_n)
            except Exception:
                pass
            return False

        title = f"현재표 {top_col} TOP {top_n}"
        try:
            log.info(
                "[chat.followup.generic_top] query=%r source_action=%r top_column=%r source_rows=%s rows=%s table_key=%s",
                t,
                source_action,
                top_col,
                len(df),
                len(out),
                table_key,
            )
        except Exception:
            pass

        return bool(push_table(
            title=title,
            action=title,
            df=out,
            query_summary=f"현재표 / {top_col} TOP {top_n} / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=top_n,
            extra_meta={
                "top_column": top_col,
                "top_n": int(top_n),
                "source_row_count": int(len(df)),
            },
        ))

    # 1) 숫자 조건 필터: 현재표 현재재고수량 0 이하 보여줘
    num_col, op, threshold, op_label, threshold_label = _find_common_numeric_filter(df, t)
    if num_col and num_col in df.columns and op:
        try:
            nums = _to_numeric_for_common_filter(_first_series_for_column(df, num_col))
            mask = _apply_numeric_mask(nums, op, threshold)
            filtered = df.loc[mask].copy()
            if not filtered.empty:
                filtered[num_col] = nums.loc[filtered.index].values
        except Exception:
            try:
                log.exception(
                    "[chat.followup_table] common numeric filter failed col=%r op=%r threshold=%r",
                    num_col,
                    op,
                    threshold,
                )
            except Exception:
                pass
            return False

        threshold_text = _fmt_threshold(threshold, threshold_label)
        filtered_rows = int(len(filtered))

        if filtered_rows <= 0:
            if not callable(push_notice):
                return False
            return bool(push_notice(
                title=f"현재표 {num_col} {threshold_text} {op_label} 자료 없음",
                action=f"현재표 {num_col} {threshold_text} {op_label} 자료 없음",
                message=f"현재표에서 {num_col} 컬럼이 {threshold_text} {op_label}인 자료가 없습니다.",
                query_summary=f"현재표 / {num_col} {threshold_text} {op_label} / 전체 {len(df):,}건 중 0건",
                source_query=t,
            ))

        has_top = any(w in _compact(t) for w in ("TOP", "top", "상위"))
        out = filtered.copy()
        if has_top and top_n:
            ascending = op in ("<", "<=")
            try:
                out = out.sort_values(num_col, ascending=ascending).head(int(top_n)).copy()
            except Exception:
                out = out.head(int(top_n)).copy()
        out = _reorder_numeric_filter_columns(out, num_col)
        out = _add_seq_column(out)
        out = _drop_current_followup_detail_attrs(out)

        title = (
            f"현재표 {num_col} {threshold_text} {op_label} TOP {top_n}"
            if has_top
            else f"현재표 {num_col} {threshold_text} {op_label} 목록"
        )

        try:
            log.info(
                "[chat.followup.generic_filter] query=%r source_action=%r filter_column=%r operator=%s filter_value=%s source_rows=%s result_rows=%s table_key=%s",
                t,
                source_action,
                num_col,
                op,
                threshold_label,
                len(df),
                filtered_rows,
                table_key,
            )
            log.info(
                "[chat.followup_table] common numeric filter built source_action=%r col=%r op=%s threshold=%s source_rows=%s filtered_rows=%s rows=%s table_key=%s",
                source_action,
                num_col,
                op,
                threshold,
                len(df),
                filtered_rows,
                len(out),
                table_key,
            )
        except Exception:
            pass

        return bool(push_table(
            title=title,
            action=title,
            df=out,
            query_summary=f"현재표 / {num_col} {threshold_text} {op_label} / 전체 {len(df):,}건 중 {filtered_rows:,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=top_n if has_top else None,
            extra_meta={
                "filter_column": num_col,
                "filter_operator": op,
                "filter_value": threshold_label,
                "source_row_count": int(len(df)),
            },
        ))

    # 2) 문자/코드값 포함 필터: 현재표 제조사명 한미 상세히 보여줘
    col, value_norm = _find_common_column_filter(df, t)
    if not col or col not in df.columns or not value_norm:
        if callable(push_notice) and _has_common_filter_intent(t):
            candidate = _extract_common_filter_candidate(t)
            if candidate:
                available = _available_common_filter_columns(df)
                available_text = ", ".join(available)
                try:
                    log.info(
                        "[chat.followup_table] current table filter column not found query=%r candidate=%r available=%r",
                        t,
                        candidate,
                        available_text,
                    )
                except Exception:
                    pass
                return bool(push_notice(
                    title=f"현재표 {candidate} 상세표 불가",
                    action=f"현재표 {candidate} 상세표 불가",
                    message=(
                        f"현재표에는 '{candidate}' 컬럼이 없어 상세표를 만들 수 없습니다.\n"
                        "현재표에서 가능한 컬럼은 다음과 같습니다:\n"
                        f"{available_text or '(확인 가능한 컬럼 없음)'}"
                    ),
                    query_summary=f"현재표 / {candidate} 상세표 불가 / 컬럼 없음",
                    source_query=t,
                ))
        return False

    try:
        compact_values = _series_compact_for_filter(_first_series_for_column(df, col))
        mask = compact_values.str.contains(re.escape(value_norm), na=False, regex=True)
        filtered = df.loc[mask].copy()
    except Exception:
        try:
            log.exception("[chat.followup_table] common column filter failed col=%r value=%r", col, value_norm)
        except Exception:
            pass
        return False

    value_label = _display_filter_value(value_norm)
    filtered_rows = int(len(filtered))

    if filtered_rows <= 0:
        try:
            log.info(
                "[chat.followup_table] current table filter no rows query=%r column=%r value=%r source_rows=%s",
                t,
                col,
                value_label,
                len(df),
            )
        except Exception:
            pass
        if not callable(push_notice):
            return False
        return bool(push_notice(
            title=f"현재표 {col} {value_label} 자료 없음",
            action=f"현재표 {col} {value_label} 자료 없음",
            message=(
                f"현재표에서 {col}에 '{value_label}'이 포함된 행을 찾지 못했습니다.\n"
                "다른 검색어로 다시 시도해 주세요."
            ),
            query_summary=f"현재표 / {col}={value_label} / 전체 {len(df):,}건 중 0건",
            source_query=t,
        ))

    has_top = any(w in _compact(t) for w in ("TOP", "top", "상위"))
    out = filtered.copy()
    if has_top and top_n:
        out = out.head(int(top_n)).copy()
    out = _drop_current_followup_detail_attrs(out)

    title = (
        f"현재표 {col} {value_label} TOP {top_n}"
        if has_top
        else f"현재표 {col} '{value_label}' 상세표"
    )

    try:
        log.info(
            "[chat.followup.generic_filter] query=%r source_action=%r filter_column=%r operator=%s filter_value=%r source_rows=%s result_rows=%s table_key=%s",
            t,
            source_action,
            col,
            "contains",
            value_label,
            len(df),
            filtered_rows,
            table_key,
        )
        log.info(
            "[chat.followup_table] current table filter detail detected query=%r column=%r value=%r source_rows=%s matched_rows=%s",
            t,
            col,
            value_label,
            len(df),
            filtered_rows,
        )
    except Exception:
        pass

    return bool(push_table(
        title=title,
        action=title,
        df=out,
        query_summary=f"현재표 / {col}={value_label} / 전체 {len(df):,}건 중 {filtered_rows:,}건 기준",
        source_query=t,
        source_table_key=table_key,
        source_rows=len(df),
        display_limit=top_n if has_top else None,
        extra_meta={
            "filter_column": col,
            "filter_operator": "contains",
            "filter_value": value_label,
            "source_row_count": int(len(df)),
        },
    ))


def handle_generic_followup(
    *,
    df: pd.DataFrame,
    query: str,
    top_n: int,
    table_key: str,
    source_action: str,
    helpers: dict[str, Callable[..., Any]],
    log: Any,
) -> bool:
    """
    전용 action handler가 없는 generic 현재표 후속분석 중
    마스터/기초정보 표에 대한 간단한 그룹 집계를 처리한다.

    현재 1차 대상:
    - 거래처 목록 → 거래처그룹명별 거래처수
    - 거래처 목록 → 영업사원별 거래처수
    - 제품코드 목록 → 제약사/제조사별 제품수

    처리하지 못한 질문은 False를 반환해서 기존 legacy 현재표 후속분석으로 내려가게 한다.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return False

    t = str(query or "").strip()
    compact = _compact(t)
    source = str(source_action or "").strip()

    if not t:
        return False

    find_col = helpers["find_col"]
    push_table = helpers["push_table"]
    push_notice = helpers["push_notice"]

    # ------------------------------------------------------------
    # 사용자 목록: 부서별 사용자수
    # ------------------------------------------------------------
    is_user_source = any(w in source for w in ("사용자목록", "사용자 목록", "사용자"))
    if is_user_source and any(w in compact for w in ("부서별", "부서명별")):
        user_code_col = find_col(
            df,
            exact=("사용자코드", "사용자ID", "사용자아이디", "사번", "Rd06_User_Cd"),
            include_any=("사용자코드", "사용자ID", "사용자아이디", "사번"),
            exclude_any=("부서", "직책", "권한"),
        )

        dept_col = find_col(
            df,
            exact=("부서명", "부서", "부서 명"),
            include_any=("부서명", "부서"),
            exclude_any=("코드", "상세코드", "대분류코드", "번호", "순번"),
        )

        if not dept_col:
            dept_col = find_col(
                df,
                exact=("부서 상세코드", "부서코드", "부서 상세 코드"),
                include_any=("부서",),
                exclude_any=("대분류", "번호", "순번"),
            )

        if not dept_col:
            return bool(push_notice(
                title="현재표 부서별 요약 불가",
                action="현재표 부서별 요약 불가",
                message=(
                    "현재표에서 부서명/부서 컬럼을 찾지 못했습니다.\n\n"
                    f"현재표 주요 컬럼: {', '.join(str(c) for c in list(df.columns)[:40])}"
                ),
                query_summary=f"현재표 / 부서별 요약 불가 / 원본={source}",
                source_query=t,
            ))

        out = _distinct_count_summary(
            df,
            group_col=dept_col,
            distinct_col=user_code_col,
            count_label="사용자수",
        ).rename(columns={dept_col: "부서명"})

        out = _main_columns_first(out, ("순번", "부서명", "사용자수", "행수"))
        out = _apply_top_if_requested(out, compact, top_n)

        try:
            log.info(
                "[chat.followup_table] generic user dept summary built dept_col=%s source_rows=%s rows=%s table_key=%s",
                dept_col,
                len(df),
                len(out),
                table_key,
            )
        except Exception:
            pass

        return bool(push_table(
            title="현재표 부서별 사용자수",
            action="현재표 부서별 사용자수",
            df=out,
            query_summary=f"현재표 / 부서별 사용자수 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=None,
        ))
    
    # ------------------------------------------------------------
    # 거래처 목록: 거래처그룹명별/영업사원별 거래처수
    # ------------------------------------------------------------
    is_vendor_source = any(w in source for w in ("거래처 목록", "거래처목록", "거래처"))
    if is_vendor_source:
        vendor_code_col = _find_col_with_helper(
            df,
            find_col,
            exact=(
                "거래처코드", "거래처 코드", "거래처", "코드",
                "Rd03_Code", "rd03_code", "ven_cd", "ven_code", "vendor_code",
            ),
            include_any=("거래처코드", "거래처 코드", "Rd03_Code", "rd03_code", "ven_cd", "ven_code"),
            exclude_any=("그룹", "종류", "등급"),
        )

        if any(w in compact for w in ("거래처그룹명별", "거래처그룹별", "그룹명별", "그룹별")):
            group_col = _find_col_with_helper(
                df,
                find_col,
                exact=(
                    # 표시명 후보
                    "거래처그룹명", "거래처 그룹명", "거래처그룹", "거래처 그룹",
                    "거래처분류명", "거래처 분류명", "그룹명", "분류명",

                    # Rddbc030 원본/서비스 alias 후보
                    "ven_group_nm", "ven_group_name", "Ven_Group_Nm", "Ven_Group_Name",
                    "Rd03_Ven_Group", "rd03_ven_group",
                    "Rd03_Ven_Group_Gcode", "rd03_ven_group_gcode",
                    "거래처그룹 상세코드", "거래처그룹　상세코드",
                    "거래처그룹 대분류코드", "거래처그룹　대분류코드",
                ),
                include_any=(
                    "거래처그룹", "거래처 그룹", "그룹명", "거래처분류", "분류명",
                    "ven_group", "vengroup", "rd03_ven_group", "rd03vengroup",
                ),
                include_all=("거래처", "그룹"),
                exclude_any=("순번", "번호"),
            )

            if not group_col:
                try:
                    log.warning(
                        "[chat.followup_table] generic vendor group column not found source_rows=%s columns=%s",
                        len(df),
                        [str(c) for c in list(df.columns)[:120]],
                    )
                except Exception:
                    pass

                return bool(push_notice(
                    title="현재표 거래처그룹명별 분석 불가",
                    action="현재표 거래처그룹명별 분석 불가",
                    message=(
                        "현재표에서 거래처그룹명 컬럼을 찾지 못했습니다.\n\n"
                        f"현재표 주요 컬럼: {', '.join(str(c) for c in list(df.columns)[:40])}"
                    ),
                    query_summary=f"현재표 / 거래처그룹명별 분석 / 원본={source}",
                    source_query=t,
                ))

            out = _distinct_count_summary(
                df,
                group_col=group_col,
                distinct_col=vendor_code_col,
                count_label="거래처수",
            ).rename(columns={group_col: "거래처그룹명"})
            out = _main_columns_first(out, ("순번", "거래처그룹명", "거래처수", "행수"))
            out = _apply_top_if_requested(out, compact, top_n)

            try:
                log.info(
                    "[chat.followup_table] generic vendor group summary built group_col=%s source_rows=%s rows=%s table_key=%s",
                    group_col,
                    len(df),
                    len(out),
                    table_key,
                )
            except Exception:
                pass

            return bool(push_table(
                title="현재표 거래처그룹명별 거래처수",
                action="현재표 거래처그룹명별 거래처수",
                df=out,
                query_summary=f"현재표 / 거래처그룹명별 거래처수 / 전체 {len(df):,}건 기준",
                source_query=t,
                source_table_key=table_key,
                source_rows=len(df),
                display_limit=None,
            ))

        if "영업사원별" in compact or "담당자별" in compact:
            staff_col = _find_col_with_helper(
                df,
                find_col,
                exact=(
                    # 표시명 후보
                    "영업사원명", "영업 사원명", "영업담당자명", "영업 담당자명",
                    "담당자명", "담당사원명", "관리사원명", "사원명",
                    "영업사원", "영업담당자", "담당사원", "관리사원", "영업사원코드",

                    # Rddbc030 원본/서비스 alias 후보
                    "sales_man_nm", "salesman_nm", "Sales_Man_Nm", "sales_man_name",
                    "Rd03_Sales_Man", "rd03_sales_man",
                    "Rd06_User_Nm", "rd06_user_nm",
                ),
                include_any=(
                    "영업사원명", "영업사원", "영업담당", "담당자명",
                    "담당사원", "관리사원", "사원명",
                    "sales_man", "salesman", "rd03_sales_man", "rd03salesman",
                ),
                exclude_any=("번호", "ID", "아이디"),
            )

            if not staff_col:
                try:
                    log.warning(
                        "[chat.followup_table] generic vendor staff column not found source_rows=%s columns=%s",
                        len(df),
                        [str(c) for c in list(df.columns)[:120]],
                    )
                except Exception:
                    pass

                return bool(push_notice(
                    title="현재표 영업사원별 거래처수 불가",
                    action="현재표 영업사원별 거래처수 불가",
                    message=(
                        "현재표에서 영업사원명 컬럼을 찾지 못했습니다.\n\n"
                        f"현재표 주요 컬럼: {', '.join(str(c) for c in list(df.columns)[:40])}"
                    ),
                    query_summary=f"현재표 / 영업사원별 거래처수 불가 / 원본={source}",
                    source_query=t,
                ))

            out = _distinct_count_summary(
                df,
                group_col=staff_col,
                distinct_col=vendor_code_col,
                count_label="거래처수",
            ).rename(columns={staff_col: "영업사원명"})
            out = _main_columns_first(out, ("순번", "영업사원명", "거래처수", "행수"))
            out = _apply_top_if_requested(out, compact, top_n)

            try:
                log.info(
                    "[chat.followup_table] generic vendor staff summary built staff_col=%s source_rows=%s rows=%s table_key=%s",
                    staff_col,
                    len(df),
                    len(out),
                    table_key,
                )
            except Exception:
                pass

            return bool(push_table(
                title="현재표 영업사원별 거래처수",
                action="현재표 영업사원별 거래처수",
                df=out,
                query_summary=f"현재표 / 영업사원별 거래처수 / 전체 {len(df):,}건 기준",
                source_query=t,
                source_table_key=table_key,
                source_rows=len(df),
                display_limit=None,
            ))

    # ------------------------------------------------------------
    # 제품코드 목록: 제품그룹별 제품수
    # ------------------------------------------------------------
    is_goods_source = any(w in source for w in ("제품코드 목록", "제품코드목록", "제품"))
    if is_goods_source and any(w in compact for w in ("제품그룹별", "제품그룹명별", "그룹별")):
        product_code_col = find_col(
            df,
            exact=("제품코드", "상품코드", "품목코드"),
            include_any=("제품코드", "상품코드", "품목코드"),
            exclude_any=("그룹", "구분", "분류"),
        )

        group_col = find_col(
            df,
            exact=("제품그룹명", "제품 그룹명", "제품그룹", "제품 그룹", "그룹명"),
            include_any=("제품그룹", "제품 그룹", "그룹명"),
            exclude_any=("코드", "번호", "순번"),
        )

        if not group_col:
            group_col = find_col(
                df,
                exact=("제품그룹코드", "제품그룹 코드", "제품그룹코드 분류"),
                include_any=("제품그룹",),
                exclude_any=("대분류번호", "순번"),
            )

        if not group_col:
            return bool(push_notice(
                title="현재표 제품그룹별 요약 불가",
                action="현재표 제품그룹별 요약 불가",
                message=(
                    "현재표에서 제품그룹명/제품그룹코드 컬럼을 찾지 못했습니다.\n\n"
                    f"현재표 주요 컬럼: {', '.join(str(c) for c in list(df.columns)[:40])}"
                ),
                query_summary=f"현재표 / 제품그룹별 요약 불가 / 원본={source}",
                source_query=t,
            ))

        out = _distinct_count_summary(
            df,
            group_col=group_col,
            distinct_col=product_code_col,
            count_label="제품수",
        ).rename(columns={group_col: "제품그룹명"})

        out = _main_columns_first(out, ("순번", "제품그룹명", "제품수", "행수"))
        out = _apply_top_if_requested(out, compact, top_n)

        try:
            log.info(
                "[chat.followup_table] generic goods group summary built group_col=%s source_rows=%s rows=%s table_key=%s",
                group_col,
                len(df),
                len(out),
                table_key,
            )
        except Exception:
            pass

        return bool(push_table(
            title="현재표 제품그룹별 제품수",
            action="현재표 제품그룹별 제품수",
            df=out,
            query_summary=f"현재표 / 제품그룹별 제품수 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=None,
        ))

    # ------------------------------------------------------------
    # 제품코드 목록: 제조사/제약사별 제품수
    # ------------------------------------------------------------
    is_goods_source = any(w in source for w in ("제품코드 목록", "제품코드목록", "제품"))
    if is_goods_source and any(w in compact for w in ("제약사별", "제조사별", "제조처별", "메이커별")):
        product_code_col = _find_col_with_helper(
            df,
            find_col,
            exact=("제품코드", "상품코드", "품목코드"),
            include_any=("제품코드", "상품코드", "품목코드"),
            exclude_any=("그룹", "구분", "분류"),
        )
        maker_col = _find_col_with_helper(
            df,
            find_col,
            exact=("제조사명", "제약사명", "제조처명", "메이커명", "제조사", "제약사"),
            include_any=("제조사", "제약사", "제조처", "메이커"),
            exclude_any=("코드", "번호", "순번"),
        )

        if not maker_col:
            return bool(push_notice(
                title="현재표 제조사별 제품수 불가",
                action="현재표 제조사별 제품수 불가",
                message=(
                    "현재표에서 제조사명/제약사명 컬럼을 찾지 못했습니다.\n\n"
                    f"현재표 주요 컬럼: {', '.join(str(c) for c in list(df.columns)[:40])}"
                ),
                query_summary=f"현재표 / 제조사별 제품수 불가 / 원본={source}",
                source_query=t,
            ))

        out = _distinct_count_summary(
            df,
            group_col=maker_col,
            distinct_col=product_code_col,
            count_label="제품수",
        ).rename(columns={maker_col: "제조사명"})
        out = _main_columns_first(out, ("순번", "제조사명", "제품수", "행수"))
        out = _apply_top_if_requested(out, compact, top_n)

        try:
            log.info(
                "[chat.followup_table] generic goods maker summary built maker_col=%s source_rows=%s rows=%s table_key=%s",
                maker_col,
                len(df),
                len(out),
                table_key,
            )
        except Exception:
            pass

        return bool(push_table(
            title="현재표 제조사별 제품수",
            action="현재표 제조사별 제품수",
            df=out,
            query_summary=f"현재표 / 제조사별 제품수 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=None,
        ))

    return False
