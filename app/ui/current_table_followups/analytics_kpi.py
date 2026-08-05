# app/ui/current_table_followups/analytics_kpi.py
# Create 2026-06-09
# 분석/KPI 현재표 후속분석       → analytics_kpi

from __future__ import annotations

from typing import Any, Callable

import difflib
import re
import pandas as pd


# ---------------------------------------------------------------------
# 분석/KPI 현재표 후속분석 보조 함수
# - 매출추세/요약표, 매출예상, 재고부족현황은 컬럼 구조가 다르다.
# - 따라서 '예상'과 '부족'은 일반 매출 집계로 흘리지 않고 전용 집계를 먼저 처리한다.
# ---------------------------------------------------------------------
def _ak_num_series(df: pd.DataFrame, col: str | None, to_num) -> pd.Series:
    if not col or col not in df.columns:
        return pd.Series([0.0] * len(df), index=df.index, dtype="float64")

    try:
        s = to_num(df[col])
        return pd.to_numeric(s, errors="coerce").fillna(0.0)
    except Exception:
        return pd.to_numeric(
            df[col].astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        ).fillna(0.0)


def _ak_text_series(df: pd.DataFrame, col: str | None) -> pd.Series:
    if col and col in df.columns:
        return df[col].fillna("").astype(str).str.strip()
    return pd.Series([""] * len(df), index=df.index, dtype="object")


def _ak_valid_name_mask(s: pd.Series) -> pd.Series:
    name = s.fillna("").astype(str).str.strip()
    bad = (
        name.eq("")
        | name.str.contains(r"^(?:합계|총계|소계|전체|TOTAL)$", case=False, regex=True, na=False)
    )
    return ~bad

def _ak_is_dimension_col(col: str | None) -> bool:
    """
    그룹 기준으로 쓸 수 있는 명칭 컬럼인지 판정한다.

    find_col()이 '매입처명' 대신 '매입처수' 같은 집계 컬럼을 잡으면
    groupby/reset_index 충돌이 나거나 의미 없는 표가 만들어진다.
    """
    s = str(col or "").strip()
    if not s:
        return False

    bad_exact = {
        "거래처수", "매입처수", "재고적용처수", "품목수", "제품수",
        "행수", "건수", "집계건수", "총집계건수", "출고건수", "총출고건수",
    }
    if s in bad_exact:
        return False

    # 명칭 컬럼은 허용
    if s.endswith("명") or s in {"제조사", "거래처", "매입처", "재고적용처", "부족등급", "예상등급", "추세판정"}:
        return True

    measure_words = (
        "수량", "금액", "공급가액", "세액", "합계", "단가", "평균",
        "매출", "출고", "재고", "부족", "필요", "예상", "증감률",
        "커버", "월수", "건수",
    )
    if any(w in s for w in measure_words):
        return False

    if s.endswith("수") or s.endswith("액") or s.endswith("율"):
        return False

    return True


def _ak_explicit_column(df: pd.DataFrame, aliases: tuple[str, ...]) -> str:
    """Return only an exact business alias; never infer a dimension from position or dtype."""
    normalized_aliases = {re.sub(r"\s+", "", alias) for alias in aliases}
    for column in df.columns:
        if re.sub(r"\s+", "", str(column)) in normalized_aliases:
            return str(column)
    return ""


def _ak_clean_dimension_col(col: str | None) -> str | None:
    return col if _ak_is_dimension_col(col) else None


def _ak_has_top(query: str) -> bool:
    q = str(query or "")
    return "TOP" in q.upper() or "상위" in q


def _ak_insert_seq(out: pd.DataFrame) -> pd.DataFrame:
    if out is None or out.empty:
        return out
    out = out.copy().reset_index(drop=True)
    if "순번" in out.columns:
        out = out.drop(columns=["순번"])
    out.insert(0, "순번", range(1, len(out) + 1))
    return out


def _ak_reorder(out: pd.DataFrame, order_cols: list[str]) -> pd.DataFrame:
    """
    표시 컬럼 순서 정리.

    같은 컬럼명이 order_cols에 중복되거나, 이미 DataFrame에 중복 컬럼명이 있으면
    Streamlit NumberColumn 보정 단계에서 df[col]이 DataFrame으로 반환되어
    Series truth-value 오류가 날 수 있다.
    여기서 컬럼 순서를 잡으면서 중복 라벨은 1회만 남긴다.
    """
    if out is None or out.empty:
        return out

    seen: set[str] = set()
    use: list[str] = []
    for c in order_cols:
        if c in out.columns and c not in seen:
            use.append(c)
            seen.add(c)

    rest: list[str] = []
    for c in out.columns:
        if c not in seen:
            rest.append(c)
            seen.add(c)

    return out.loc[:, use + rest]


def _ak_round_display_nums(out: pd.DataFrame) -> pd.DataFrame:
    if out is None or out.empty:
        return out
    out = out.copy()

    decimal_words = ("평균", "단가", "율", "커버", "예상기준월수량")
    for col in list(dict.fromkeys(out.columns)):
        s = str(col)
        if s == "순번":
            continue
        if any(w in s for w in ("코드", "명", "규격", "등급", "판정", "자료원")):
            continue
        try:
            target = out.loc[:, col]
            if isinstance(target, pd.DataFrame):
                # 중복 컬럼 라벨이 남아 있으면 안전하게 건너뛴다.
                continue
            num = pd.to_numeric(target, errors="coerce")
        except Exception:
            continue
        if num.notna().sum() == 0:
            continue
        if any(w in s for w in decimal_words):
            out[col] = num.fillna(0).round(2)
        else:
            out[col] = num.fillna(0).round(0).astype("int64")
    return out


def _ak_group_count_pivot(
    *,
    work: pd.DataFrame,
    group_cols: list[str],
    label_col: str,
    product_col: str | None,
    suffix: str,
) -> pd.DataFrame:
    if label_col not in work.columns:
        return pd.DataFrame()

    value_col = "_product_key" if "_product_key" in work.columns else group_cols[-1]
    aggfunc = pd.Series.nunique if value_col == "_product_key" else "count"

    piv = (
        work.pivot_table(
            index=group_cols,
            columns=label_col,
            values=value_col,
            aggfunc=aggfunc,
            fill_value=0,
        )
        .reset_index()
    )

    rename_map = {}
    for c in piv.columns:
        if c in group_cols:
            continue
        label = str(c or "").strip() or "미지정"
        rename_map[c] = f"{label}{suffix}"

    return piv.rename(columns=rename_map)


def _ak_forecast_product_table(
    *,
    df: pd.DataFrame,
    top_n: int,
    has_top: bool,
    to_num,
) -> pd.DataFrame:
    out = df.copy()

    sort_col = None
    for c in ("다음월예상매출", "3개월예상매출", "6개월예상매출", "총매출액", "총매출공급가액"):
        if c in out.columns:
            sort_col = c
            out[c] = _ak_num_series(out, c, to_num)
            break

    for c in [
        "총출고수량", "총출고할증수량", "총매출공급가액", "총매출세액", "총매출액",
        "월평균매출", "매출발생월수", "최근3개월평균매출", "최근6개월평균매출",
        "최근3개월증감률", "적용증감률", "다음월예상매출", "3개월예상매출", "6개월예상매출",
        "거래처수", "매입처수", "재고적용처수", "총출고건수", "총집계건수",
    ]:
        if c in out.columns:
            out[c] = _ak_num_series(out, c, to_num)

    if sort_col:
        out = out.sort_values(sort_col, ascending=False).reset_index(drop=True)

    if has_top:
        out = out.head(top_n).copy()

    out = _ak_reorder(out, [
        "제품코드", "제품명", "규격", "제조사코드", "제조사명",
        "제품그룹명", "제품구분명", "제품분류명",
        "총출고수량", "총출고할증수량", "총매출공급가액", "총매출세액", "총매출액",
        "월평균매출", "매출발생월수", "최근3개월평균매출", "최근6개월평균매출",
        "최근3개월증감률", "추세판정", "예상기준", "적용증감률",
        "다음월예상매출", "3개월예상매출", "6개월예상매출", "예상등급",
        "거래처수", "매입처수", "재고적용처수", "총출고건수", "총집계건수",
    ])
    out = _ak_round_display_nums(out)
    return _ak_insert_seq(out)


def _ak_forecast_group_summary(
    *,
    df: pd.DataFrame,
    group_cols: list[str],
    group_label: str,
    top_n: int,
    has_top: bool,
    to_num,
    product_col: str | None,
) -> pd.DataFrame:
    work = pd.DataFrame(index=df.index)

    for c in group_cols:
        work[c] = _ak_text_series(df, c)
    work = work[_ak_valid_name_mask(work[group_cols[-1]])].copy()

    product_key = product_col if product_col and product_col in df.columns else None
    work["_product_key"] = _ak_text_series(df, product_key) if product_key else work.index.astype(str)

    # 합산 가능한 수치 컬럼
    # - 금액/수량/건수/품목수 계열은 group 합계가 의미가 있다.
    # - 월평균매출/최근N개월평균매출도 품목별 평균매출의 합계로 보면
    #   그룹 전체의 월평균 규모로 해석할 수 있으므로 합계 유지.
    numeric_sum_cols = [
        # 매출추세 원자료/구버전 컬럼
        "출고수량", "출고할증수량",
        "매출공급가액", "매출세액", "매출합계",
        "집계건수",

        # 매출추세 요약표/매출예상 컬럼
        "총출고수량", "총출고할증수량",
        "총매출공급가액", "총매출세액", "총매출액",
        "월평균매출", "매출발생월수",
        "최근3개월평균매출", "최근6개월평균매출",
        "다음월예상매출", "3개월예상매출", "6개월예상매출",

        # 건수/차원 수
        "거래처수", "매입처수", "재고적용처수",
        "총출고건수", "총집계건수",
    ]

    # 비율/증감률 컬럼
    # - 단순 합계는 업무 의미가 없다.
    # - 예: 예상등급별 요약에서 적용증감률 276은 20개 품목 합계가 되어
    #   실제 평균 적용증감률 13.8%와 다르게 보인다.
    # - 따라서 group 요약에서는 평균 컬럼명으로 별도 집계한다.
    numeric_mean_cols = [
        "최근3개월증감률",
        "적용증감률",
    ]

    for c in numeric_sum_cols + numeric_mean_cols:
        if c in df.columns:
            work[c] = _ak_num_series(df, c, to_num)

    if "예상등급" in df.columns:
        work["예상등급"] = _ak_text_series(df, "예상등급")
    if "추세판정" in df.columns:
        work["추세판정"] = _ak_text_series(df, "추세판정")

    agg_dict = {c: (c, "sum") for c in numeric_sum_cols if c in work.columns}

    if "최근3개월증감률" in work.columns:
        agg_dict["평균최근3개월증감률"] = ("최근3개월증감률", "mean")

    if "적용증감률" in work.columns:
        agg_dict["평균적용증감률"] = ("적용증감률", "mean")

    grouped = work.groupby(group_cols, dropna=False).agg(**agg_dict).reset_index()

    row_counts = work.groupby(group_cols, dropna=False).size().reset_index(name="행수")
    grouped = grouped.merge(row_counts, on=group_cols, how="left")

    product_counts = (
        work.groupby(group_cols, dropna=False)["_product_key"].nunique().reset_index(name="품목수")
    )
    grouped = grouped.merge(product_counts, on=group_cols, how="left")

    if "예상등급" in work.columns and "예상등급" not in group_cols:
        piv = _ak_group_count_pivot(
            work=work,
            group_cols=group_cols,
            label_col="예상등급",
            product_col=product_col,
            suffix="품목수",
        )
        if not piv.empty:
            grouped = grouped.merge(piv, on=group_cols, how="left")

    # 예상등급의 '자료부족품목수'와 추세판정의 '자료부족품목수'가 충돌하지 않도록
    # 추세판정 pivot은 별도 접미어를 사용한다.
    if "추세판정" in work.columns and "추세판정" not in group_cols:
        piv = _ak_group_count_pivot(
            work=work,
            group_cols=group_cols,
            label_col="추세판정",
            product_col=product_col,
            suffix="추세품목수",
        )
        if not piv.empty:
            grouped = grouped.merge(piv, on=group_cols, how="left")

    sort_col = next(
        (
            c for c in (
                "다음월예상매출",
                "3개월예상매출",
                "6개월예상매출",
                "총매출액",
                "총매출공급가액",
                "매출합계",
                "매출공급가액",
                "월평균매출",
                "최근3개월평균매출",
                "최근6개월평균매출",
                "품목수",
                "행수",
            )
            if c in grouped.columns
        ),
        None,
    )

    if sort_col:
        grouped = grouped.sort_values(sort_col, ascending=False).reset_index(drop=True)

    if has_top:
        grouped = grouped.head(top_n).copy()

    grouped = _ak_reorder(grouped, [
        *group_cols,
        "행수", "품목수",

        "출고수량", "출고할증수량",
        "매출공급가액", "매출세액", "매출합계",
        "집계건수",

        "총출고수량", "총출고할증수량",
        "총매출공급가액", "총매출세액", "총매출액",
        "월평균매출", "매출발생월수",
        "최근3개월평균매출", "최근6개월평균매출",
        "평균최근3개월증감률", "평균적용증감률",

        "다음월예상매출", "3개월예상매출", "6개월예상매출",

        "상승예상품목수", "감소예상품목수", "안정예상품목수",
        "신규확인품목수", "반품주의품목수", "자료부족품목수",

        "증가추세품목수", "안정추세품목수", "감소추세품목수",
        "신규/증가추세품목수", "반품주의추세품목수", "자료부족추세품목수",

        "거래처수", "매입처수", "재고적용처수",
        "총출고건수", "총집계건수",
    ])

    grouped = _ak_round_display_nums(grouped)
    grouped = _ak_insert_seq(grouped)
    return grouped.rename(columns={group_cols[-1]: group_label})


def _ak_shortage_sort_col(df: pd.DataFrame, query: str, to_num) -> str | None:
    q = str(query or "")
    candidates = []
    if "3개월" in q:
        candidates = ["3개월부족수량", "2개월부족수량", "1개월부족수량"]
    elif "2개월" in q:
        candidates = ["2개월부족수량", "3개월부족수량", "1개월부족수량"]
    elif "1개월" in q:
        candidates = ["1개월부족수량", "2개월부족수량", "3개월부족수량"]
    else:
        candidates = ["1개월부족수량", "2개월부족수량", "3개월부족수량", "현재재고금액"]

    for c in candidates:
        if c in df.columns:
            try:
                if float(_ak_num_series(df, c, to_num).sum()) != 0:
                    return c
            except Exception:
                return c
    return next((c for c in candidates if c in df.columns), None)


def _ak_shortage_product_table(
    *,
    df: pd.DataFrame,
    top_n: int,
    has_top: bool,
    to_num,
    query: str,
) -> tuple[pd.DataFrame, str]:
    out = df.copy()

    numeric_cols = [
        "현재재고수량", "현재재고금액", "장부재고평가단가", "실재고평가단가",
        "최근3개월평균수량", "최근6개월평균수량", "월평균출고수량", "예상기준월수량", "재고커버월수",
        "1개월필요수량", "1개월부족수량", "2개월필요수량", "2개월부족수량", "3개월필요수량", "3개월부족수량",
        "총출고수량", "총매출액", "월평균매출", "최근3개월평균매출", "최근6개월평균매출",
    ]
    for c in numeric_cols:
        if c in out.columns:
            out[c] = _ak_num_series(out, c, to_num)

    sort_col = _ak_shortage_sort_col(out, query, to_num)
    if sort_col:
        sort_cols = [sort_col]
        ascending = [False]
        if "재고커버월수" in out.columns:
            sort_cols.append("재고커버월수")
            ascending.append(True)
        out = out.sort_values(sort_cols, ascending=ascending).reset_index(drop=True)

    if has_top:
        out = out.head(top_n).copy()

    out = _ak_reorder(out, [
        "제품코드", "제품명", "규격", "제조사코드", "제조사명",
        "제품그룹명", "제품구분명", "제품분류명", "재고기준",
        "현재재고수량", "현재재고금액", "장부재고평가단가", "실재고평가단가",
        "최근3개월평균수량", "최근6개월평균수량", "월평균출고수량", "예상기준월수량", "재고커버월수",
        "1개월필요수량", "1개월부족수량", "2개월필요수량", "2개월부족수량", "3개월필요수량", "3개월부족수량",
        "부족등급", "총출고수량", "총매출액", "월평균매출", "최근3개월평균매출", "최근6개월평균매출", "추세판정", "예상등급",
        "거래처수", "매입처수", "재고적용처수",
    ])
    out = _ak_round_display_nums(out)
    return _ak_insert_seq(out), (sort_col or "부족수량")



def _ak_compact_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def _ak_extract_shortage_grade_filter(query: str) -> str:
    """
    재고부족현황 현재표 후속질문에서 특정 부족등급 값을 추출한다.

    예:
    - 현재표 부족등급 재고없음 상세히 보여줘 -> 재고없음
    - 현재표 1개월내 부족 제품 목록 -> 1개월내 부족
    - 현재표 재고없음/수요없음 보여줘 -> 재고없음/수요없음
    """
    q = _ak_compact_text(query)
    if not q:
        return ""

    # 숫자 조건/수량 조건 질문은 부족등급 필터로 오인하지 않는다.
    # 예: "현재표 1개월부족수량 100 이상 보여줘"는
    #     부족등급=1개월내 부족이 아니라 1개월부족수량 >= 100 이어야 한다.
    numeric_condition_words = (
        "이상", "이하", "초과", "미만", "같음", "동일", "=", ">=", "<=", ">", "<",
        "마이너스", "음수", "양수",
    )
    numeric_measure_words = (
        "부족수량", "필요수량", "재고수량", "재고금액", "매출액", "매출금액",
        "총매출액", "커버월수", "재고커버월수", "평균수량", "평균매출",
    )

    if any(w in q for w in numeric_condition_words) and any(w in q for w in numeric_measure_words):
        return ""

    # 긴/복합 등급을 먼저 본다. 그래야 "재고없음/수요없음"이
    # "재고없음"으로 잘못 축약되지 않는다.
    grade_aliases: list[tuple[str, tuple[str, ...]]] = [
        ("재고없음/수요없음", ("재고없음/수요없음", "재고없음수요없음", "수요없음")),
        ("3개월내 부족주의", ("3개월내부족주의", "3개월부족주의")),
        ("2개월내 부족주의", ("2개월내부족주의", "2개월부족주의")),
        ("1개월내 부족", ("1개월내부족", "1개월부족")),
        ("3개월내 부족", ("3개월내부족", "3개월부족")),
        ("2개월내 부족", ("2개월내부족", "2개월부족")),
        ("재고없음", ("재고없음", "재고없슴", "재고없어", "재고없")),
        ("수요관찰", ("수요관찰",)),
        ("정상", ("정상",)),
    ]

    has_grade_anchor = any(w in q for w in ("부족등급", "등급"))
    has_detail_intent = any(
        w in q
        for w in ("상세", "목록", "제품", "품목", "조회", "보여줘", "보여", "분석", "요약", "리스트")
    )

    for label, aliases in grade_aliases:
        if not any(alias in q for alias in aliases):
            continue

        # 위험/부족 등급명은 자체가 강한 신호다.
        if label not in {"정상", "수요관찰"}:
            return label

        # 정상/수요관찰은 일반 단어로도 쓰일 수 있으므로 의도 단어를 확인한다.
        if has_grade_anchor or has_detail_intent:
            return label

    return ""

def _ak_is_numeric_measure_condition_query(query: str) -> bool:
    """
    현재표 숫자 조건 질문인지 판정한다.

    예:
    - 현재표 1개월부족수량 100 이상 보여줘
    - 현재표 현재재고수량 0 이하 보여줘
    - 현재표 총매출액 100만원 초과 보여줘
    """
    q = _ak_compact_text(query)
    if not q:
        return False

    numeric_condition_words = (
        "이상", "이하", "초과", "미만", "같음", "동일",
        "=", ">=", "<=", ">", "<",
        "마이너스", "음수", "양수",
    )
    numeric_measure_words = (
        "부족수량", "필요수량", "재고수량", "재고금액",
        "매출액", "매출금액", "총매출액",
        "커버월수", "재고커버월수",
        "평균수량", "평균매출",
    )

    return (
        any(w in q for w in numeric_condition_words)
        and any(w in q for w in numeric_measure_words)
    )


def _ak_filter_by_shortage_grade(
    df: pd.DataFrame,
    grade_col: str,
    grade_label: str,
) -> pd.DataFrame:
    """부족등급 컬럼을 특정 등급값으로 필터링한다."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    if not grade_col or grade_col not in df.columns:
        return pd.DataFrame()

    target = _ak_compact_text(grade_label)
    if not target:
        return pd.DataFrame()

    try:
        work = df.copy()
        grade_s = work[grade_col].fillna("").astype(str).str.strip()
        mask = grade_s.map(_ak_compact_text).eq(target)
        return work.loc[mask].copy()
    except Exception:
        return pd.DataFrame()

def _ak_shortage_group_summary(
    *,
    df: pd.DataFrame,
    group_cols: list[str],
    group_label: str,
    top_n: int,
    has_top: bool,
    to_num,
    product_col: str | None,
) -> pd.DataFrame:
    work = pd.DataFrame(index=df.index)

    for c in group_cols:
        work[c] = _ak_text_series(df, c)
    work = work[_ak_valid_name_mask(work[group_cols[-1]])].copy()

    product_key = product_col if product_col and product_col in df.columns else None
    work["_product_key"] = _ak_text_series(df, product_key) if product_key else work.index.astype(str)

    numeric_sum_cols = [
        "현재재고수량", "현재재고금액",
        "최근3개월평균수량", "최근6개월평균수량", "월평균출고수량", "예상기준월수량",
        "1개월필요수량", "1개월부족수량", "2개월필요수량", "2개월부족수량", "3개월필요수량", "3개월부족수량",
        "총출고수량", "총매출액",
    ]
    for c in numeric_sum_cols:
        if c in df.columns:
            work[c] = _ak_num_series(df, c, to_num)

    if "재고커버월수" in df.columns:
        work["재고커버월수"] = _ak_num_series(df, "재고커버월수", to_num)

    if "부족등급" in df.columns:
        work["부족등급"] = _ak_text_series(df, "부족등급")

    agg_dict = {c: (c, "sum") for c in numeric_sum_cols if c in work.columns}
    if "재고커버월수" in work.columns:
        agg_dict["평균재고커버월수"] = ("재고커버월수", "mean")

    grouped = work.groupby(group_cols, dropna=False).agg(**agg_dict).reset_index()

    row_counts = work.groupby(group_cols, dropna=False).size().reset_index(name="행수")
    grouped = grouped.merge(row_counts, on=group_cols, how="left")

    product_counts = work.groupby(group_cols, dropna=False)["_product_key"].nunique().reset_index(name="품목수")
    grouped = grouped.merge(product_counts, on=group_cols, how="left")

    shortage_grades = {"재고없음", "1개월내 부족", "2개월내 부족", "2개월내 부족주의", "3개월내 부족", "3개월내 부족주의"}
    if "부족등급" in work.columns:
        risk = work[work["부족등급"].isin(shortage_grades)].copy()
        if not risk.empty:
            risk_counts = risk.groupby(group_cols, dropna=False)["_product_key"].nunique().reset_index(name="부족품목수")
            grouped = grouped.merge(risk_counts, on=group_cols, how="left")

        if "부족등급" not in group_cols:
            piv = _ak_group_count_pivot(
                work=work,
                group_cols=group_cols,
                label_col="부족등급",
                product_col=product_col,
                suffix="품목수",
            )
            if not piv.empty:
                grouped = grouped.merge(piv, on=group_cols, how="left")

    sort_col = "1개월부족수량" if "1개월부족수량" in grouped.columns else "3개월부족수량"
    if sort_col in grouped.columns:
        grouped = grouped.sort_values(sort_col, ascending=False).reset_index(drop=True)

    if has_top:
        grouped = grouped.head(top_n).copy()

    grouped = _ak_reorder(grouped, [
        *group_cols,
        "행수", "품목수", "부족품목수",
        "현재재고수량", "현재재고금액",
        "최근3개월평균수량", "최근6개월평균수량", "월평균출고수량", "예상기준월수량", "평균재고커버월수",
        "1개월필요수량", "1개월부족수량", "2개월필요수량", "2개월부족수량", "3개월필요수량", "3개월부족수량",
        "재고없음품목수", "1개월내 부족품목수", "2개월내 부족주의품목수", "3개월내 부족주의품목수", "3개월내 부족품목수",
        "정상품목수", "수요관찰품목수", "재고없음/수요없음품목수",
        "총출고수량", "총매출액",
    ])
    grouped = _ak_round_display_nums(grouped)
    grouped = _ak_insert_seq(grouped)
    return grouped.rename(columns={group_cols[-1]: group_label})


def normalize_current_table_query(text: str) -> str:
    """현재표 후속질문에서 공백/접두어/요청 동사를 제거한 비교용 문자열."""
    q = re.sub(r"\s+", "", str(text or "").strip())
    for token in ("현제표의", "현제표에서", "현제표", "현제의", "현제에서", "현제"):
        q = q.replace(token, "현재표" if token == "현제표" else "")
    for token in ("현재표의", "현재표에서", "현재표", "현재의", "현재에서", "현재"):
        q = q.replace(token, "")
    for token in ("만들어줘", "보여줘", "해줘", "해주세요", "결과", "표"):
        q = q.replace(token, "")
    return q


def extract_groupby_candidate(text: str) -> str:
    """
    "OO별 분석", "OO명별 분석", "OO 명 별 분석" 형태에서 OO 후보를 추출한다.

    단순 "제품별/품목별"은 기존 제품별 상세/Top 경로를 유지해야 하므로 제외하고,
    "제품명별/품목명별/제품코드별"처럼 실제 컬럼명이 명시된 경우만 공통 컬럼 분석 후보로 본다.
    """
    q = normalize_current_table_query(text)
    if not q or "별" not in q:
        return ""

    q = re.sub(r"(분석|집계|요약|매출|현황|목록|TOP\d*|Top\d*|top\d*)+$", "", q)
    match = re.search(r"(.+?)(명별|코드별|별)$", q)
    if not match:
        return ""

    candidate = str(match.group(1) or "").strip()
    suffix = str(match.group(2) or "")
    if not candidate:
        return ""

    if suffix == "명별" and not candidate.endswith("명"):
        candidate = f"{candidate}명"
    elif suffix == "코드별" and not candidate.endswith("코드"):
        candidate = f"{candidate}코드"

    # "현재표 제품별 분석", "현재표 품목별 분석"은 기존 제품별 분석 경로가 담당한다.
    if suffix == "별" and candidate in {"제품", "품목", "상품"}:
        return ""

    return candidate


def _ak_norm_col_name(value: Any) -> str:
    return re.sub(r"[\s_\-./()]+", "", str(value or "").strip()).lower()


def _ak_is_measure_like_col(col: str | None) -> bool:
    s = str(col or "").strip()
    if not s:
        return True
    if s == "순번":
        return True
    measure_words = (
        "수량", "금액", "공급가액", "세액", "합계", "평균", "단가", "율", "증감률",
        "커버", "부족수량", "필요수량", "건수", "품목수", "거래처수", "매입처수",
        "출고수", "매출액", "재고금액", "평가금액",
    )
    return any(w in s for w in measure_words)


def _ak_available_dimension_columns(df: pd.DataFrame) -> list[str]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []

    priority_words = (
        "명", "구분", "분류", "그룹", "등급", "상태", "판정", "위치", "처",
        "제조사", "거래처", "매입처", "재고적용처", "창고", "지역",
    )
    out: list[str] = []
    for col in [str(c).strip() for c in df.columns]:
        if not col or col in out:
            continue
        if _ak_is_measure_like_col(col):
            continue
        if "코드" in col and not col.endswith("명"):
            continue
        if _ak_is_dimension_col(col) or any(w in col for w in priority_words):
            out.append(col)
    return out


def find_similar_column(candidate: str, columns) -> str | None:
    """질문에서 추출한 OO 후보를 실제 현재표 컬럼 목록에서 찾는다."""
    cand = str(candidate or "").strip()
    if not cand:
        return None

    cols = [str(c).strip() for c in columns if str(c).strip()]
    if not cols:
        return None

    wants_code = "코드" in cand
    usable_cols = [
        c for c in cols
        if not _ak_is_measure_like_col(c)
        and (wants_code or "코드" not in c)
        and (_ak_is_dimension_col(c) or any(w in c for w in ("명", "구분", "분류", "그룹", "등급", "상태", "판정", "위치", "처", "창고")))
    ]

    norm_cand = _ak_norm_col_name(cand)
    norm_to_col = {_ak_norm_col_name(c): c for c in usable_cols}
    if norm_cand in norm_to_col:
        return norm_to_col[norm_cand]

    aliases = {
        "제조사": ("제조사명", "제조사", "제약사명", "제약사"),
        "제조사명": ("제조사명", "제조사", "제약사명", "제약사"),
        "제품그룹": ("제품그룹명", "제품그룹"),
        "제품그룹명": ("제품그룹명", "제품그룹"),
        "제품구분": ("제품구분명", "제품구분"),
        "제품구분명": ("제품구분명", "제품구분"),
        "제품분류": ("제품분류명", "제품분류"),
        "제품분류명": ("제품분류명", "제품분류"),
        "매입처": ("매입처명", "매입처"),
        "매입처명": ("매입처명", "매입처"),
        "재고적용처": ("재고적용처명", "재고적용처"),
        "재고적용처명": ("재고적용처명", "재고적용처"),
        "거래처": ("거래처명", "거래처", "매출처명", "매입처명", "재고적용처명"),
        "거래처명": ("거래처명", "거래처", "매출처명", "매입처명", "재고적용처명"),
        "매출구분": ("매출구분명", "매출구분"),
        "매출구분명": ("매출구분명", "매출구분"),
        "제품명": ("제품명", "품목명", "상품명"),
        "품목명": ("품목명", "제품명", "상품명"),
        "재고위치": ("재고위치", "재고위치명", "재고기준", "창고명"),
        "재고위치명": ("재고위치명", "재고위치", "재고기준", "창고명"),
    }
    for alias in aliases.get(cand, ()):
        hit = norm_to_col.get(_ak_norm_col_name(alias))
        if hit:
            return hit

    for col in usable_cols:
        n = _ak_norm_col_name(col)
        if norm_cand and (norm_cand in n or n in norm_cand):
            return col

    close = difflib.get_close_matches(norm_cand, list(norm_to_col.keys()), n=1, cutoff=0.88)
    if close:
        return norm_to_col[close[0]]

    return None


def handle_analytics_kpi_followup(
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
    분석/KPI 현재표 전용 후속분석.

    1차 대상:
    - 품목별 매출 추세 분석
    - 품목별 매출 추세 요약표
    - 품목별 매출 예상
    - 품목별 재고부족현황

    품목별 매출 추세 분석은 보통 기준월 월집계표이므로,
    일자/요일 분석은 하지 않고 월별/제품별/거래처성 컬럼 기준으로 집계한다.
    """
    t = str(query or "").strip()
    compact = re.sub(r"\s+", "", t)

    find_col = helpers["find_col"]
    to_num = helpers["to_num"]
    push_table = helpers["push_table"]
    push_notice = helpers["push_notice"]

    col_names = [str(c).strip() for c in df.columns]

    def available_dimension_labels() -> list[str]:
        return _ak_available_dimension_columns(df)

    def unsupported_dimension_notice(request_label: str) -> bool:
        available = available_dimension_labels()
        if available:
            available_text = "\n".join(f"- {c}" for c in available)
        else:
            available_text = ", ".join(col_names[:45]) or "표시 가능한 구분 컬럼 없음"
        return push_notice(
            title=f"현재표 {request_label}별 분석 불가",
            action=f"현재표 {request_label}별 분석 불가",
            message=(
                f"현재표에 \"{request_label}\" 컬럼이 없어 {request_label}별 분석을 만들 수 없습니다.\n\n"
                "현재표에서 가능한 구분은 다음과 같습니다:\n"
                f"{available_text}"
            ),
            query_summary=f"현재표 / {request_label}별 분석 불가 / 전체 {len(df):,}건 기준",
            source_query=t,
        )

    month_col = find_col(
        df,
        exact=("기준월", "년월", "월", "매출월"),
        include_any=("기준월", "년월", "매출월"),
        exclude_any=("평균", "최근"),
    )
    date_col = find_col(
        df,
        exact=("일자", "출고일자", "매출일자", "기준일자"),
        include_any=("일자", "날짜"),
        exclude_any=("등록", "수정"),
    )

    product_col = _ak_explicit_column(df, ("제품명", "품목명", "상품명"))
    product_code_col = _ak_explicit_column(df, ("제품코드", "품목코드", "상품코드"))
    spec_col = find_col(
        df,
        exact=("규격", "제품규격"),
        include_any=("규격",),
        exclude_any=("코드", "번호"),
    )
    maker_col = find_col(
        df,
        exact=("제조사명", "제조사", "제약사명", "제약사"),
        include_any=("제조사", "제약사"),
        exclude_any=("코드", "번호"),
    )
    maker_code_col = find_col(
        df,
        exact=("제조사코드", "제약사코드"),
        include_any=("제조사코드", "제약사코드"),
        exclude_any=("그룹", "분류", "구분"),
    )

    buy_col = find_col(
        df,
        exact=("매입처명", "발주처명", "입고처명"),
        include_any=("매입처", "발주처", "입고처"),
        exclude_any=("코드", "번호", "분류", "구분"),
    )
    buy_code_col = find_col(
        df,
        exact=("매입처코드", "발주처코드", "입고처코드"),
        include_any=("매입처코드", "발주처코드", "입고처코드"),
        exclude_any=("그룹", "분류", "구분"),
    )
    stock_vendor_col = find_col(
        df,
        exact=("재고적용처명", "재고적용처"),
        include_any=("재고적용처",),
        exclude_any=("코드", "번호", "분류", "구분"),
    )
    stock_vendor_code_col = find_col(
        df,
        exact=("재고적용처코드",),
        include_any=("재고적용처코드",),
        exclude_any=("그룹", "분류", "구분"),
    )
    vendor_col = find_col(
        df,
        exact=("거래처명", "매출처명", "매입처명", "재고적용처명"),
        include_any=("거래처", "매출처", "매입처", "재고적용처"),
        exclude_any=("코드", "번호", "분류", "구분"),
    )
    vendor_code_col = find_col(
        df,
        exact=("거래처코드", "매출처코드", "매입처코드", "재고적용처코드"),
        include_any=("거래처코드", "매출처코드", "매입처코드", "재고적용처코드"),
        exclude_any=("그룹", "분류", "구분"),
    )

    # find_col()이 매입처수/거래처수/재고적용처수 같은 집계 컬럼을
    # 명칭 차원으로 오인하지 않도록 보정한다.
    buy_col = _ak_clean_dimension_col(buy_col)
    stock_vendor_col = _ak_clean_dimension_col(stock_vendor_col)
    vendor_col = _ak_clean_dimension_col(vendor_col)

    qty_col = find_col(
        df,
        exact=("출고수량", "매출수량", "수량"),
        include_any=("출고수량", "매출수량", "수량"),
        exclude_any=("할증", "금액", "단가", "코드"),
    )
    bonus_qty_col = find_col(
        df,
        exact=("출고할증수량", "할증수량"),
        include_any=("할증수량",),
        exclude_any=("금액", "단가", "코드"),
    )
    supply_col = find_col(
        df,
        exact=("매출공급가액", "공급가액", "출고공급가액"),
        include_any=("매출공급가액", "공급가액", "출고공급가액"),
        exclude_any=("확정", "상세합", "차이", "단가"),
    )
    tax_col = find_col(
        df,
        exact=("매출세액", "세액", "출고세액"),
        include_any=("매출세액", "세액", "출고세액"),
        exclude_any=("확정", "상세합", "차이", "단가"),
    )
    total_col = find_col(
        df,
        exact=("매출합계", "매출금액", "합계금액", "총금액", "매출공급가액"),
        include_any=("매출합계", "매출금액", "합계금액", "총금액", "매출공급가액"),
        exclude_any=("단가", "율", "평균", "최근"),
    )
    count_col = find_col(
        df,
        exact=("집계건수", "건수"),
        include_any=("집계건수", "건수"),
        exclude_any=("품목수", "거래처수", "매입처수"),
    )

    dimension_query_words = (
        "제품그룹별",
        "제품그룹명별",
        "제품구분별",
        "제품구분명별",
        "제품분류별",
        "제품분류명별",
        "제조사별",
        "제조사명별",
        "매입처별",
        "매입처명별",
        "재고적용처별",
        "재고적용처명별",
        "매출구분별",
        "매출구분명별",
    )
    is_dimension_query = any(w in t or w in compact for w in dimension_query_words)
    groupby_candidate = extract_groupby_candidate(t)

    if not product_col and not month_col and not is_dimension_query and not groupby_candidate:
        return push_notice(
            title="현재표 분석/KPI 후속분석 불가",
            action="현재표 분석/KPI 후속분석 불가",
            message=(
                "현재표는 분석/KPI 표로 보이지만 제품명 또는 기준월 컬럼을 찾지 못했습니다.\n\n"
                f"현재표 기준 행수: {len(df):,}건\n"
                f"현재표 주요 컬럼: {', '.join(col_names[:40])}"
            ),
            query_summary="현재표 / 분석/KPI 후속분석 불가",
            source_query=t,
        )

    def _series(col: str | None) -> pd.Series:
        if col and col in df.columns:
            return to_num(df[col])
        return pd.Series([0] * len(df), index=df.index, dtype="float64")

    def _text_series(col: str | None) -> pd.Series:
        if col and col in df.columns:
            return df[col].fillna("").astype(str).str.strip()
        return pd.Series([""] * len(df), index=df.index, dtype="object")

    def _valid_name_mask(s: pd.Series) -> pd.Series:
        name = s.fillna("").astype(str).str.strip()
        bad = (
            name.eq("")
            | name.str.contains(r"^(?:합계|총계|소계|전체|TOTAL)$", case=False, regex=True, na=False)
        )
        return ~bad

    qty_s = _series(qty_col)
    bonus_qty_s = _series(bonus_qty_col)
    supply_s = _series(supply_col)
    tax_s = _series(tax_col)
    amount_s = _series(total_col) if total_col else supply_s + tax_s
    count_s = _series(count_col)

    def _base_work(group_col: str | None = None, group_label: str | None = None) -> pd.DataFrame:
        work = pd.DataFrame(
            {
                "_qty": qty_s,
                "_bonus_qty": bonus_qty_s,
                "_supply": supply_s,
                "_tax": tax_s,
                "_amount": amount_s,
                "_count": count_s,
                "_product": _text_series(product_col),
            },
            index=df.index,
        )
        if group_col and group_label:
            work[group_label] = _text_series(group_col)
            work = work[_valid_name_mask(work[group_label])].copy()
        return work

    def _month_text_series() -> pd.Series:
        raw = _text_series(month_col).str.replace(r"\D", "", regex=True).str[:6]
        return raw.str[:4] + "-" + raw.str[4:6]

    def _add_avg_price(out: pd.DataFrame) -> pd.DataFrame:
        if "매출공급가액" in out.columns and "출고수량" in out.columns:
            denom = pd.to_numeric(out["출고수량"], errors="coerce").replace(0, pd.NA)
            out["평균공급단가"] = (pd.to_numeric(out["매출공급가액"], errors="coerce") / denom).fillna(0).round(2)
        return out

    def _agg_work(work: pd.DataFrame, group_keys: list[str]) -> pd.DataFrame:
        out = (
            work.groupby(group_keys, dropna=False)
            .agg(
                행수=("_amount", "size"),
                집계건수=("_count", "sum"),
                품목수=("_product", "nunique"),
                출고수량=("_qty", "sum"),
                출고할증수량=("_bonus_qty", "sum"),
                매출공급가액=("_supply", "sum"),
                매출세액=("_tax", "sum"),
                매출합계=("_amount", "sum"),
            )
            .reset_index()
        )
        if count_col is None:
            out["집계건수"] = out["행수"]
        out = _add_avg_price(out)
        return out

    def _sort_insert_seq(out: pd.DataFrame, sort_col: str = "매출합계", ascending: bool = False) -> pd.DataFrame:
        if out.empty:
            return out
        if sort_col in out.columns:
            out = out.sort_values(sort_col, ascending=ascending).reset_index(drop=True)
        out.insert(0, "순번", range(1, len(out) + 1))
        return out

    def _with_optional_text_cols(work: pd.DataFrame, cols: list[tuple[str | None, str]]) -> pd.DataFrame:
        for src_col, label in cols:
            if src_col and src_col in df.columns:
                work[label] = df.loc[work.index, src_col].fillna("").astype(str).str.strip()
        return work


    # ------------------------------------------------------------
    # A) 품목별 매출 예상 전용 후속분석
    # - 일반 제품별/제조사별 매출 집계로 흘러가면 "예상" 요청인데
    #   action/컬럼이 "매출 분석"으로 표시되는 문제가 생긴다.
    # ------------------------------------------------------------
    compact_upper = compact.upper()
    has_top = _ak_has_top(t)

    is_shortage_source = (
        "품목별 재고부족현황" in str(source_action or "")
        or "부족등급" in col_names
        or "1개월부족수량" in col_names
        or "현재재고수량" in col_names
    )

    is_forecast_source = (
        (
            "품목별 매출 예상" in str(source_action or "")
            or "다음월예상매출" in col_names
            or "예상등급" in col_names
        )
        and not is_shortage_source
        and "부족" not in compact
    )

    if groupby_candidate:
        attr_col = find_similar_column(groupby_candidate, df.columns)
        if attr_col:
            has_group_top = _ak_has_top(t)
            if is_shortage_source:
                out = _ak_shortage_group_summary(
                    df=df,
                    group_cols=[attr_col],
                    group_label=attr_col,
                    top_n=top_n if has_group_top else 0,
                    has_top=has_group_top,
                    to_num=to_num,
                    product_col=product_code_col,
                )
                subject = "재고부족"
            else:
                out = _ak_forecast_group_summary(
                    df=df,
                    group_cols=[attr_col],
                    group_label=attr_col,
                    top_n=top_n if has_group_top else 0,
                    has_top=has_group_top,
                    to_num=to_num,
                    product_col=product_code_col,
                )
                subject = "매출"

            title = f"현재표 {attr_col}별 {subject} 분석"

            try:
                log.info(
                    "[chat.followup_table] current table column group detected query=%r candidate=%r attr=%r source_rows=%s",
                    t,
                    groupby_candidate,
                    attr_col,
                    len(df),
                )
            except Exception:
                pass

            return push_table(
                title=title,
                action=title,
                df=out,
                query_summary=f"현재표 / {attr_col}별 {subject} 분석 / 전체 {len(df):,}건 기준",
                source_query=t,
                source_table_key=table_key,
                source_rows=len(df),
                display_limit=top_n if has_group_top else None,
            )

        available = _ak_available_dimension_columns(df)
        try:
            log.info(
                "[chat.followup_table] current table column group not found query=%r candidate=%r available=%r",
                t,
                groupby_candidate,
                ",".join(available[:40]),
            )
        except Exception:
            pass
        return unsupported_dimension_notice(groupby_candidate)

    if is_forecast_source:
        wants_forecast_product = (
            any(w in compact for w in ("제품별", "품목별", "제품", "품목"))
            and any(w in compact for w in ("예상", "매출", "금액", "분석", "요약", "집계"))
        ) or ("예상" in compact and has_top)

        wants_forecast_maker = "제조사별" in compact and any(w in compact for w in ("예상", "분석", "요약", "집계", "매출"))

        wants_forecast_vendor = any(
            w in compact
            for w in ("거래처별", "매입처별", "발주처별", "입고처별", "재고적용처별")
        ) and any(w in compact for w in ("예상", "분석", "요약", "집계", "매출"))

        wants_forecast_grade = any(
            w in compact
            for w in ("예상등급별", "예상등급요약", "예상등급분석")
        )

        if wants_forecast_product:
            out = _ak_forecast_product_table(
                df=df,
                top_n=top_n,
                has_top=has_top,
                to_num=to_num,
            )
            title = f"현재표 제품별 예상 TOP {top_n}" if has_top else "현재표 제품별 예상 분석"
            log.info(
                "[chat.followup_table] analytics kpi forecast product built source_rows=%s rows=%s table_key=%s",
                len(df),
                len(out),
                table_key,
            )
            return push_table(
                title=title,
                action=title,
                df=out,
                query_summary=(
                    f"현재표 / 제품별 예상 TOP {top_n} / 전체 {len(df):,}건 기준"
                    if has_top
                    else f"현재표 / 제품별 예상 분석 / 전체 {len(df):,}건 기준"
                ),
                source_query=t,
                source_table_key=table_key,
                source_rows=len(df),
                display_limit=top_n if has_top else None,
            )

        if wants_forecast_maker:
            if not maker_col:
                return push_notice(
                    title="현재표 제조사별 예상 분석 불가",
                    action="현재표 제조사별 예상 분석 불가",
                    message="현재표에서 제조사명 컬럼을 찾지 못했습니다.",
                    query_summary=f"현재표 / 제조사별 예상 분석 불가 / 전체 {len(df):,}건 기준",
                    source_query=t,
                )

            group_cols = [maker_col]
            if maker_code_col:
                group_cols = [maker_code_col, maker_col]

            out = _ak_forecast_group_summary(
                df=df,
                group_cols=group_cols,
                group_label="제조사명",
                top_n=top_n,
                has_top=has_top,
                to_num=to_num,
                product_col=product_code_col,
            )
            title = f"현재표 제조사별 예상 TOP {top_n}" if has_top else "현재표 제조사별 예상 분석"
            log.info(
                "[chat.followup_table] analytics kpi forecast maker built source_rows=%s rows=%s table_key=%s",
                len(df),
                len(out),
                table_key,
            )
            return push_table(
                title=title,
                action=title,
                df=out,
                query_summary=f"현재표 / 제조사별 예상 분석 / 전체 {len(df):,}건 기준",
                source_query=t,
                source_table_key=table_key,
                source_rows=len(df),
                display_limit=top_n if has_top else None,
            )

        if wants_forecast_vendor:
            if "재고적용처" in t:
                group_col = stock_vendor_col
                code_col = stock_vendor_code_col
                group_label = "재고적용처명"
            elif any(w in t for w in ("매입처", "발주처", "입고처")):
                group_col = buy_col
                code_col = buy_code_col
                group_label = "매입처명"
            else:
                group_col = vendor_col
                code_col = vendor_code_col
                group_label = str(vendor_col or "거래처명")

            group_col = _ak_clean_dimension_col(group_col)
            if not group_col:
                code_col = None
                return push_notice(
                    title="현재표 매입처별 예상 분석 불가",
                    action="현재표 매입처별 예상 분석 불가",
                    message=(
                        "현재표에는 매입처명 컬럼이 없어 매입처별 예상 분석을 만들 수 없습니다. "
                        "현재 표에는 매입처수만 있으며, 매입처별 분석을 하려면 매입처명 단위 원자료가 필요합니다."
                        f"현재표 주요 컬럼: {', '.join(col_names[:45])}"
                    ),
                    query_summary=f"현재표 / 매입처별 예상 분석 불가 / 전체 {len(df):,}건 기준",
                    source_query=t,
                )

            group_cols = [group_col]
            if code_col:
                group_cols = [code_col, group_col]

            out = _ak_forecast_group_summary(
                df=df,
                group_cols=group_cols,
                group_label=group_label,
                top_n=top_n,
                has_top=has_top,
                to_num=to_num,
                product_col=product_code_col,
            )
            base = group_label.replace("명", "")
            title = f"현재표 {base}별 예상 TOP {top_n}" if has_top else f"현재표 {base}별 예상 분석"
            log.info(
                "[chat.followup_table] analytics kpi forecast vendor built source_rows=%s rows=%s group_label=%s table_key=%s",
                len(df),
                len(out),
                group_label,
                table_key,
            )
            return push_table(
                title=title,
                action=title,
                df=out,
                query_summary=f"현재표 / {base}별 예상 분석 / 전체 {len(df):,}건 기준",
                source_query=t,
                source_table_key=table_key,
                source_rows=len(df),
                display_limit=top_n if has_top else None,
            )

        if wants_forecast_grade:
            grade_col = find_col(
                df,
                exact=("예상등급",),
                include_any=("예상등급",),
            )

            if not grade_col:
                return push_notice(
                    title="현재표 예상등급별 요약 불가",
                    action="현재표 예상등급별 요약 불가",
                    message="현재표에서 예상등급 컬럼을 찾지 못했습니다.",
                    query_summary=f"현재표 / 예상등급별 요약 불가 / 전체 {len(df):,}건 기준",
                    source_query=t,
                )

            out = _ak_forecast_group_summary(
                df=df,
                group_cols=[grade_col],
                group_label="예상등급",
                top_n=0,
                has_top=False,
                to_num=to_num,
                product_col=product_code_col,
            )

            order_map = {
                "상승예상": 1,
                "안정예상": 2,
                "감소예상": 3,
                "신규확인": 4,
                "반품주의": 5,
                "자료부족": 6,
                "": 99,
            }

            if "예상등급" in out.columns:
                sort_metric = next(
                    (
                        c for c in (
                            "다음월예상매출",
                            "3개월예상매출",
                            "6개월예상매출",
                            "총매출액",
                            "총매출공급가액",
                            "품목수",
                            "행수",
                        )
                        if c in out.columns
                    ),
                    None,
                )

                out["_정렬"] = out["예상등급"].map(
                    lambda x: order_map.get(str(x or "").strip(), 90)
                )

                if sort_metric:
                    out = (
                        out.sort_values(["_정렬", sort_metric], ascending=[True, False])
                        .drop(columns=["_정렬"])
                        .reset_index(drop=True)
                    )
                else:
                    out = (
                        out.sort_values(["_정렬"], ascending=[True])
                        .drop(columns=["_정렬"])
                        .reset_index(drop=True)
                    )

                if "순번" in out.columns:
                    out = out.drop(columns=["순번"])

                out.insert(0, "순번", range(1, len(out) + 1))

            title = "현재표 예상등급별 요약"

            log.info(
                "[chat.followup_table] analytics kpi forecast grade built source_rows=%s rows=%s table_key=%s",
                len(df),
                len(out),
                table_key,
            )

            return push_table(
                title=title,
                action=title,
                df=out,
                query_summary=f"현재표 / 예상등급별 요약 / 전체 {len(df):,}건 기준",
                source_query=t,
                source_table_key=table_key,
                source_rows=len(df),
                display_limit=None,
            )

    # ------------------------------------------------------------
    # B) 품목별 재고부족현황 전용 후속분석
    # - 부족 요청을 일반 매출 분석으로 보내지 않고 부족수량/부족등급 기준으로 처리한다.
    # ------------------------------------------------------------
    is_shortage_source = (
        "품목별 재고부족현황" in str(source_action or "")
        or "부족등급" in col_names
        or "1개월부족수량" in col_names
        or "현재재고수량" in col_names
    )

    if is_shortage_source:
        wants_shortage_product = (
            "부족" in compact
            and (
                has_top
                or any(w in compact for w in ("부족수량", "제품", "품목", "목록", "조회", "상세"))
            )
            and not any(w in compact for w in ("제조사별", "매입처별", "거래처별", "재고적용처별"))
        )

        wants_shortage_maker = "제조사별" in compact and "부족" in compact

        wants_shortage_vendor = any(
            w in compact
            for w in ("거래처별", "매입처별", "발주처별", "입고처별", "재고적용처별")
        ) and "부족" in compact

        wants_shortage_grade = any(w in compact for w in ("부족등급별", "부족등급요약", "부족등급분석"))

        shortage_grade_filter = _ak_extract_shortage_grade_filter(t)

        if _ak_is_numeric_measure_condition_query(t):
            return False

        # "부족등급 재고없음 상세히"처럼 특정 부족등급값을 지정한 경우는
        # 일반 부족수량 제품목록보다 먼저 처리한다.
        # 그렇지 않으면 질문의 "상세" 단어 때문에 전체표가 1개월부족수량 목록으로 흐른다.
        if shortage_grade_filter:
            grade_col = find_col(df, exact=("부족등급",), include_any=("부족등급",))
            if not grade_col:
                return push_notice(
                    title="현재표 부족등급 제품 목록 불가",
                    action="현재표 부족등급 제품 목록 불가",
                    message="현재표에서 부족등급 컬럼을 찾지 못했습니다.",
                    query_summary=f"현재표 / 부족등급 제품 목록 불가 / 전체 {len(df):,}건 기준",
                    source_query=t,
                )

            filtered_df = _ak_filter_by_shortage_grade(df, grade_col, shortage_grade_filter)
            filtered_rows = int(len(filtered_df))

            if filtered_df.empty:
                return push_notice(
                    title=f"현재표 부족등급 {shortage_grade_filter} 제품 없음",
                    action=f"현재표 부족등급 {shortage_grade_filter} 제품 없음",
                    message=f"현재표에서 부족등급이 '{shortage_grade_filter}'인 자료가 없습니다.",
                    query_summary=(
                        f"현재표 / 부족등급={shortage_grade_filter} / "
                        f"전체 {len(df):,}건 중 0건"
                    ),
                    source_query=t,
                )

            out, sort_col = _ak_shortage_product_table(
                df=filtered_df,
                top_n=top_n,
                has_top=True if has_top else False,
                to_num=to_num,
                query=t,
            )

            title = (
                f"현재표 부족등급 {shortage_grade_filter} TOP {top_n}"
                if has_top
                else f"현재표 부족등급 {shortage_grade_filter} 제품 목록"
            )

            log.info(
                "[chat.followup_table] analytics kpi shortage grade detail built source_rows=%s filtered_rows=%s rows=%s grade=%s sort_col=%s table_key=%s",
                len(df),
                filtered_rows,
                len(out),
                shortage_grade_filter,
                sort_col,
                table_key,
            )

            return push_table(
                title=title,
                action=title,
                df=out,
                query_summary=(
                    f"현재표 / 부족등급={shortage_grade_filter} / "
                    f"전체 {len(df):,}건 중 {filtered_rows:,}건 기준"
                ),
                source_query=t,
                source_table_key=table_key,
                source_rows=len(df),
                display_limit=top_n if has_top else None,
            )

        if wants_shortage_product:
            out, sort_col = _ak_shortage_product_table(
                df=df,
                top_n=top_n,
                has_top=True if has_top else False,
                to_num=to_num,
                query=t,
            )
            title = f"현재표 {sort_col} TOP {top_n}" if has_top else f"현재표 {sort_col} 제품 목록"
            log.info(
                "[chat.followup_table] analytics kpi shortage product built source_rows=%s rows=%s sort_col=%s table_key=%s",
                len(df),
                len(out),
                sort_col,
                table_key,
            )
            return push_table(
                title=title,
                action=title,
                df=out,
                query_summary=(
                    f"현재표 / {sort_col} TOP {top_n} / 전체 {len(df):,}건 기준"
                    if has_top
                    else f"현재표 / {sort_col} 제품 목록 / 전체 {len(df):,}건 기준"
                ),
                source_query=t,
                source_table_key=table_key,
                source_rows=len(df),
                display_limit=top_n if has_top else None,
            )

        if wants_shortage_maker:
            if not maker_col:
                return push_notice(
                    title="현재표 제조사별 부족 분석 불가",
                    action="현재표 제조사별 부족 분석 불가",
                    message="현재표에서 제조사명 컬럼을 찾지 못했습니다.",
                    query_summary=f"현재표 / 제조사별 부족 분석 불가 / 전체 {len(df):,}건 기준",
                    source_query=t,
                )

            group_cols = [maker_col]
            if maker_code_col:
                group_cols = [maker_code_col, maker_col]

            out = _ak_shortage_group_summary(
                df=df,
                group_cols=group_cols,
                group_label="제조사명",
                top_n=top_n,
                has_top=has_top,
                to_num=to_num,
                product_col=product_code_col,
            )
            title = f"현재표 제조사별 부족 TOP {top_n}" if has_top else "현재표 제조사별 부족 분석"
            log.info(
                "[chat.followup_table] analytics kpi shortage maker built source_rows=%s rows=%s table_key=%s",
                len(df),
                len(out),
                table_key,
            )
            return push_table(
                title=title,
                action=title,
                df=out,
                query_summary=f"현재표 / 제조사별 부족 분석 / 전체 {len(df):,}건 기준",
                source_query=t,
                source_table_key=table_key,
                source_rows=len(df),
                display_limit=top_n if has_top else None,
            )

        if wants_shortage_vendor:
            if "재고적용처" in t:
                group_col = stock_vendor_col
                code_col = stock_vendor_code_col
                group_label = "재고적용처명"
            elif any(w in t for w in ("매입처", "발주처", "입고처")):
                group_col = buy_col
                code_col = buy_code_col
                group_label = "매입처명"
            else:
                group_col = vendor_col
                code_col = vendor_code_col
                group_label = str(vendor_col or "거래처명")

            group_col = _ak_clean_dimension_col(group_col)
            if not group_col:
                code_col = None
                return push_notice(
                    title="현재표 매입처별 부족 분석 불가",
                    action="현재표 매입처별 부족 분석 불가",
                    message=(
                        "현재표에는 매입처명 컬럼이 없어 매입처별 부족 분석을 만들 수 없습니다. "
                        "현재 표에는 매입처수만 있으며, 매입처별 분석을 하려면 매입처명 단위 원자료가 필요합니다."
                        f"현재표 주요 컬럼: {', '.join(col_names[:45])}"
                    ),
                    query_summary=f"현재표 / 매입처별 부족 분석 불가 / 전체 {len(df):,}건 기준",
                    source_query=t,
                )

            group_cols = [group_col]
            if code_col:
                group_cols = [code_col, group_col]

            out = _ak_shortage_group_summary(
                df=df,
                group_cols=group_cols,
                group_label=group_label,
                top_n=top_n,
                has_top=has_top,
                to_num=to_num,
                product_col=product_code_col,
            )
            base = group_label.replace("명", "")
            title = f"현재표 {base}별 부족 TOP {top_n}" if has_top else f"현재표 {base}별 부족 분석"
            log.info(
                "[chat.followup_table] analytics kpi shortage vendor built source_rows=%s rows=%s group_label=%s table_key=%s",
                len(df),
                len(out),
                group_label,
                table_key,
            )
            return push_table(
                title=title,
                action=title,
                df=out,
                query_summary=f"현재표 / {base}별 부족 분석 / 전체 {len(df):,}건 기준",
                source_query=t,
                source_table_key=table_key,
                source_rows=len(df),
                display_limit=top_n if has_top else None,
            )

        if wants_shortage_grade:
            grade_col = find_col(df, exact=("부족등급",), include_any=("부족등급",))
            if not grade_col:
                return push_notice(
                    title="현재표 부족등급별 요약 불가",
                    action="현재표 부족등급별 요약 불가",
                    message="현재표에서 부족등급 컬럼을 찾지 못했습니다.",
                    query_summary=f"현재표 / 부족등급별 요약 불가 / 전체 {len(df):,}건 기준",
                    source_query=t,
                )
            out = _ak_shortage_group_summary(
                df=df,
                group_cols=[grade_col],
                group_label="부족등급",
                top_n=0,
                has_top=False,
                to_num=to_num,
                product_col=product_code_col,
            )
            title = "현재표 부족등급별 요약"
            log.info(
                "[chat.followup_table] analytics kpi shortage grade built source_rows=%s rows=%s table_key=%s",
                len(df),
                len(out),
                table_key,
            )
            return push_table(
                title=title,
                action=title,
                df=out,
                query_summary=f"현재표 / 부족등급별 요약 / 전체 {len(df):,}건 기준",
                source_query=t,
                source_table_key=table_key,
                source_rows=len(df),
                display_limit=None,
            )

    # ------------------------------------------------------------
    # C) 일반 매출/KPI 그룹 요약
    # - 품목별 매출 추세 요약표 / 품목별 매출 추세 분석용
    # - 매출예상/재고부족 전용 분기에서 처리되지 않은 경우만 여기로 온다.
    # ------------------------------------------------------------

    wants_sales_maker = (
        any(marker in compact for marker in ("제조사별", "제조사분석"))
        and any(w in compact for w in ("매출", "분석", "요약", "집계", "금액", "TOP", "상위"))
    )

    if wants_sales_maker:
        if not maker_col:
            return push_notice(
                title="현재표 제조사별 매출 분석 불가",
                action="현재표 제조사별 매출 분석 불가",
                message="현재표에서 제조사명 컬럼을 찾지 못했습니다.",
                query_summary=f"현재표 / 제조사별 매출 분석 불가 / 전체 {len(df):,}건 기준",
                source_query=t,
            )

        group_cols = [maker_col]
        if maker_code_col:
            group_cols = [maker_code_col, maker_col]

        has_top = _ak_has_top(t)

        out = _ak_forecast_group_summary(
            df=df,
            group_cols=group_cols,
            group_label="제조사명",
            top_n=top_n,
            has_top=has_top,
            to_num=to_num,
            product_col=product_code_col,
        )

        title = f"현재표 제조사별 매출 TOP {top_n}" if has_top else "현재표 제조사별 매출 분석"

        log.info(
            "[chat.followup_table] analytics kpi maker summary built source_rows=%s rows=%s table_key=%s",
            len(df),
            len(out),
            table_key,
        )

        return push_table(
            title=title,
            action=title,
            df=out,
            query_summary=f"현재표 / 제조사별 매출 분석 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=top_n if has_top else None,
        )

    # 판정결과별 요약
    if "판정결과" in t or "판정결과별" in compact:
        result_col = find_col(
            df,
            exact=("판정결과",),
            include_any=("판정결과",),
        )

        if not result_col:
            return push_notice(
                title="현재표 판정결과별 요약 불가",
                action="현재표 판정결과별 요약 불가",
                message="현재표에서 판정결과 컬럼을 찾지 못했습니다.",
                query_summary=f"현재표 / 판정결과별 요약 불가 / 전체 {len(df):,}건 기준",
                source_query=t,
            )

        out = _ak_forecast_group_summary(
            df=df,
            group_cols=[result_col],
            group_label="판정결과",
            top_n=0,
            has_top=False,
            to_num=to_num,
            product_col=product_code_col,
        )

        title = "현재표 판정결과별 요약"

        log.info(
            "[chat.followup_table] analytics kpi result summary built source_rows=%s rows=%s table_key=%s",
            len(df),
            len(out),
            table_key,
        )

        return push_table(
            title=title,
            action=title,
            df=out,
            query_summary=f"현재표 / 판정결과별 요약 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=None,
        )


    wants_trend_summary = any(
        w in compact
        for w in ("추세판정별", "추세판정요약", "추세판정분석", "판정별", "추세별")
    )

    if wants_trend_summary:
        trend_col = find_col(
            df,
            exact=("추세판정",),
            include_any=("추세판정",),
        )

        if not trend_col:
            return push_notice(
                title="현재표 추세판정별 요약 불가",
                action="현재표 추세판정별 요약 불가",
                message="현재표에서 추세판정 컬럼을 찾지 못했습니다.",
                query_summary=f"현재표 / 추세판정별 요약 불가 / 전체 {len(df):,}건 기준",
                source_query=t,
            )

        out = _ak_forecast_group_summary(
            df=df,
            group_cols=[trend_col],
            group_label="추세판정",
            top_n=0,
            has_top=False,
            to_num=to_num,
            product_col=product_code_col,
        )

        order_map = {
            "증가": 1,
            "신규/증가": 2,
            "안정": 3,
            "감소": 4,
            "반품주의": 5,
            "자료부족": 6,
            "": 99,
        }

        if "추세판정" in out.columns:
            sort_metric = next(
                (
                    c for c in (
                        "매출합계",
                        "총매출액",
                        "총매출공급가액",
                        "매출공급가액",
                        "월평균매출",
                        "최근3개월평균매출",
                        "최근6개월평균매출",
                        "품목수",
                        "행수",
                    )
                    if c in out.columns
                ),
                None,
            )

            out["_정렬"] = out["추세판정"].map(
                lambda x: order_map.get(str(x or "").strip(), 90)
            )

            if sort_metric:
                out = (
                    out.sort_values(["_정렬", sort_metric], ascending=[True, False])
                    .drop(columns=["_정렬"])
                    .reset_index(drop=True)
                )
            else:
                out = (
                    out.sort_values(["_정렬"], ascending=True)
                    .drop(columns=["_정렬"])
                    .reset_index(drop=True)
                )

            if "순번" in out.columns:
                out = out.drop(columns=["순번"])
            out.insert(0, "순번", range(1, len(out) + 1))

        title = "현재표 추세판정별 요약"

        log.info(
            "[chat.followup_table] analytics kpi trend summary built source_rows=%s rows=%s table_key=%s",
            len(df),
            len(out),
            table_key,
        )

        return push_table(
            title=title,
            action=title,
            df=out,
            query_summary=f"현재표 / 추세판정별 요약 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=None,
        )


    # 0) 일자/요일 요청: 품목별 매출 추세 분석은 월 기준 표라서 일자/요일 분석 불가
    wants_day_or_weekday = (
        any(w in t for w in ("일자", "날짜", "요일"))
        and any(w in t for w in ("금액", "매출", "수량", "최고", "가장", "많은"))
    )
    if wants_day_or_weekday and not date_col:
        return push_notice(
            title="현재표 일자/요일 분석 불가",
            action="현재표 일자/요일 분석 불가",
            message=(
                "현재표는 분석/KPI의 월 기준 표입니다. 일자/요일 컬럼이 없어 "
                "'가장 많은 일자와 요일' 분석은 할 수 없습니다.\n\n"
                "대신 아래처럼 조회해 주세요.\n"
                "- 현재표 월별 요약\n"
                "- 현재표 매출이 가장 많은 월"
            ),
            query_summary=f"현재표 / 일자·요일 분석 불가 / 전체 {len(df):,}건 기준",
            source_query=t,
        )

    # 1) 월별 요약 / 월별 매출
    wants_month_summary = (
        "월별" in t
        and any(w in t for w in ("요약", "분석", "집계", "매출", "금액", "수량"))
    ) or any(w in compact for w in ("월별요약", "월별매출", "월별금액", "월별수량"))

    wants_top_month = (
        "월" in t
        and any(w in t for w in ("최고", "가장", "많은", "1위"))
        and any(w in t for w in ("매출", "금액", "수량"))
    )

    if wants_month_summary or wants_top_month:
        if not month_col:
            return push_notice(
                title="현재표 월별 요약 불가",
                action="현재표 월별 요약 불가",
                message="현재표에는 월별 요약에 필요한 기준월 컬럼이 없습니다.",
                query_summary="현재표 / 월별 요약 불가",
                source_query=t,
            )

        work = _base_work()
        work["월"] = _month_text_series()
        work = work[work["월"].str.len() == 7].copy()
        if work.empty:
            return False

        out = _agg_work(work, ["월"])

        if wants_top_month:
            out = _sort_insert_seq(out, "매출합계", ascending=False).head(1).copy()
            title = "현재표 매출 최고 월"
            query_summary = f"현재표 / 매출 최고 월 / 전체 {len(df):,}건 기준"
            display_limit = 1
        else:
            out = out.sort_values("월", ascending=True).reset_index(drop=True)
            out.insert(0, "순번", range(1, len(out) + 1))
            title = "현재표 월별 매출 요약"
            query_summary = f"현재표 / 월별 매출 요약 / 전체 {len(df):,}건 기준"
            display_limit = None

        log.info(
            "[chat.followup_table] analytics kpi monthly summary built source_rows=%s rows=%s table_key=%s",
            len(df),
            len(out),
            table_key,
        )

        return push_table(
            title=title,
            action=title,
            df=out,
            query_summary=query_summary,
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=display_limit,
        )

    # A-0) 현재표에 없는 예상/부족 컬럼 요청은 일반 매출 TOP으로 흘리지 않는다.
    asks_forecast = any(w in compact for w in ("예상등급", "예상매출", "다음월예상매출"))
    asks_shortage = any(w in compact for w in ("부족등급", "재고부족", "부족품목", "부족수량"))

    if asks_forecast and not any(c in df.columns for c in ("예상등급", "다음월예상매출", "3개월예상매출", "6개월예상매출")):
        return push_notice(
            title="현재표 예상 분석 불가",
            action="현재표 예상 분석 불가",
            message=(
                "현재표는 [품목별 매출 추세 분석] 결과입니다.\n"
                "이 표에는 예상등급/예상매출 컬럼이 없어 예상등급별 요약이나 예상매출 TOP을 만들 수 없습니다.\n\n"
                "예상 분석은 아래처럼 먼저 조회해 주세요.\n"
                "- 품목별 매출 예상 2026 조회\n"
                "- 현재표 예상등급별 요약\n"
                "- 현재표 예상매출 TOP 20"
            ),
            query_summary=f"현재표 / 예상 분석 불가 / 원본={source_action} / 전체 {len(df):,}건 기준",
            source_query=t,
        )

    if asks_shortage and not any(c in df.columns for c in ("부족등급", "1개월부족수량", "2개월부족수량", "3개월부족수량", "현재재고수량")):
        return push_notice(
            title="현재표 재고부족 분석 불가",
            action="현재표 재고부족 분석 불가",
            message=(
                "현재표는 [품목별 매출 추세 분석] 결과입니다.\n"
                "이 표에는 부족등급/부족수량/현재재고수량 컬럼이 없어 재고부족 품목 TOP을 만들 수 없습니다.\n\n"
                "재고부족 분석은 아래처럼 먼저 조회해 주세요.\n"
                "- 품목별 재고부족현황 2026 조회\n"
                "- 현재표 부족등급별 요약\n"
                "- 현재표 재고부족 품목 TOP 20"
            ),
            query_summary=f"현재표 / 재고부족 분석 불가 / 원본={source_action} / 전체 {len(df):,}건 기준",
            source_query=t,
        )

    if any(w in t or w in compact for w in ("매출구분별", "매출구분명별", "매출구분")):
        sales_type_col = find_col(
            df,
            exact=("매출구분", "매출구분명"),
            include_any=("매출구분",),
            exclude_any=("코드", "번호"),
        )
        if not sales_type_col:
            return unsupported_dimension_notice("매출구분")

    # 제품/거래처 속성별 분석: 제품그룹별 / 제품구분별 / 제품분류별 / 제조사별 / 매입처별 / 재고적용처별
    product_attr_specs = [
        (("제품그룹별", "제품그룹명별"), "제품그룹명", ("제품그룹명", "제품그룹")),
        (("제품구분별", "제품구분명별"), "제품구분명", ("제품구분명", "제품구분")),
        (("제품분류별", "제품분류명별"), "제품분류명", ("제품분류명", "제품분류")),
        (("제조사별", "제조사명별"), "제조사명", ("제조사명", "제조사")),
        (("매입처별", "매입처명별"), "매입처명", ("매입처명", "매입처")),
        (("재고적용처별", "재고적용처명별"), "재고적용처명", ("재고적용처명", "재고적용처")),
    ]

    for triggers, label, exact_cols in product_attr_specs:
        trigger = triggers[0]
        if any(x in t or x in compact for x in triggers):
            group_col = find_col(
                df,
                exact=exact_cols,
                include_any=exact_cols,
                exclude_any=("코드", "번호"),
            )

            if not group_col:
                return unsupported_dimension_notice(label)

            out = _ak_forecast_group_summary(
                df=df,
                group_cols=[group_col],
                group_label=label,
                top_n=top_n if _ak_has_top(t) else 0,
                has_top=_ak_has_top(t),
                to_num=to_num,
                product_col=product_code_col,
            )
            
            title = f"현재표 {label.replace('명', '')}별 매출 분석"

            log.info(
                "[chat.followup_table] analytics kpi product attr summary built attr=%s source_rows=%s rows=%s table_key=%s",
                label,
                len(df),
                len(out),
                table_key,
            )

            return push_table(
                title=title,
                action=title,
                df=out,
                query_summary=f"현재표 / {label.replace('명', '')}별 매출 분석 / 전체 {len(df):,}건 기준",
                source_query=t,
                source_table_key=table_key,
                source_rows=len(df),
                display_limit=top_n if _ak_has_top(t) else None,
            )


    # 2) 제품별/품목별 TOP N
    is_product_attr_query = any(
        w in compact
        for w in (
            "제품그룹별",
            "제품그룹명별",
            "제품구분별",
            "제품구분명별",
            "제품분류별",
            "제품분류명별",
            "제조사별",
            "제조사명별",
            "매입처별",
            "매입처명별",
            "재고적용처별",
            "재고적용처명별",
            "매출구분별",
            "매출구분명별",
        )
    )

    wants_product_top = (
        not is_product_attr_query
        and ("제품별" in t or "품목별" in t or "제품" in t or "품목" in t)
        and any(w in t for w in ("TOP", "top", "상위", "분석", "집계", "요약", "매출", "금액", "수량"))
    )


    if wants_product_top:
        if not product_col:
            return push_notice(
                title="현재표 제품별 분석 불가",
                action="현재표 제품별 분석 불가",
                message="현재표에는 제품별 분석에 필요한 제품명/품목명 컬럼이 없습니다.",
                query_summary="현재표 / 제품별 분석 불가",
                source_query=t,
            )

        work = _base_work(product_col, "제품명")
        work = _with_optional_text_cols(
            work,
            [
                (product_code_col, "제품코드"),
                (spec_col, "규격"),
                (maker_code_col, "제조사코드"),
                (maker_col, "제조사명"),
            ],
        )

        group_cols = []
        for c in ("제품코드", "제품명", "규격", "제조사코드", "제조사명"):
            if c in work.columns and c not in group_cols:
                group_cols.append(c)
        if not group_cols:
            group_cols = ["제품명"]

        out = _agg_work(work, group_cols)
        out = _sort_insert_seq(out, "매출합계", ascending=False)

        has_top = any(w in t for w in ("TOP", "top", "상위"))
        out2 = out.head(top_n).copy() if has_top else out
        title = f"현재표 제품별 매출 TOP {top_n}" if has_top else "현재표 제품별 매출 분석"
        query_summary = (
            f"현재표 / 제품별 매출 TOP {top_n} / 전체 {len(df):,}건 기준"
            if has_top
            else f"현재표 / 제품별 매출 분석 / 전체 {len(df):,}건 기준"
        )

        log.info(
            "[chat.followup_table] analytics kpi product top built source_rows=%s rows=%s table_key=%s",
            len(df),
            len(out2),
            table_key,
        )

        return push_table(
            title=title,
            action=title,
            df=out2,
            query_summary=query_summary,
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=top_n if has_top else None,
        )

    # 3) 거래처성 컬럼별 분석
    wants_vendor_summary = any(
        w in t
        for w in (
            "거래처별",
            "매입처별",
            "발주처별",
            "입고처별",
            "재고적용처별",
        )
    ) and any(w in t for w in ("분석", "집계", "요약", "매출", "금액", "수량", "TOP", "top", "상위"))

    if wants_vendor_summary:
        if "재고적용처" in t:
            group_col = stock_vendor_col
            code_col = stock_vendor_code_col
            group_label = "재고적용처명"
        elif any(w in t for w in ("매입처", "발주처", "입고처")):
            group_col = buy_col
            code_col = buy_code_col
            group_label = "매입처명"
        else:
            # 품목별 매출 추세표에는 일반 거래처명이 없을 수 있으므로
            # 거래처별 요청은 매입처명 → 재고적용처명 → 거래처명 순으로 해석한다.
            if buy_col:
                group_col = buy_col
                code_col = buy_code_col
                group_label = "매입처명"
            elif stock_vendor_col:
                group_col = stock_vendor_col
                code_col = stock_vendor_code_col
                group_label = "재고적용처명"
            else:
                group_col = vendor_col
                code_col = vendor_code_col
                group_label = "거래처명"

        if not group_col:
            return push_notice(
                title="현재표 거래처별 분석 불가",
                action="현재표 거래처별 분석 불가",
                message=(
                    "현재표에는 거래처별 분석에 사용할 거래처명/매입처명/재고적용처명 컬럼이 없습니다.\n\n"
                    f"현재표 주요 컬럼: {', '.join(col_names[:40])}"
                ),
                query_summary="현재표 / 거래처별 분석 불가",
                source_query=t,
            )

        work = _base_work(group_col, group_label)
        if code_col and code_col in df.columns:
            code_label = group_label.replace("명", "코드")
            work[code_label] = df.loc[work.index, code_col].fillna("").astype(str).str.strip()
            group_cols = [code_label, group_label]
        else:
            group_cols = [group_label]

        out = _agg_work(work, group_cols)
        out = _sort_insert_seq(out, "매출합계", ascending=False)

        has_top = any(w in t for w in ("TOP", "top", "상위"))
        out2 = out.head(top_n).copy() if has_top else out
        base_title = f"현재표 {group_label.replace('명', '')}별 매출 분석"
        title = f"{base_title} TOP {top_n}" if has_top else base_title

        log.info(
            "[chat.followup_table] analytics kpi vendor summary built source_rows=%s rows=%s group_label=%s table_key=%s",
            len(df),
            len(out2),
            group_label,
            table_key,
        )

        return push_table(
            title=title,
            action=title,
            df=out2,
            query_summary=f"현재표 / {group_label.replace('명', '')}별 매출 분석 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=top_n if has_top else None,
        )

    return False
