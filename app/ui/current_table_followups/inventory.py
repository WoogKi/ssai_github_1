# app/ui/current_table_followups/inventory.py
# 제품재고현황 조회 현재표 전용 후속분석
# Create 2026-06-08
# 제품재고현황 조회          → inventory

from __future__ import annotations

from typing import Any, Callable

import pandas as pd


def handle_inventory_followup(
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
    제품재고현황 조회 현재표 전용 후속분석.

    현재표 action이 제품재고현황이면:
    - 제조사별/발주처별/제품그룹별/제품분류별/제품구분별 재고수량 분석
    - 재고수량 0 이하/0 이상 제품 목록
    - 재고수량이 가장 많은 제품 / 제품별 재고수량 TOP N
    을 제품재고현황 기준으로 처리한다.
    """
    t = str(query or "").strip()
    compact = t.replace(" ", "")

    find_col = helpers["find_col"]
    to_num = helpers["to_num"]
    push_table = helpers["push_table"]
    push_notice = helpers["push_notice"]

    col_names = [str(c).strip() for c in df.columns]
    current_stock_subtotal_mask = pd.Series(False, index=df.index)
    if {
        "순번", "재고위치코드", "재고위치명", "재고수량",
    }.issubset(set(col_names)):
        # 현재고 조회는 위치별 상세와 제품 합계가 함께 제공된다. 제품 TOP은
        # 합계행만 사용해 상세행을 다시 더하지 않는다.
        current_stock_subtotal_mask = (
            df["재고위치명"].fillna("").astype(str).str.strip().eq("제품 합계")
        )

    product_col = find_col(
        df,
        exact=("제품명", "품목명", "상품명"),
        include_any=("제품명", "품목명", "상품명"),
        exclude_any=("코드", "번호", "분류", "구분"),
    )
    product_code_col = find_col(
        df,
        exact=("제품코드", "품목코드", "상품코드"),
        include_any=("제품코드", "품목코드", "상품코드"),
        exclude_any=("그룹", "분류", "구분"),
    )
    spec_col = find_col(
        df,
        exact=("규격", "제품규격"),
        include_any=("규격",),
        exclude_any=("코드", "번호"),
    )

    stock_col = find_col(
        df,
        exact=("재고수량", "현재재고수량"),
        include_any=("재고수량",),
        exclude_any=("이월", "입고", "출고", "금액", "단가", "코드"),
    )
    prev_col = find_col(
        df,
        exact=("이월수량",),
        include_any=("이월수량",),
        exclude_any=("금액", "단가", "코드"),
    )
    in_col = find_col(
        df,
        exact=("입고수량",),
        include_any=("입고수량",),
        exclude_any=("금액", "단가", "코드"),
    )
    out_col = find_col(
        df,
        exact=("출고수량",),
        include_any=("출고수량",),
        exclude_any=("금액", "단가", "코드"),
    )
    stock_amt_col = find_col(
        df,
        exact=("재고금액",),
        include_any=("재고금액",),
        exclude_any=("단가", "율"),
    )
    ins_amt_col = find_col(
        df,
        exact=("보험금액",),
        include_any=("보험금액",),
        exclude_any=("단가", "율"),
    )

    if not stock_col:
        return push_notice(
            title="현재표 재고수량 분석 불가",
            action="현재표 재고수량 분석 불가",
            message=(
                "현재표는 제품재고현황으로 보이지만 재고수량 컬럼을 찾지 못했습니다.\n\n"
                f"현재표 기준 행수: {len(df):,}건\n"
                f"현재표 주요 컬럼: {', '.join(col_names[:40])}"
            ),
            query_summary="현재표 / 재고수량 분석 불가",
            source_query=t,
        )

    def _series(col: str | None) -> pd.Series:
        if col and col in df.columns:
            return to_num(df[col])
        return pd.Series([0] * len(df), index=df.index, dtype="float64")
    
    def _valid_product_name_mask_from_series(s: pd.Series) -> pd.Series:
        """
        제품재고현황 결과에는 제품명/제품코드가 비어 있는 전체 합계 라인이 포함될 수 있다.
        제품별 TOP/목록에서는 이 합계 라인을 제외해야 한다.
        """
        name = s.fillna("").astype(str).str.strip()

        bad = (
            name.eq("")
            | name.str.contains(r"^(합계|총계|소계|합계금액|전체|TOTAL)$", case=False, regex=True, na=False)
            | name.str.contains(r"합계\s*금액", case=False, regex=True, na=False)
        )

        return ~bad

    def _filter_real_product_rows(work: pd.DataFrame, *, name_col: str = "제품명") -> pd.DataFrame:
        if name_col not in work.columns:
            return work

        mask = _valid_product_name_mask_from_series(work[name_col])
        return work.loc[mask].copy()


    def _inventory_work(extra_col: str | None = None, extra_label: str | None = None) -> pd.DataFrame:
        data: dict[str, Any] = {
            "_prev": _series(prev_col),
            "_in": _series(in_col),
            "_out": _series(out_col),
            "_stock": _series(stock_col),
            "_stock_amt": _series(stock_amt_col),
            "_ins_amt": _series(ins_amt_col),
        }
        if extra_col and extra_label:
            data[extra_label] = df[extra_col].astype(str).str.strip()
        return pd.DataFrame(data, index=df.index)

    def _find_group() -> tuple[str | None, str]:
        group_specs = [
            ("제조사별", "제조사명", ("제조사명", "제조사"), ("제조사",), ("코드", "번호")),
            ("발주처별", "발주처명", ("발주처명", "발주처"), ("발주처",), ("코드", "번호")),
            ("매입처별", "매입처명", ("매입처명", "매입처"), ("매입처",), ("코드", "번호")),
            ("재고위치별", "재고위치", ("재고위치", "재고위치명"), ("재고위치",), ("코드", "번호", "대분류")),            
            ("제품그룹별", "제품그룹명", ("제품그룹명", "제품그룹"), ("제품그룹",), ("코드", "번호")),
            ("제품구분별", "제품구분명", ("제품구분명", "제품구분"), ("제품구분",), ("코드", "번호")),
            ("제품분류별", "제품분류명", ("제품분류명", "제품분류"), ("제품분류",), ("코드", "번호")),
            ("특수관리별", "특수관리제품명", ("특수관리제품명", "특수관리제품"), ("특수관리",), ("코드", "번호")),
        ]

        for keyword, label, exact, include_any, exclude_any in group_specs:
            if keyword in t:
                col = find_col(
                    df,
                    exact=exact,
                    include_any=include_any,
                    exclude_any=exclude_any,
                )
                return col, label

        return None, ""

    # 1) 재고수량 0 이하 / 0 이상 제품 목록
    if "재고수량" in t and any(w in t for w in ("0이하", "0 이하", "0미만", "0 미만", "0이상", "0 이상")):
        work = df.copy()
        stock_s = _series(stock_col)

        if "0미만" in t or "0 미만" in t:
            mask = stock_s < 0
            title_suffix = "0 미만"
        elif "0이하" in t or "0 이하" in t:
            mask = stock_s <= 0
            title_suffix = "0 이하"
        else:
            mask = stock_s >= 0
            title_suffix = "0 이상"

        if product_col and product_col in df.columns:
            product_mask = _valid_product_name_mask_from_series(df[product_col])
            mask = mask & product_mask

        out = work.loc[mask].copy()

        preferred_cols = [
            product_code_col,
            product_col,
            spec_col,
            stock_col,
            prev_col,
            in_col,
            out_col,
            stock_amt_col,
            ins_amt_col,
            "제조사명",
            "발주처명",
            "제품그룹명",
            "제품구분명",
            "제품분류명",
        ]
        keep_cols = []
        for c in preferred_cols:
            if c and c in out.columns and c not in keep_cols:
                keep_cols.append(c)

        if keep_cols:
            out = out[keep_cols].copy()

        log.info(
            "[chat.followup_table] inventory stock filter built condition=%s source_rows=%s rows=%s table_key=%s",
            title_suffix,
            len(df),
            len(out),
            table_key,
        )

        return push_table(
            title=f"현재표 재고수량 {title_suffix} 목록",
            action=f"현재표 재고수량 {title_suffix} 목록",
            df=out,
            query_summary=f"현재표 / 재고수량 {title_suffix}",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
        )

    # 2) 제조사별/발주처별/제품분류별 등 그룹 재고수량 분석
    group_col, group_label = _find_group()
    if group_col:
        work = _inventory_work(group_col, group_label)
        work = work[work[group_label] != ""]
        metric_col = "재고금액" if "재고금액" in compact else "재고수량"

        out = (
            work.groupby(group_label, dropna=False)
            .agg(
                건수=("_stock", "size"),
                이월수량=("_prev", "sum"),
                입고수량=("_in", "sum"),
                출고수량=("_out", "sum"),
                재고수량=("_stock", "sum"),
                재고금액=("_stock_amt", "sum"),
                보험금액=("_ins_amt", "sum"),
            )
            .reset_index()
            .sort_values(metric_col, ascending=False)
        )

        out.insert(0, "순번", range(1, len(out) + 1))

        log.info(
            "[chat.followup_table] inventory group table built group=%s group_col=%s source_rows=%s rows=%s table_key=%s",
            group_label,
            group_col,
            len(df),
            len(out),
            table_key,
        )

        group_title = group_label.replace("명", "")

        return push_table(
            title=f"현재표 {group_title}별 {metric_col} 분석",
            action=f"현재표 {group_title}별 {metric_col} 분석",
            df=out,
            query_summary=f"현재표 / {group_title}별 {metric_col} 분석 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
        )

    # 3) 제품별 재고수량/재고금액 TOP / 1위 제품
    wants_product_stock_top = (
        any(w in compact for w in ("재고수량", "재고금액"))
        and (
            "제품별" in t
            or "품목별" in t
            or "가장많은제품" in compact
            or "재고수량이가장많은" in compact
            or "재고금액이가장많은" in compact
            or "TOP" in t
            or "top" in t
            or "상위" in t
        )
    )

    if wants_product_stock_top:
        if not product_col:
            return push_notice(
                title="현재표 제품별 재고 TOP 불가",
                action="현재표 제품별 재고 TOP 불가",
                message="현재표에는 제품별 분석에 필요한 제품명/품목명 컬럼이 없습니다.",
                query_summary="현재표 / 제품별 재고 TOP 불가",
                source_query=t,
            )

        metric_col = "재고금액" if "재고금액" in compact else "재고수량"
        sort_key = "_stock_amt" if metric_col == "재고금액" else "_stock"

        if metric_col == "재고금액" and not stock_amt_col:
            return push_notice(
                title="현재표 제품별 재고금액 TOP 불가",
                action="현재표 제품별 재고금액 TOP 불가",
                message=(
                    "현재표에는 제품별 재고금액 TOP을 만들 재고금액 컬럼이 없습니다.\n\n"
                    f"현재표 기준 행수: {len(df):,}건\n"
                    f"현재표 주요 컬럼: {', '.join(col_names[:40])}"
                ),
                query_summary="현재표 / 제품별 재고금액 TOP 불가",
                source_query=t,
            )

        work = _inventory_work(product_col, "제품명")
        if bool(current_stock_subtotal_mask.any()):
            # 현재고 표는 여러 위치 제품에만 제품 합계행을 넣는다. 합계행이
            # 있는 제품은 그 행 하나만, 단일 위치 제품은 상세행 하나만 쓴다.
            product_key_cols = [column for column in (product_code_col, product_col, spec_col) if column]
            key_frame = df[product_key_cols].fillna("").astype(str) if product_key_cols else pd.DataFrame(index=df.index)
            product_keys = key_frame.agg("\x1f".join, axis=1) if not key_frame.empty else pd.Series(df.index.astype(str), index=df.index)
            subtotal_keys = set(product_keys.loc[current_stock_subtotal_mask])
            fallback_rows = (
                (~current_stock_subtotal_mask)
                & ~product_keys.isin(subtotal_keys)
                & ~product_keys.duplicated(keep="first")
            )
            work = work.loc[current_stock_subtotal_mask | fallback_rows].copy()
        if product_code_col:
            work["제품코드"] = df.loc[work.index, product_code_col].astype(str).str.strip()
        if spec_col:
            work["규격"] = df.loc[work.index, spec_col].astype(str).str.strip()
        maker_col = find_col(
            df,
            exact=("제조사명",),
            include_any=("제조사명", "제조사"),
            exclude_any=("코드", "번호"),
        )
        if maker_col:
            work["제조사명"] = df.loc[work.index, maker_col].astype(str).str.strip()

        work = _filter_real_product_rows(work, name_col="제품명")

        if work.empty:
            return push_notice(
                title=f"현재표 제품별 {metric_col} TOP 불가",
                action=f"현재표 제품별 {metric_col} TOP 불가",
                message=(
                    f"현재표에서 제품별 {metric_col} TOP을 만들 수 있는 실제 제품 행을 찾지 못했습니다.\n\n"
                    f"현재표 기준 행수: {len(df):,}건"
                ),
                query_summary=f"현재표 / 제품별 {metric_col} TOP 불가",
                source_query=t,
            )

        group_cols = ["제품명"]
        if product_code_col:
            group_cols.insert(0, "제품코드")
        if spec_col:
            group_cols.append("규격")
        if "제조사명" in work.columns:
            group_cols.append("제조사명")

        out = (
            work.groupby(group_cols, dropna=False)
            .agg(
                재고수량=("_stock", "sum"),
                보험금액=("_ins_amt", "sum"),
            )
            .reset_index()
            .sort_values(metric_col, ascending=False)
        )

        wants_first = (
            "가장" in t
            and "TOP" not in t
            and "top" not in t
            and "상위" not in t
        )

        if wants_first:
            limit = 1
            title = f"현재표 {metric_col} 1위 제품"
            query_summary = f"현재표 / {metric_col}이 가장 많은 제품 / 전체 {len(df):,}건 기준"
        else:
            limit = top_n
            title = f"현재표 제품별 {metric_col} TOP {top_n}"
            query_summary = f"현재표 / 제품별 {metric_col} TOP {top_n} / 전체 {len(df):,}건 기준"

        out.insert(0, "순번", range(1, len(out) + 1))
        top_columns = ["순번", "제품코드", "제품명", "규격", "제조사명", "재고수량", "보험금액"]
        out2 = out.head(limit).copy()
        for column in top_columns:
            if column not in out2.columns:
                out2[column] = "" if column not in {"재고수량", "보험금액"} else 0
        out2 = out2[top_columns]

        log.info(
            "[chat.followup_table] inventory product stock top built metric=%s source_rows=%s work_rows=%s rows=%s table_key=%s",
            metric_col,
            len(df),
            len(work),
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
            display_limit=limit,
        )
    
