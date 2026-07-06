# app/ui/current_table_followups/monthly_stock.py
# Create 2026-06-07
# 실재고월집계 조회          → monthly_stock
# 장부재고월집계 조회        → monthly_stock


from __future__ import annotations

from typing import Any, Callable

import re
import pandas as pd


def handle_monthly_stock_followup(
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
    실재고월집계/장부재고월집계 현재표 전용 후속분석.
    월집계 action은 일반 재고/매출 블록으로 내려가지 않도록 여기서 먼저 처리한다.
    """
    t = str(query or "").strip()
    compact = t.replace(" ", "")

    find_col = helpers["find_col"]

    to_num = helpers["to_num"]

    push_table = helpers["push_table"]
    push_notice = helpers["push_notice"]

    col_names = [str(c).strip() for c in df.columns]    

    month_col = find_col(
        df,
        exact=("재고년월", "기준월", "월"),
        include_any=("재고년월", "기준월"),
        exclude_any=("코드", "번호"),
    )
    product_col = find_col(
        df,
        exact=("제품명", "품목명", "상품명"),
        include_any=("제품명", "품목명", "상품명"),
        exclude_any=("코드", "번호", "분류", "구분"),
    )
    vendor_col = find_col(
        df,
        exact=("거래처명", "매입처명", "매출처명", "입고처명"),
        include_any=("거래처", "매입처", "매출처", "입고처"),
        exclude_any=("코드", "번호", "분류", "구분"),
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
    in_supply_col = find_col(
        df,
        exact=("입고공급가액",),
        include_any=("입고공급가액",),
        exclude_any=("단가", "율"),
    )
    in_tax_col = find_col(
        df,
        exact=("입고세액",),
        include_any=("입고세액",),
        exclude_any=("단가", "율"),
    )
    out_supply_col = find_col(
        df,
        exact=("출고공급가액",),
        include_any=("출고공급가액",),
        exclude_any=("단가", "율"),
    )
    out_tax_col = find_col(
        df,
        exact=("출고세액",),
        include_any=("출고세액",),
        exclude_any=("단가", "율"),
    )

    stock_qty_col = find_col(
        df,
        exact=("재고수량", "현재재고수량", "최종재고수량", "장부재고수량", "실재고수량"),
        include_any=("재고수량",),
        exclude_any=("입고", "출고", "이월", "금액", "단가", "코드"),
    )
    stock_amount_col = find_col(
        df,
        exact=("재고금액", "재고평가금액", "장부재고금액", "실재고금액"),
        include_any=("재고금액", "재고평가금액"),
        exclude_any=("입고", "출고", "단가", "코드"),
    )


    def _series(col: str | None) -> pd.Series:
        if col and col in df.columns:
            return to_num(df[col])
        return pd.Series([0] * len(df), index=df.index, dtype="float64")

    def _monthly_work(extra_col: str | None = None, extra_label: str | None = None) -> pd.DataFrame:
        data: dict[str, Any] = {
            "_in": _series(in_col),
            "_out": _series(out_col),
            "_in_supply": _series(in_supply_col),
            "_in_tax": _series(in_tax_col),
            "_out_supply": _series(out_supply_col),
            "_out_tax": _series(out_tax_col),
        }
        if extra_col and extra_label:
            data[extra_label] = df[extra_col].astype(str).str.strip()
        return pd.DataFrame(data, index=df.index)

    # 0) 월집계표에는 현재 재고수량/재고금액 컬럼이 없을 수 있다.
    #    없는 컬럼으로 질문하면 미지원이 아니라 명확한 notice를 반환한다.
    wants_stock_qty_query = "재고수량" in compact
    wants_stock_amount_query = "재고금액" in compact

    if wants_stock_qty_query and not stock_qty_col:
        msg = (
            "현재표는 실재고/장부재고 월집계표이며, 현재 재고수량 컬럼이 없습니다.\n\n"
            f"현재표 기준 행수: {len(df):,}건\n"
            f"현재표 주요 컬럼: {', '.join(col_names[:40])}\n\n"
            "이 현재표에서는 입고수량, 출고수량, 입고공급가액, 출고공급가액 기준 분석은 가능합니다.\n"
            "현재 재고수량 기준 분석은 [제품재고현황 조회] 또는 [제품수불현황 조회] 후 실행해 주세요."
        )

        return push_notice(
            title="현재표 재고수량 분석 불가",
            action="현재표 재고수량 분석 불가",
            message=msg,
            query_summary=f"현재표 / 재고수량 분석 불가 / 전체 {len(df):,}건 기준",
            source_query=t,
        )

    if wants_stock_amount_query and not stock_amount_col:
        msg = (
            "현재표는 실재고/장부재고 월집계표이며, 현재 재고금액 컬럼이 없습니다.\n\n"
            f"현재표 기준 행수: {len(df):,}건\n"
            f"현재표 주요 컬럼: {', '.join(col_names[:40])}\n\n"
            "이 현재표에서는 입고공급가액, 출고공급가액 기준 분석은 가능합니다.\n"
            "재고금액 기준 분석은 재고금액 컬럼이 포함된 제품재고현황/재고평가표 계열 조회 후 실행해야 합니다."
        )

        return push_notice(
            title="현재표 재고금액 분석 불가",
            action="현재표 재고금액 분석 불가",
            message=msg,
            query_summary=f"현재표 / 재고금액 분석 불가 / 전체 {len(df):,}건 기준",
            source_query=t,
        )


    # 1) 월집계에서는 현재 재고수량 TOP을 만들 수 없다.
    if any(w in t for w in ("제품별", "품목별", "거래처별")) and "재고수량" in t:
        group_word = "거래처별" if "거래처별" in t else "제품별"

        log.info(
            "[chat.followup_table] monthly stock quantity top blocked group=%s source_rows=%s table_key=%s query=%r",
            group_word,
            len(df),
            table_key,
            t,
        )

        msg = (
            "현재표는 실재고/장부재고 월집계표이며, 현재 재고수량 컬럼이 없습니다.\n\n"
            f"현재표 기준 행수: {len(df):,}건\n\n"
            "월집계 현재표에서는 제품별 입고수량 TOP, 제품별 출고수량 TOP, "
            "제품별 출고공급가액 TOP, 거래처별 출고공급가액 TOP, "
            "거래처별 입고/출고수량 분석, 월별 입고/출고수량 요약을 조회할 수 있습니다.\n"
            "제품별 현재 재고수량 TOP은 [제품재고현황 조회] 후 실행해 주세요."
        )

        return push_notice(
            title=f"현재표 {group_word} 재고수량 TOP 불가",
            action=f"현재표 {group_word} 재고수량 TOP 불가",
            message=msg,
            query_summary=f"현재표 / {group_word} 재고수량 TOP 불가",
            source_query=t,
        )

    # 1-1) 월집계 제품별/거래처별 입고수량/출고수량 TOP
    monthly_qty_top_hit = (
        any(w in compact for w in ("제품별", "품목별", "거래처별"))
        and any(w in compact for w in ("입고수량", "출고수량"))
        and "재고수량" not in compact
    )

    if monthly_qty_top_hit:
        if "거래처별" in compact:
            group_col = vendor_col
            group_label = "거래처명"
            title_group = "거래처별"
        else:
            group_col = product_col
            group_label = "제품명"
            title_group = "제품별"

        if "입고수량" in compact:
            metric_name = "입고수량"
            required_col = in_col
        else:
            metric_name = "출고수량"
            required_col = out_col

        if not group_col:
            return push_notice(
                title=f"현재표 {title_group} {metric_name} TOP 불가",
                action=f"현재표 {title_group} {metric_name} TOP 불가",
                message=(
                    f"현재표는 실재고/장부재고 월집계표이지만 {title_group} 분석에 필요한 컬럼을 찾지 못했습니다.\n\n"
                    f"현재표 기준 행수: {len(df):,}건\n"
                    f"현재표 주요 컬럼: {', '.join(col_names[:40])}"
                ),
                query_summary=f"현재표 / {title_group} {metric_name} TOP 불가",
                source_query=t,
            )

        if not required_col:
            return push_notice(
                title=f"현재표 {title_group} {metric_name} TOP 불가",
                action=f"현재표 {title_group} {metric_name} TOP 불가",
                message=f"현재표에서 {metric_name} 컬럼을 찾지 못했습니다.",
                query_summary=f"현재표 / {title_group} {metric_name} TOP 불가",
                source_query=t,
            )

        work = _monthly_work(group_col, group_label)
        work = work[work[group_label] != ""].copy()

        if work.empty:
            return push_notice(
                title=f"현재표 {title_group} {metric_name} TOP 결과 없음",
                action=f"현재표 {title_group} {metric_name} TOP 결과 없음",
                message=f"현재표에서 {title_group} {metric_name} TOP을 만들 유효 자료가 없습니다.",
                query_summary=f"현재표 / {title_group} {metric_name} TOP 결과 없음 / 전체 {len(df):,}건 기준",
                source_query=t,
            )

        out = (
            work.groupby(group_label, dropna=False)
            .agg(
                건수=("_in", "size"),
                입고수량=("_in", "sum"),
                출고수량=("_out", "sum"),
                입고공급가액=("_in_supply", "sum"),
                입고세액=("_in_tax", "sum"),
                출고공급가액=("_out_supply", "sum"),
                출고세액=("_out_tax", "sum"),
            )
            .reset_index()
            .sort_values(metric_name, ascending=False)
        )

        out.insert(0, "순번", range(1, len(out) + 1))
        out2 = out.head(top_n).copy()

        log.info(
            "[chat.followup_table] monthly stock qty top built group=%s metric=%s source_rows=%s rows=%s table_key=%s query=%r",
            title_group,
            metric_name,
            len(df),
            len(out2),
            table_key,
            t,
        )

        return push_table(
            title=f"현재표 {title_group} {metric_name} TOP {top_n}",
            action=f"현재표 {title_group} {metric_name} TOP {top_n}",
            df=out2,
            query_summary=f"현재표 / {title_group} {metric_name} TOP {top_n} / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=top_n,
        )

    # 1-2) 월집계 입고수량/출고수량 조건 목록: 0 이상, -1 이하, 10 초과, 5 미만
    qty_cond_m = re.search(
        r"(입고수량|출고수량)(-?\d+(?:\.\d+)?)(이상|이하|초과|미만|같음|동일|=)",
        compact,
    )

    if qty_cond_m and "목록" in t:
        metric_name = qty_cond_m.group(1)
        threshold = float(qty_cond_m.group(2))
        op = qty_cond_m.group(3)
        metric_col = in_col if metric_name == "입고수량" else out_col

        if not metric_col or metric_col not in df.columns:
            return push_notice(
                title=f"현재표 {metric_name} 조건 목록 불가",
                action=f"현재표 {metric_name} 조건 목록 불가",
                message=f"현재표에서 {metric_name} 컬럼을 찾지 못했습니다.",
                query_summary=f"현재표 / {metric_name} 조건 목록 불가",
                source_query=t,
            )

        s = to_num(df[metric_col])
        if op == "이상":
            mask = s >= threshold
        elif op == "이하":
            mask = s <= threshold
        elif op == "초과":
            mask = s > threshold
        elif op == "미만":
            mask = s < threshold
        else:
            mask = s == threshold

        out = df.loc[mask].copy().reset_index(drop=True)

        title = f"현재표 {metric_name} {threshold:g} {op} 목록"
        query_summary = f"현재표 / {metric_name} {threshold:g} {op} 목록 / 전체 {len(df):,}건 기준"

        log.info(
            "[chat.followup_table] monthly stock qty filter built metric=%s condition=%s %s source_rows=%s rows=%s table_key=%s query=%r",
            metric_name,
            threshold,
            op,
            len(df),
            len(out),
            table_key,
            t,
        )

        if out.empty:
            return push_notice(
                title=title,
                action=title,
                message=f"{title} 조회결과가 없습니다.",
                query_summary=query_summary,
                source_query=t,
            )

        if "순번" in out.columns:
            out = out.drop(columns=["순번"])
        out.insert(0, "순번", range(1, len(out) + 1))

        return push_table(
            title=title,
            action=title,
            df=out,
            query_summary=query_summary,
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=None,
         )

    # 2) 월집계에서 매출/금액 TOP은 출고공급가액 기준으로 처리한다.
    monthly_amount_top_hit = (
        any(w in t for w in ("제품별", "품목별", "거래처별"))
        and any(w in t for w in ("매출", "거래금액", "계산서금액", "금액", "공급가액", "출고공급가액"))
    )

    if monthly_amount_top_hit:
        if "거래처별" in t:
            group_col = vendor_col
            group_label = "거래처명"
            title_group = "거래처별"
        else:
            group_col = product_col
            group_label = "제품명"
            title_group = "제품별"

        if not group_col:
            return push_notice(
                title=f"현재표 {title_group} 출고공급가액 TOP 불가",
                action=f"현재표 {title_group} 출고공급가액 TOP 불가",
                message=(
                    f"현재표는 실재고/장부재고 월집계표이지만 {title_group} 분석에 필요한 컬럼을 찾지 못했습니다.\n\n"
                    f"현재표 기준 행수: {len(df):,}건"
                ),
                query_summary=f"현재표 / {title_group} 출고공급가액 TOP 불가",
                source_query=t,
            )

        if not out_supply_col:
            return push_notice(
                title=f"현재표 {title_group} 출고공급가액 TOP 불가",
                action=f"현재표 {title_group} 출고공급가액 TOP 불가",
                message="현재표는 월집계표이지만 출고공급가액 컬럼을 찾지 못했습니다.",
                query_summary=f"현재표 / {title_group} 출고공급가액 TOP 불가",
                source_query=t,
            )

        work = _monthly_work(group_col, group_label)
        work = work[work[group_label] != ""]

        out = (
            work.groupby(group_label, dropna=False)
            .agg(
                건수=("_in", "size"),
                입고수량=("_in", "sum"),
                출고수량=("_out", "sum"),
                입고공급가액=("_in_supply", "sum"),
                입고세액=("_in_tax", "sum"),
                출고공급가액=("_out_supply", "sum"),
                출고세액=("_out_tax", "sum"),
            )
            .reset_index()
            .sort_values("출고공급가액", ascending=False)
        )

        out.insert(0, "순번", range(1, len(out) + 1))
        out2 = out.head(top_n).copy()

        log.info(
            "[chat.followup_table] monthly stock amount top built group=%s source_rows=%s rows=%s table_key=%s query=%r",
            title_group,
            len(df),
            len(out2),
            table_key,
            t,
        )

        return push_table(
            title=f"현재표 {title_group} 출고공급가액 TOP {top_n}",
            action=f"현재표 {title_group} 출고공급가액 TOP {top_n}",
            df=out2,
            query_summary=f"현재표 / {title_group} 출고공급가액 TOP {top_n}",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
        )

    # 3) 월별 입고수량/출고수량/공급가액 요약 + 월별 TOP
    wants_monthly_flow = (
        month_col
        and (
            (
                "월별" in t
                and any(
                    w in compact
                    for w in (
                        "입고수량",
                        "출고수량",
                        "입출고수량",
                        "입고공급가액",
                        "출고공급가액",
                        "입고세액",
                        "출고세액",
                        "공급가액",
                        "세액",
                    )
                )
            )
            or any(
                w in compact
                for w in (
                    "입고수량이가장많은월",
                    "입고수량이제일많은월",
                    "입고수량최고월",
                    "입고수량top",
                    "입고수량TOP",
                    "출고수량이가장많은월",
                    "출고수량이제일많은월",
                    "출고수량최고월",
                    "출고수량top",
                    "출고수량TOP",
                    "입고공급가액이가장많은월",
                    "입고공급가액이제일많은월",
                    "입고공급가액최고월",
                    "입고공급가액top",
                    "입고공급가액TOP",
                    "출고공급가액이가장많은월",
                    "출고공급가액이제일많은월",
                    "출고공급가액최고월",
                    "출고공급가액top",
                    "출고공급가액TOP",
                    "입고금액이가장많은월",
                    "입고금액이제일많은월",
                    "출고금액이가장많은월",
                    "출고금액이제일많은월",
                )
            )
        )
        and not any(w in compact for w in ("제품별", "품목별", "거래처별"))
    )

    if wants_monthly_flow:
        work = _monthly_work()
        raw_month = df[month_col].astype(str).str.replace(r"\D", "", regex=True).str[:6]
        work["월"] = raw_month.str[:4] + "-" + raw_month.str[4:6]
        work = work[work["월"].str.len() == 7].copy()

        if work.empty:
            return push_notice(
                title="현재표 월별 입출고 요약 결과 없음",
                action="현재표 월별 입출고 요약 결과 없음",
                message="현재표에서 월별 집계가 가능한 재고년월/기준월 자료를 찾지 못했습니다.",
                query_summary=f"현재표 / 월별 입출고 요약 결과 없음 / 전체 {len(df):,}건 기준",
                source_query=t,
            )

        out = (
            work.groupby("월", dropna=False)
            .agg(
                건수=("_in", "size"),
                입고수량=("_in", "sum"),
                출고수량=("_out", "sum"),
                입고공급가액=("_in_supply", "sum"),
                입고세액=("_in_tax", "sum"),
                출고공급가액=("_out_supply", "sum"),
                출고세액=("_out_tax", "sum"),
            )
            .reset_index()
        )

        has_in_qty = "입고수량" in compact
        has_out_qty = "출고수량" in compact
        has_in_amt = "입고공급가액" in compact or "입고금액" in compact
        has_out_amt = "출고공급가액" in compact or "출고금액" in compact
        has_supply = "공급가액" in compact and not has_in_amt and not has_out_amt

        top_match = re.search(r"(?:TOP|top|상위)\s*(\d{1,4})", t)
        explicit_top_n = int(top_match.group(1)) if top_match else None

        wants_rank = (
            explicit_top_n is not None
            or "가장많은월" in compact
            or "제일많은월" in compact
            or "최고월" in compact
        )

        metric_col = None
        if has_in_qty:
            metric_col = "입고수량"
        elif has_out_qty:
            metric_col = "출고수량"
        elif has_in_amt:
            metric_col = "입고공급가액"
        elif has_out_amt:
            metric_col = "출고공급가액"

        if wants_rank and metric_col:
            rank_n = explicit_top_n if explicit_top_n is not None else 1
            rank_n = max(1, min(int(rank_n), 1000))

            out = out.sort_values(metric_col, ascending=False).head(rank_n).reset_index(drop=True)
            out.insert(0, "순번", range(1, len(out) + 1))

            if rank_n == 1:
                title = f"현재표 {metric_col} 1위 월"
                query_summary = f"현재표 / {metric_col}이 가장 많은 월 / 전체 {len(df):,}건 기준"
            else:
                title = f"현재표 {metric_col} TOP {rank_n} 월"
                query_summary = f"현재표 / {metric_col} TOP {rank_n} 월 / 전체 {len(df):,}건 기준"

            display_limit = rank_n

        else:
            out = out.sort_values("월", ascending=True).reset_index(drop=True)
            out.insert(0, "순번", range(1, len(out) + 1))

            if has_in_qty and not has_out_qty:
                title = "현재표 월별 입고수량 요약"
                query_summary = f"현재표 / 월별 입고수량 요약 / 전체 {len(df):,}건 기준"
            elif has_out_qty and not has_in_qty:
                title = "현재표 월별 출고수량 요약"
                query_summary = f"현재표 / 월별 출고수량 요약 / 전체 {len(df):,}건 기준"
            elif has_in_amt and not has_out_amt:
                title = "현재표 월별 입고공급가액 요약"
                query_summary = f"현재표 / 월별 입고공급가액 요약 / 전체 {len(df):,}건 기준"
            elif has_out_amt and not has_in_amt:
                title = "현재표 월별 출고공급가액 요약"
                query_summary = f"현재표 / 월별 출고공급가액 요약 / 전체 {len(df):,}건 기준"
            elif has_supply:
                title = "현재표 월별 입고/출고공급가액 요약"
                query_summary = f"현재표 / 월별 입고공급가액 출고공급가액 요약 / 전체 {len(df):,}건 기준"
            else:
                title = "현재표 월별 입고/출고수량 요약"
                query_summary = f"현재표 / 월별 입고수량 출고수량 요약 / 전체 {len(df):,}건 기준"

            display_limit = None

        log.info(
            "[chat.followup_table] monthly stock monthly flow built source_rows=%s rows=%s table_key=%s query=%r",
            len(df),
            len(out),
            table_key,
            t,
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
    return False
        