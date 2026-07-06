# app/ui/current_table_followups/stock_ledger.py
# Created by: 2026-06-08
# 제품수불현황 조회          → stock_ledger

from __future__ import annotations

from typing import Any, Callable

import re
import pandas as pd


def handle_stock_ledger_followup(
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
    제품수불현황 조회 현재표 전용 후속분석.

    제품수불현황은 보통 단일 제품의 입고/출고/재고 흐름이다.
    - 입고수량 합계 / 출고수량 합계
    - 월별 입고수량/출고수량 요약
    - 재고수량이 가장 많은 제품? → 단일 제품 수불현황이면 최종 재고수량 안내
    - 거래처별 입고/출고수량 분석
    """
    t = str(query or "").strip()
    compact = t.replace(" ", "")

    find_col = helpers["find_col"]
    to_num = helpers["to_num"]
    push_table = helpers["push_table"]
    push_notice = helpers["push_notice"]

    col_names = [str(c).strip() for c in df.columns]

    date_col = find_col(
        df,
        exact=("입출고일자", "수불일자", "입고일자", "출고일자", "기준일자", "일자"),
        include_any=("일자",),
        exclude_any=("등록", "수정", "발행", "유효"),
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
    vendor_col = find_col(
        df,
        exact=("거래처명", "매입처명", "매출처명", "입고처명", "출고처명"),
        include_any=("거래처", "매입처", "매출처", "입고처", "출고처"),
        exclude_any=("코드", "번호", "분류", "구분"),
    )

    prev_col = find_col(
        df,
        exact=("이월재고", "이월수량", "전월재고"),
        include_any=("이월",),
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
    stock_col = find_col(
        df,
        exact=("재고수량", "현재재고수량", "최종재고수량"),
        include_any=("재고수량",),
        exclude_any=("이월", "입고", "출고", "금액", "단가", "코드"),
    )
    amount_col = find_col(
        df,
        exact=("수불금액", "합계금액", "공급가액", "금액"),
        include_any=("금액", "공급가액"),
        exclude_any=("단가", "율"),
    )

    def _series(col: str | None) -> pd.Series:
        if col and col in df.columns:
            return to_num(df[col])
        return pd.Series([0] * len(df), index=df.index, dtype="float64")

    def _text_series(col: str | None) -> pd.Series:
        if col and col in df.columns:
            return df[col].fillna("").astype(str).str.strip()
        return pd.Series([""] * len(df), index=df.index, dtype="object")

    def _first_non_empty(col: str | None, default: str = "") -> str:
        s = _text_series(col)
        s = s[s != ""]
        return str(s.iloc[0]) if not s.empty else default

    def _valid_product_mask() -> pd.Series:
        if not product_col or product_col not in df.columns:
            return pd.Series([True] * len(df), index=df.index)

        name = df[product_col].fillna("").astype(str).str.strip()
        bad = (
            name.eq("")
            | name.str.contains(r"^(합계|총계|소계|합계금액|전체|TOTAL)$", case=False, regex=True, na=False)
            | name.str.contains(r"합계\s*금액", case=False, regex=True, na=False)
        )
        return ~bad

    def _work(extra_col: str | None = None, extra_label: str | None = None) -> pd.DataFrame:
        data: dict[str, Any] = {
            "_prev": _series(prev_col),
            "_in": _series(in_col),
            "_out": _series(out_col),
            "_stock": _series(stock_col),
            "_amount": _series(amount_col),
        }

        if date_col:
            raw_day = df[date_col].fillna("").astype(str).str.replace(r"\D", "", regex=True)
            data["_date_raw"] = raw_day
            data["_month"] = raw_day.str[:4] + "-" + raw_day.str[4:6]
            data["_sortkey"] = raw_day.str[:8]
        else:
            data["_date_raw"] = pd.Series([""] * len(df), index=df.index)
            data["_month"] = pd.Series([""] * len(df), index=df.index)
            data["_sortkey"] = pd.Series([""] * len(df), index=df.index)

        if extra_col and extra_label:
            data[extra_label] = df[extra_col].fillna("").astype(str).str.strip()

        return pd.DataFrame(data, index=df.index)

    if not in_col and not out_col and not stock_col:
        return push_notice(
            title="현재표 제품수불 분석 불가",
            action="현재표 제품수불 분석 불가",
            message=(
                "현재표는 제품수불현황으로 보이지만 입고수량/출고수량/재고수량 컬럼을 찾지 못했습니다.\n\n"
                f"현재표 기준 행수: {len(df):,}건\n"
                f"현재표 주요 컬럼: {', '.join(col_names[:40])}"
            ),
            query_summary="현재표 / 제품수불 분석 불가",
            source_query=t,
        )
    # 1) 입고수량/출고수량/재고수량 요약
    
    is_monthly_qty_query = (
        "월별" in t
        or any(
            w in compact
            for w in (
                "월별입고수량",
                "월별출고수량",
                "월별재고수량",
                "입고수량이가장많은월",
                "출고수량이가장많은월",
                "재고수량이가장많은월",
                "입고수량최고월",
                "출고수량최고월",
                "재고수량최고월",
            )
        )
    )    
    
    wants_qty_summary = (
        not is_monthly_qty_query
        and (
            any(
                w in compact
                for w in (
                    "입고수량합계",
                    "출고수량합계",
                    "재고수량합계",
                    "입고수량요약",
                    "출고수량요약",
                    "재고수량요약",
                )
            )
            or (
                any(w in compact for w in ("입고수량", "출고수량", "재고수량"))
                and any(w in t for w in ("합계", "알려", "요약"))
            )
        )
    )

    if wants_qty_summary:
        work = _work()
        valid_mask = _valid_product_mask()
        work = work.loc[valid_mask].copy()

        in_sum = float(work["_in"].sum())
        out_sum = float(work["_out"].sum())

        final_stock = None
        stock_sum = None
        if stock_col:
            sorted_work = work.sort_values("_sortkey")
            stock_s = sorted_work["_stock"].dropna()
            if not stock_s.empty:
                final_stock = float(stock_s.iloc[-1])
                stock_sum = float(stock_s.sum())
        else:
            final_stock = float(in_sum - out_sum)

        out = pd.DataFrame(
            [{
                "전체건수": int(len(work)),
                "입고수량합계": in_sum,
                "출고수량합계": out_sum,
                "재고수량합계_참고": stock_sum if stock_sum is not None else "",
                "최종재고수량": final_stock if final_stock is not None else "",
                "제품코드": _first_non_empty(product_code_col, "조회조건의 단일 제품"),
                "제품명": _first_non_empty(product_col, "조회조건의 단일 제품"),
            }]
        )

        has_in_qty = "입고수량" in compact
        has_out_qty = "출고수량" in compact
        has_stock_qty = "재고수량" in compact

        if has_in_qty and not has_out_qty and not has_stock_qty:
            title = "현재표 입고수량 합계"
            query_summary = f"현재표 / 입고수량 합계 / 전체 {len(df):,}건 기준"
        elif has_out_qty and not has_in_qty and not has_stock_qty:
            title = "현재표 출고수량 합계"
            query_summary = f"현재표 / 출고수량 합계 / 전체 {len(df):,}건 기준"
        elif has_stock_qty and not has_in_qty and not has_out_qty:
            title = "현재표 재고수량 요약"
            query_summary = f"현재표 / 재고수량 요약 / 전체 {len(df):,}건 기준"
        else:
            title = "현재표 입고/출고수량 합계"
            query_summary = f"현재표 / 입고수량 합계와 출고수량 합계 / 전체 {len(df):,}건 기준"


        log.info(
            "[chat.followup_table] stock ledger qty summary built source_rows=%s rows=%s table_key=%s",
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
            display_limit=1,
        )
    
    # 2) 월별 입고수량/출고수량/재고수량 요약
    wants_monthly = (
        (
            "월별" in t
            and any(w in t for w in ("입고수량", "출고수량", "재고수량", "수불수량", "수량"))
        )
        or any(
            w in compact
            for w in (
                "월별입고수량",
                "월별출고수량",
                "월별재고수량",
                "입고수량이가장많은월",
                "출고수량이가장많은월",
                "재고수량이가장많은월",
                "입고수량최고월",
                "출고수량최고월",
                "재고수량최고월",
            )
        )
    )

    if wants_monthly:
        if not date_col:
            return push_notice(
                title="현재표 월별 수량 요약 불가",
                action="현재표 월별 수량 요약 불가",
                message="현재표에는 월별 분석에 필요한 일자 컬럼이 없습니다.",
                query_summary="현재표 / 월별 수량 요약 불가",
                source_query=t,
            )

        work = _work()
        work = work.loc[_valid_product_mask()].copy()
        work = work[work["_month"].str.len() == 7]
        work = work.sort_values(["_month", "_sortkey"])

        if work.empty:
            return push_notice(
                title="현재표 월별 수량 요약 결과 없음",
                action="현재표 월별 수량 요약 결과 없음",
                message="현재표에서 월별 집계가 가능한 유효 일자 자료를 찾지 못했습니다.",
                query_summary=f"현재표 / 월별 수량 요약 결과 없음 / 전체 {len(df):,}건 기준",
                source_query=t,
            )

        out = (
            work.groupby("_month", dropna=False)
            .agg(
                건수=("_in", "size"),
                입고수량=("_in", "sum"),
                출고수량=("_out", "sum"),
                재고수량=("_stock", "last"),
                수불금액=("_amount", "sum"),
            )
            .reset_index()
            .rename(columns={"_month": "월"})
        )

        metric_col = None
        if any(w in compact for w in ("입고수량이가장많은월", "입고수량최고월")):
            metric_col = "입고수량"
        elif any(w in compact for w in ("출고수량이가장많은월", "출고수량최고월")):
            metric_col = "출고수량"
        elif any(w in compact for w in ("재고수량이가장많은월", "재고수량최고월")):
            metric_col = "재고수량"

        if metric_col:
            out = out.sort_values(metric_col, ascending=False).head(1).reset_index(drop=True)
            title = f"현재표 {metric_col} 1위 월"
            query_summary = f"현재표 / {metric_col}이 가장 많은 월 / 전체 {len(df):,}건 기준"
            display_limit = 1
        else:
            out = out.sort_values("월", ascending=True).reset_index(drop=True)

            has_in_qty = "입고수량" in compact
            has_out_qty = "출고수량" in compact
            has_stock_qty = "재고수량" in compact

            if has_in_qty and not has_out_qty and not has_stock_qty:
                title = "현재표 월별 입고수량 요약"
                query_summary = f"현재표 / 월별 입고수량 요약 / 전체 {len(df):,}건 기준"
            elif has_out_qty and not has_in_qty and not has_stock_qty:
                title = "현재표 월별 출고수량 요약"
                query_summary = f"현재표 / 월별 출고수량 요약 / 전체 {len(df):,}건 기준"
            elif has_stock_qty and not has_in_qty and not has_out_qty:
                title = "현재표 월별 재고수량 요약"
                query_summary = f"현재표 / 월별 재고수량 요약 / 월 마지막 재고수량 기준 / 전체 {len(df):,}건 기준"                
            else:
                title = "현재표 월별 입고/출고수량 요약"
                query_summary = f"현재표 / 월별 입고수량 출고수량 요약 / 전체 {len(df):,}건 기준"

            display_limit = None

        out.insert(0, "순번", range(1, len(out) + 1))

        log.info(
            "[chat.followup_table] stock ledger monthly table built source_rows=%s rows=%s table_key=%s",
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

    # 2-1) 재고수량 0 이하/이상 목록
    wants_stock_zero_list = (
        "재고수량" in t
        and any(
            w in compact
            for w in (
                "0이하",
                "0미만",
                "0이상",
                "0초과",
                "마이너스",
                "음수",
                "양수",
                "<=0",
                "<0",
                ">=0",
                ">0",
            )
        )
    )

    if wants_stock_zero_list:
        if not stock_col:
            return push_notice(
                title="현재표 재고수량 목록 불가",
                action="현재표 재고수량 목록 불가",
                message="현재표에는 재고수량 0 이하/이상 목록을 만들 재고수량 컬럼이 없습니다.",
                query_summary="현재표 / 재고수량 0 이하/이상 목록 불가",
                source_query=t,
            )

        stock_s = _series(stock_col)
        valid_mask = _valid_product_mask()

        is_under = any(w in compact for w in ("0이하", "0미만", "마이너스", "음수", "<=0", "<0"))
        is_strict_under = any(w in compact for w in ("0미만", "<0"))
        is_over = any(w in compact for w in ("0이상", "0초과", "양수", ">=0", ">0"))
        is_strict_over = any(w in compact for w in ("0초과", ">0"))

        if is_under:
            if is_strict_under:
                mask = valid_mask & stock_s.lt(0)
                title = "현재표 재고수량 0 미만 목록"
                query_summary = f"현재표 / 재고수량 0 미만 목록 / 전체 {len(df):,}건 기준"
            else:
                mask = valid_mask & stock_s.le(0)
                title = "현재표 재고수량 0 이하 목록"
                query_summary = f"현재표 / 재고수량 0 이하 목록 / 전체 {len(df):,}건 기준"
            sort_ascending = True
        elif is_over:
            if is_strict_over:
                mask = valid_mask & stock_s.gt(0)
                title = "현재표 재고수량 0 초과 목록"
                query_summary = f"현재표 / 재고수량 0 초과 목록 / 전체 {len(df):,}건 기준"
            else:
                mask = valid_mask & stock_s.ge(0)
                title = "현재표 재고수량 0 이상 목록"
                query_summary = f"현재표 / 재고수량 0 이상 목록 / 전체 {len(df):,}건 기준"
            sort_ascending = False
        else:
            return False

        out = df.loc[mask].copy()

        if out.empty:
            return push_notice(
                title=title,
                action=title,
                message=f"{title} 조회결과가 없습니다.",
                query_summary=query_summary,
                source_query=t,
            )

        out["_재고수량정렬"] = stock_s.loc[out.index]
        out = out.sort_values("_재고수량정렬", ascending=sort_ascending).drop(columns=["_재고수량정렬"])

        if "순번" in out.columns:
            out = out.drop(columns=["순번"])

        out.insert(0, "순번", range(1, len(out) + 1))

        log.info(
            "[chat.followup_table] stock ledger stock zero list built source_rows=%s rows=%s stock_col=%s table_key=%s",
            len(df),
            len(out),
            stock_col,
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
            display_limit=None,
        )


    # 3) 재고수량이 가장 많은 제품은?
    wants_stock_max_product = (
        "재고수량" in t
        and (
            "가장많은제품" in compact
            or "가장많은품목" in compact
            or "재고수량최대" in compact
            or "가장 많은 제품" in t
            or "가장 많은 품목" in t
        )
    )

    if wants_stock_max_product:
        valid_mask = _valid_product_mask()
        product_s = _text_series(product_col).loc[valid_mask] if product_col else pd.Series([], dtype="object")
        unique_products = product_s[product_s != ""].nunique()

        work = _work()
        work = work.loc[valid_mask].copy()
        work = work.sort_values("_sortkey")

        if unique_products <= 1:
            stock_s = work["_stock"].dropna()
            final_stock = float(stock_s.iloc[-1]) if not stock_s.empty else float(work["_in"].sum() - work["_out"].sum())

            product_code = _first_non_empty(product_code_col, "조회조건의 단일 제품")
            product_name = _first_non_empty(product_col, "조회조건의 단일 제품")

            msg = (
                f"전체 현재표 {len(df):,}건 기준입니다.\n\n"
                "현재표는 단일 제품 수불현황입니다. 제품 간 재고수량 순위 비교 대상은 없습니다.\n\n"
                f"제품코드: {product_code}\n"
                f"제품명: {product_name}\n"
                f"최종 재고수량: {final_stock:,.0f}개\n"
                f"재고수량 기준 컬럼: {stock_col or '입고수량-출고수량'}"
            )

            log.info(
                "[chat.followup_table] stock ledger single product final stock notice source_rows=%s final_stock=%s table_key=%s",
                len(df),
                final_stock,
                table_key,
            )

            return push_notice(
                title="현재표 단일 제품 최종 재고수량",
                action="현재표 단일 제품 최종 재고수량",
                message=msg,
                query_summary=f"현재표 / 단일 제품 최종 재고수량 / 전체 {len(df):,}건 기준",
                source_query=t,
            )

        # 여러 제품이 섞인 경우만 제품별 TOP 표 생성
        if not product_col:
            return push_notice(
                title="현재표 제품별 재고수량 TOP 불가",
                action="현재표 제품별 재고수량 TOP 불가",
                message="현재표에는 제품별 분석에 필요한 제품명/품목명 컬럼이 없습니다.",
                query_summary="현재표 / 제품별 재고수량 TOP 불가",
                source_query=t,
            )

        work["제품명"] = df.loc[work.index, product_col].fillna("").astype(str).str.strip()
        if product_code_col:
            work["제품코드"] = df.loc[work.index, product_code_col].fillna("").astype(str).str.strip()
        if spec_col:
            work["규격"] = df.loc[work.index, spec_col].fillna("").astype(str).str.strip()

        group_cols = ["제품명"]
        if product_code_col:
            group_cols.insert(0, "제품코드")
        if spec_col:
            group_cols.append("규격")

        out = (
            work.groupby(group_cols, dropna=False)
            .agg(
                건수=("_stock", "size"),
                입고수량=("_in", "sum"),
                출고수량=("_out", "sum"),
                재고수량=("_stock", "last"),
                수불금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values("재고수량", ascending=False)
        )

        out.insert(0, "순번", range(1, len(out) + 1))
        out2 = out.head(top_n).copy()

        return push_table(
            title=f"현재표 제품별 재고수량 TOP {top_n}",
            action=f"현재표 제품별 재고수량 TOP {top_n}",
            df=out2,
            query_summary=f"현재표 / 제품별 재고수량 TOP {top_n} / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=top_n,
        )

    # 4) 거래처별 입고/출고수량 분석
    wants_vendor = (
        "거래처별" in t
        and any(w in t for w in ("입고수량", "출고수량", "수불수량", "수량", "분석", "집계"))
    )

    if wants_vendor:
        if not vendor_col:
            return push_notice(
                title="현재표 거래처별 입고/출고수량 분석 불가",
                action="현재표 거래처별 입고/출고수량 분석 불가",
                message="현재표에는 거래처별 분석에 필요한 거래처명 컬럼이 없습니다.",
                query_summary="현재표 / 거래처별 입고/출고수량 분석 불가",
                source_query=t,
            )

        work = _work(vendor_col, "거래처명")
        work = work.loc[_valid_product_mask()].copy()
        work = work[work["거래처명"] != ""]

        out = (
            work.groupby("거래처명", dropna=False)
            .agg(
                건수=("_in", "size"),
                입고수량=("_in", "sum"),
                출고수량=("_out", "sum"),
                수불금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values(["출고수량", "입고수량"], ascending=[False, False])
        )

        out.insert(0, "순번", range(1, len(out) + 1))

        log.info(
            "[chat.followup_table] stock ledger vendor table built source_rows=%s rows=%s table_key=%s",
            len(df),
            len(out),
            table_key,
        )

        return push_table(
            title="현재표 거래처별 입고/출고수량 분석",
            action="현재표 거래처별 입고/출고수량 분석",
            df=out,
            query_summary=f"현재표 / 거래처별 입고수량 출고수량 분석 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
        )

    # 5) 입고/출고/재고수량 조건 목록: 0 이상, -1 이하, 10 초과, 5 미만
    qty_cond_m = re.search(
        r"(입고수량|출고수량|재고수량)(-?\d+(?:\.\d+)?)(이상|이하|초과|미만|같음|동일|=)",
        compact,
    )

    if qty_cond_m and "목록" in t:
        metric_name = qty_cond_m.group(1)
        threshold = float(qty_cond_m.group(2))
        op = qty_cond_m.group(3)

        metric_col = {
            "입고수량": in_col,
            "출고수량": out_col,
            "재고수량": stock_col,
        }.get(metric_name)

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

        mask = mask & _valid_product_mask()

        out = df.loc[mask].copy().reset_index(drop=True)
        
        if "순번" in out.columns:
            out["순번"] = range(1, len(out) + 1)
        else:
            out.insert(0, "순번", range(1, len(out) + 1))

        if date_col and date_col in out.columns:
            out = out.sort_values([date_col, "순번"], ascending=[True, True]).reset_index(drop=True)
            out["순번"] = range(1, len(out) + 1)

        log.info(
            "[chat.followup_table] stock ledger qty filter built metric=%s condition=%s %s source_rows=%s rows=%s table_key=%s",
            metric_name,
            threshold,
            op,
            len(df),
            len(out),
            table_key,
        )

        return push_table(
            title=f"현재표 {metric_name} {threshold:g} {op} 목록",
            action=f"현재표 {metric_name} {threshold:g} {op} 목록",
            df=out,
            query_summary=f"현재표 / {metric_name} {threshold:g} {op} 목록 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
        )

    return False