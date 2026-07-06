# app/ui/current_table_followups/validation.py
# 입고↔세금계산서 검증 / 출고↔세금계산서 검증 /
# 입고↔거래명세서 검증 / 출고↔거래명세서 검증 현재표 전용 후속분석
# Create 2026-06-08
# 현재표 action이 검증이면:
# 입고↔세금계산서 검증       → validation
# 출고↔세금계산서 검증       → validation
# 입고↔거래명세서 검증       → validation
# 출고↔거래명세서 검증       → validation



from __future__ import annotations

from typing import Any, Callable

import pandas as pd


def handle_validation_followup(
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
    입고↔세금계산서 검증 / 출고↔세금계산서 검증 /
    입고↔거래명세서 검증 / 출고↔거래명세서 검증 현재표 전용 후속분석.

    현재표 action이 검증이면:
    - 거래처별 계산서금액/매출/불일치 → 거래처별 불일치 분석
    - 제품별 매출 TOP/계산서금액/불일치 → 제품별 불일치 분석
    - 월별 계산서금액/불일치 → 월별 불일치 분석
    - 일자별 계산서금액/불일치 → 일자별 불일치 분석
    """
    t = str(query or "").strip()

    find_col = helpers["find_col"]
    to_num = helpers["to_num"]
    push_table = helpers["push_table"]
    push_notice = helpers["push_notice"]

    # 검증 현재표에서 처리할 그룹 기준 판정
    compact = t.replace(" ", "")

    group_kind = ""
    if "거래처별" in t:
        group_kind = "vendor"
    elif "제품별" in t or "품목별" in t:
        group_kind = "product"
    elif "월별" in t:
        group_kind = "month"
    elif "일자별" in t or "날짜별" in t:
        group_kind = "date"
    elif "불일치" in compact and "목록" in compact:
        group_kind = "list"

    if not group_kind:
        return False

    col_names = [str(c).strip() for c in df.columns]

    log.info(
        "[chat.followup.validation] action=%r group=%s rows=%s query=%r cols_head=%s",
        source_action,
        group_kind,
        len(df),
        t,
        col_names[:30],
    )

    date_col = find_col(
        df,
        exact=("입고일자", "출고일자", "거래명세서일자", "세금계산서일자", "일자"),
        include_any=("일자",),
        exclude_any=("등록", "수정", "발행"),
    )
    vendor_col = find_col(
        df,
        exact=("거래처명", "매입처명", "매출처명", "입고처명", "출고처명"),
        include_any=("거래처", "매입처", "매출처", "입고처", "출고처"),
        exclude_any=("코드", "번호", "분류", "구분"),
    )
    product_col = find_col(
        df,
        exact=("제품명", "품목명", "상품명"),
        include_any=("제품명", "품목명", "상품명"),
        exclude_any=("코드", "번호", "분류", "구분"),
    )

    qty_col = find_col(
        df,
        exact=("수량", "입고수량", "출고수량"),
        include_any=("수량",),
        exclude_any=("금액", "단가", "코드", "할증"),
    )
    supply_col = find_col(
        df,
        exact=("공급가액",),
        include_any=("공급가액",),
        exclude_any=("확정", "상세합", "차이", "단가"),
    )
    tax_col = find_col(
        df,
        exact=("세액",),
        include_any=("세액",),
        exclude_any=("확정", "상세합", "차이", "단가"),
    )
    fixed_supply_col = find_col(
        df,
        exact=("확정공급가액",),
        include_any=("확정공급가액",),
        exclude_any=("단가", "율"),
    )
    fixed_tax_col = find_col(
        df,
        exact=("확정세액",),
        include_any=("확정세액",),
        exclude_any=("단가", "율"),
    )

    if not fixed_supply_col or not fixed_tax_col:
        return push_notice(
            title="현재표 검증 분석 불가",
            action="현재표 검증 분석 불가",
            message=(
                "현재표는 검증표로 보이지만 확정공급가액/확정세액 컬럼을 찾지 못했습니다.\n\n"
                f"현재표 기준 행수: {len(df):,}건\n"
                f"현재표 주요 컬럼: {', '.join(col_names[:40])}"
            ),
            query_summary="현재표 / 검증 분석 불가",
            source_query=t,
        )

    def _series(col: str | None) -> pd.Series:
        if col and col in df.columns:
            return to_num(df[col])
        return pd.Series([0] * len(df), index=df.index, dtype="float64")

    def _validation_work(group_col: str | None = None, group_label: str | None = None) -> pd.DataFrame:
        data: dict[str, Any] = {
            "_qty": _series(qty_col),
            "_supply": _series(supply_col),
            "_tax": _series(tax_col),
            "_fixed_supply": _series(fixed_supply_col),
            "_fixed_tax": _series(fixed_tax_col),
        }
        if group_col and group_label:
            data[group_label] = df[group_col].astype(str).str.strip()
        return pd.DataFrame(data, index=df.index)

    # 0) 검증표 현재표의 원본 불일치 목록
    #    중요: 검증 화면은 조회 단계에서 이미 only_mismatch_* 조건을 강제한다.
    #    따라서 현재표 불일치 목록은 공급가액/세액 차이를 다시 계산해 걸러내지 않고,
    #    현재 검증표 원본 전체를 그대로 목록으로 반환한다.
    #    일부 검증표는 문서 연결/순번/존재 여부 기준 불일치라서
    #    공급가액-확정공급가액, 세액-확정세액 차이가 0이어도 불일치 행일 수 있다.    
    if group_kind == "list":
        out = df.copy().reset_index(drop=True)

        # 차이 컬럼이 없으면 분석 편의를 위해 보조 컬럼만 추가한다.
        # 단, 이 값으로 행을 필터링하지는 않는다.
        try:
            work = _validation_work()            
            if "공급가액차이" not in out.columns:
                out["공급가액차이"] = (work["_supply"] - work["_fixed_supply"]).reset_index(drop=True)
            if "세액차이" not in out.columns:
                out["세액차이"] = (work["_tax"] - work["_fixed_tax"]).reset_index(drop=True)
        except Exception:
            pass

        log.info(
            "[chat.followup_table] validation mismatch list built source_rows=%s rows=%s table_key=%s mode=original",
            len(df),
            len(out),
            table_key,
        )

        if out.empty:
            return push_notice(
                title="현재표 불일치 목록 결과 없음",
                action="현재표 불일치 목록 결과 없음",
                message="현재 검증표에 표시할 불일치 행이 없습니다.",
                query_summary=f"현재표 / 불일치 목록 결과 없음 / 전체 {len(df):,}건 기준",
                source_query=t,
            )

        if "순번" in out.columns:
            out = out.drop(columns=["순번"])
        out.insert(0, "순번", range(1, len(out) + 1))

        return push_table(
            title="현재표 불일치 목록",
            action="현재표 불일치 목록",
            df=out,
            query_summary=f"현재표 / 불일치 목록 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=min(500, len(out)),
        )

    def _validation_group(group_col: str, group_label: str) -> pd.DataFrame:
        work = _validation_work(group_col, group_label)
        work = work[work[group_label] != ""]
        if work.empty:
            return pd.DataFrame()

        work["_supply_diff"] = work["_supply"] - work["_fixed_supply"]
        work["_tax_diff"] = work["_tax"] - work["_fixed_tax"]

        out = (
            work.groupby(group_label, dropna=False)
            .agg(
                불일치건수=("_qty", "size"),
                수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                확정공급가액=("_fixed_supply", "sum"),
                확정세액=("_fixed_tax", "sum"),
                공급가액차이=("_supply_diff", "sum"),
                세액차이=("_tax_diff", "sum"),
            )
            .reset_index()
        )

        out["공급가액차이절대값"] = out["공급가액차이"].abs()
        out["세액차이절대값"] = out["세액차이"].abs()

        out = out.sort_values(
            ["공급가액차이절대값", "세액차이절대값", "불일치건수"],
            ascending=[False, False, False],
        ).reset_index(drop=True)

        out.insert(0, "순번", range(1, len(out) + 1))
        return out

    if group_kind == "vendor":
        if not vendor_col:
            return push_notice(
                title="현재표 거래처별 불일치 분석 불가",
                action="현재표 거래처별 불일치 분석 불가",
                message="현재표에는 거래처별 분석에 필요한 거래처명 컬럼이 없습니다.",
                query_summary="현재표 / 거래처별 불일치 분석 불가",
                source_query=t,
            )

        out = _validation_group(vendor_col, "거래처명")
        if out.empty:
            return False

        if "TOP" in t or "top" in t or "상위" in t:
            out = out.head(top_n).copy()
            title = f"현재표 거래처별 불일치 TOP {top_n}"
            query_summary = f"현재표 / 거래처별 불일치 TOP {top_n} / 전체 {len(df):,}건 기준"
            display_limit = top_n
        else:
            title = "현재표 거래처별 불일치 분석"
            query_summary = f"현재표 / 거래처별 불일치 분석 / 전체 {len(df):,}건 기준"
            display_limit = min(500, len(out))

        log.info(
            "[chat.followup_table] validation vendor table built source_rows=%s rows=%s table_key=%s",
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

    if group_kind == "product":
        if not product_col:
            return push_notice(
                title="현재표 제품별 불일치 분석 불가",
                action="현재표 제품별 불일치 분석 불가",
                message="현재표에는 제품별 분석에 필요한 제품명/품목명 컬럼이 없습니다.",
                query_summary="현재표 / 제품별 불일치 분석 불가",
                source_query=t,
            )

        out = _validation_group(product_col, "제품명")
        if out.empty:
            return False

        if "TOP" in t or "top" in t or "상위" in t:
            out = out.head(top_n).copy()
            title = f"현재표 제품별 불일치 TOP {top_n}"
            query_summary = f"현재표 / 제품별 불일치 TOP {top_n} / 전체 {len(df):,}건 기준"
            display_limit = top_n
        else:
            title = "현재표 제품별 불일치 분석"
            query_summary = f"현재표 / 제품별 불일치 분석 / 전체 {len(df):,}건 기준"
            display_limit = min(500, len(out))

        log.info(
            "[chat.followup_table] validation product table built source_rows=%s rows=%s table_key=%s",
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

    if group_kind == "month":
        if not date_col:
            return push_notice(
                title="현재표 월별 불일치 분석 불가",
                action="현재표 월별 불일치 분석 불가",
                message="현재표에는 월별 분석에 필요한 일자 컬럼이 없습니다.",
                query_summary="현재표 / 월별 불일치 분석 불가",
                source_query=t,
            )

        work = _validation_work()
        raw_month = df[date_col].astype(str).str.replace(r"\D", "", regex=True).str[:6]
        work["월"] = raw_month.str[:4] + "-" + raw_month.str[4:6]
        work = work[work["월"].str.len() == 7]
        if work.empty:
            return False

        work["_supply_diff"] = work["_supply"] - work["_fixed_supply"]
        work["_tax_diff"] = work["_tax"] - work["_fixed_tax"]

        out = (
            work.groupby("월", dropna=False)
            .agg(
                불일치건수=("_qty", "size"),
                수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                확정공급가액=("_fixed_supply", "sum"),
                확정세액=("_fixed_tax", "sum"),
                공급가액차이=("_supply_diff", "sum"),
                세액차이=("_tax_diff", "sum"),
            )
            .reset_index()
            .sort_values("월", ascending=True)
        )

        out.insert(0, "순번", range(1, len(out) + 1))

        log.info(
            "[chat.followup_table] validation monthly table built source_rows=%s rows=%s table_key=%s",
            len(df),
            len(out),
            table_key,
        )

        return push_table(
            title="현재표 월별 불일치 분석",
            action="현재표 월별 불일치 분석",
            df=out,
            query_summary=f"현재표 / 월별 불일치 분석 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=200,
        )

    if group_kind == "date":
        if not date_col:
            return push_notice(
                title="현재표 일자별 불일치 분석 불가",
                action="현재표 일자별 불일치 분석 불가",
                message="현재표에는 일자별 분석에 필요한 일자 컬럼이 없습니다.",
                query_summary="현재표 / 일자별 불일치 분석 불가",
                source_query=t,
            )

        work = _validation_work()
        raw_day = df[date_col].astype(str).str.replace(r"\D", "", regex=True).str[:8]
        work["일자"] = raw_day.str[:4] + "-" + raw_day.str[4:6] + "-" + raw_day.str[6:8]
        work = work[work["일자"].str.len() == 10]
        if work.empty:
            return False

        work["_supply_diff"] = work["_supply"] - work["_fixed_supply"]
        work["_tax_diff"] = work["_tax"] - work["_fixed_tax"]

        out = (
            work.groupby("일자", dropna=False)
            .agg(
                불일치건수=("_qty", "size"),
                수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                확정공급가액=("_fixed_supply", "sum"),
                확정세액=("_fixed_tax", "sum"),
                공급가액차이=("_supply_diff", "sum"),
                세액차이=("_tax_diff", "sum"),
            )
            .reset_index()
            .sort_values("일자", ascending=True)
        )

        out.insert(0, "순번", range(1, len(out) + 1))

        log.info(
            "[chat.followup_table] validation daily table built source_rows=%s rows=%s table_key=%s",
            len(df),
            len(out),
            table_key,
        )

        return push_table(
            title="현재표 일자별 불일치 분석",
            action="현재표 일자별 불일치 분석",
            df=out,
            query_summary=f"현재표 / 일자별 불일치 분석 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=500,
        )

    return False