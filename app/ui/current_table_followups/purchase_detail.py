# app/ui/current_table_followups/purchase_detail.py
# Create 2026-06-08
# 입고명세 조회              → purchase_detail

from __future__ import annotations

from typing import Any, Callable

import re
import pandas as pd


def handle_purchase_detail_followup(
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
    입고명세 조회 현재표 전용 후속분석.

    입고명세는 매입/입고 기준 표로 본다.
    - 제품별 매입 TOP N
    - 거래처별 매입 TOP N
    - 제품별 입고수량 TOP N
    - 거래처별 입고수량 분석
    - 입고/매입 횟수가 가장 많은 제품
    """
    t = str(query or "").strip()
    compact = t.replace(" ", "")

    find_col = helpers["find_col"]
    to_num = helpers["to_num"]
    push_table = helpers["push_table"]
    push_notice = helpers["push_notice"]

    col_names = [str(c).strip() for c in df.columns]

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
        exact=("거래처명", "매입처명", "입고처명", "거래처"),
        include_any=("거래처", "매입처", "입고처"),
        exclude_any=("코드", "번호", "분류", "구분"),
    )
    vendor_code_col = find_col(
        df,
        exact=("거래처코드", "매입처코드", "입고처코드"),
        include_any=("거래처코드", "매입처코드", "입고처코드"),
        exclude_any=("그룹", "분류", "구분"),
    )

    date_col = find_col(
        df,
        exact=("입고일자", "매입일자", "일자"),
        include_any=("입고일자", "매입일자", "일자", "날짜"),
        exclude_any=("등록", "수정", "유효", "제조", "명세서", "계산서", "전표"),
    )

    qty_col = find_col(
        df,
        exact=("수량", "입고수량", "매입수량"),
        include_any=("수량",),
        exclude_any=("할증", "금액", "단가", "코드"),
    )
    supply_col = find_col(
        df,
        exact=("공급가액", "입고공급가액", "매입공급가액"),
        include_any=("공급가액",),
        exclude_any=("확정", "상세합", "차이", "단가"),
    )
    tax_col = find_col(
        df,
        exact=("세액", "입고세액", "매입세액"),
        include_any=("세액",),
        exclude_any=("확정", "상세합", "차이", "단가"),
    )
    total_col = find_col(
        df,
        exact=("합계금액", "총금액", "매입금액", "입고금액"),
        include_any=("합계금액", "총금액", "매입금액", "입고금액"),
        exclude_any=("단가", "율"),
    )

    if not product_col and not vendor_col:
        return push_notice(
            title="현재표 입고명세 분석 불가",
            action="현재표 입고명세 분석 불가",
            message=(
                "현재표는 입고명세로 보이지만 제품명/거래처명 컬럼을 찾지 못했습니다.\n\n"
                f"현재표 기준 행수: {len(df):,}건\n"
                f"현재표 주요 컬럼: {', '.join(col_names[:40])}"
            ),
            query_summary="현재표 / 입고명세 분석 불가",
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
            | name.str.contains(r"^(합계|총계|소계|합계금액|전체|TOTAL)$", case=False, regex=True, na=False)
            | name.str.contains(r"합계\s*금액", case=False, regex=True, na=False)
        )
        return ~bad

    qty_s = _series(qty_col)
    supply_s = _series(supply_col)
    tax_s = _series(tax_col)

    if total_col:
        amount_s = _series(total_col)
        amount_label = total_col
    else:
        amount_s = supply_s + tax_s
        amount_label = "공급가액+세액"

    # 입고명세 현재표에서 매출/출고 질문이 들어오면 매입표로 잘못 응답하지 않는다.
    if any(
        w in compact
        for w in (
            "매출거래처별",
            "매출처별",
            "매출금액",
            "출고금액",
            "출고수량",
            "매출수량",
            "영업사원별매출",
            "담당자별매출",
        )
    ):
        return push_notice(
            title="현재표 매출 분석 불가",
            action="현재표 매출 분석 불가",
            message=(
                "현재표 원본은 입고명세입니다.\n"
                "매출/출고 기준 분석은 출고명세 조회 후 실행해야 합니다.\n\n"
                "예:\n"
                "- 출고명세 2026 조회\n"
                "- 현재표 매출거래처별 매출금액\n"
                "- 현재표 제품별 매출금액 TOP 20"
            ),
            query_summary=f"현재표 / 매출 분석 불가 / 원본={source_action}",
            source_query=t,
        )


    def _base_work(group_col: str, group_label: str) -> pd.DataFrame:
        work = pd.DataFrame(
            {
                group_label: _text_series(group_col),
                "_qty": qty_s,
                "_supply": supply_s,
                "_tax": tax_s,
                "_amount": amount_s,
            },
            index=df.index,
        )
        work = work[_valid_name_mask(work[group_label])].copy()
        return work

    def _append_optional_cols(work: pd.DataFrame, cols: list[tuple[str | None, str]]) -> pd.DataFrame:
        for src_col, label in cols:
            if src_col and src_col in df.columns:
                work[label] = df.loc[work.index, src_col].fillna("").astype(str).str.strip()
        return work

    # 1) 제품별 매입/입고금액 TOP N / 1위 제품
    wants_product_purchase_first = (
        any(
            w in compact
            for w in (
                "입고금액이가장많은제품",
                "매입금액이가장많은제품",
                "입고금액이제일많은제품",
                "매입금액이제일많은제품",
                "입고금액최고제품",
                "매입금액최고제품",
                "입고금액1위제품",
                "매입금액1위제품",
                "금액이가장많은제품",
                "금액이제일많은제품",
                "금액최고제품",
                "금액1위제품",
            )
        )
        and "입고수량" not in compact
        and "수량" not in compact
    )

    wants_product_purchase_top = (
        (
            ("제품별" in t or "품목별" in t)
            and any(w in t for w in ("매입", "입고금액", "매입금액", "거래금액", "금액", "TOP", "top", "상위"))
            and "입고수량" not in t
        )
        or wants_product_purchase_first
    )

    if wants_product_purchase_top:
        if not product_col:
            return push_notice(
                title="현재표 제품별 매입 TOP 불가",
                action="현재표 제품별 매입 TOP 불가",
                message="현재표에는 제품별 분석에 필요한 제품명/품목명 컬럼이 없습니다.",
                query_summary="현재표 / 제품별 매입 TOP 불가",
                source_query=t,
            )

        work = _base_work(product_col, "제품명")
        work = _append_optional_cols(
            work,
            [
                (product_code_col, "제품코드"),
                (spec_col, "규격"),
            ],
        )

        group_cols = ["제품명"]
        if "제품코드" in work.columns:
            group_cols.insert(0, "제품코드")
        if "규격" in work.columns:
            group_cols.append("규격")

        out = (
            work.groupby(group_cols, dropna=False)
            .agg(
                건수=("_amount", "size"),
                수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                매입금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values("매입금액", ascending=False)
        )

        out.insert(0, "순번", range(1, len(out) + 1))

        explicit_top = bool(re.search(r"(?:TOP|top|상위)\s*(\d{1,4})", t))
        amount_title_word = "입고금액" if "입고금액" in t or "입고" in t else "매입금액"

        if wants_product_purchase_first:
            out2 = out.head(1).copy()
            title = f"현재표 {amount_title_word} 1위 제품"
            query_summary = f"현재표 / {amount_title_word}이 가장 많은 제품 / 전체 {len(df):,}건 기준"
            display_limit = 1
        elif explicit_top:
            out2 = out.head(top_n).copy()
            title = f"현재표 제품별 {amount_title_word} TOP {top_n}"
            query_summary = f"현재표 / 제품별 {amount_title_word} TOP {top_n} / 전체 {len(df):,}건 기준"
            display_limit = top_n
        else:
            out2 = out.head(top_n).copy()
            title = f"현재표 제품별 {amount_title_word} TOP {top_n}"
            query_summary = f"현재표 / 제품별 {amount_title_word} TOP {top_n} / 전체 {len(df):,}건 기준"
            display_limit = top_n

        log.info(
            "[chat.followup_table] purchase detail product purchase top built source_rows=%s rows=%s amount_label=%s table_key=%s",
            len(df),
            len(out2),
            amount_label,
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
            display_limit=display_limit,
        )

    def _date_ymd_series(col: str | None) -> pd.Series:
        if not col or col not in df.columns:
            return pd.Series([""] * len(df), index=df.index, dtype="object")

        s = (
            df[col]
            .fillna("")
            .astype(str)
            .str.strip()
            .str.replace("-", "", regex=False)
            .str.replace("/", "", regex=False)
            .str.replace(".", "", regex=False)
        )
        return s.str.extract(r"(\d{8})", expand=False).fillna("").astype(str)

    def _weekday_kr_series(ymd_s: pd.Series) -> pd.Series:
        dt = pd.to_datetime(ymd_s, format="%Y%m%d", errors="coerce")
        week_map = {
            0: "월요일",
            1: "화요일",
            2: "수요일",
            3: "목요일",
            4: "금요일",
            5: "토요일",
            6: "일요일",
        }
        return dt.dt.dayofweek.map(week_map).fillna("(일자없음)")


    # 2) 거래처/매입처/입고처별 매입금액 분석
    is_purchase_vendor_group = any(w in t for w in ("거래처별", "매입처별", "입고처별"))
    has_explicit_top = any(w in t for w in ("TOP", "top", "상위"))

    if "매입처별" in t:
        vendor_group_label = "매입처명"
        amount_title_word = "매입금액"
    elif "입고처별" in t:
        vendor_group_label = "입고처명"
        amount_title_word = "입고금액"
    else:
        vendor_group_label = "거래처명"
        amount_title_word = "매입금액"

    wants_vendor_purchase_top = (
        is_purchase_vendor_group
        and any(w in t for w in ("매입", "입고", "입고금액", "거래금액", "금액", "분석", "집계", "TOP", "top", "상위"))
        and "입고수량" not in t
    )

    if wants_vendor_purchase_top:
        if not vendor_col:
            return push_notice(
                title="현재표 거래처별 매입 TOP 불가",
                action="현재표 거래처별 매입 TOP 불가",
                message="현재표에는 거래처별 분석에 필요한 거래처명 컬럼이 없습니다.",
                query_summary="현재표 / 거래처별 매입 TOP 불가",
                source_query=t,
            )

        work = _base_work(vendor_col, vendor_group_label)
        work = _append_optional_cols(
            work,
            [
                (vendor_code_col, "거래처코드"),
            ],
        )

        group_cols = [vendor_group_label]
        if "거래처코드" in work.columns:
            group_cols.insert(0, "거래처코드")

        out = (
            work.groupby(group_cols, dropna=False)
            .agg(
                건수=("_amount", "size"),
                수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                매입금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values("매입금액", ascending=False)
        )

        out.insert(0, "순번", range(1, len(out) + 1))

        if has_explicit_top:
            out2 = out.head(top_n).copy()
            title = f"현재표 {vendor_group_label.replace('명', '')}별 {amount_title_word} TOP {top_n}"
            query_summary = f"현재표 / {vendor_group_label.replace('명', '')}별 {amount_title_word} TOP {top_n} / 전체 {len(df):,}건 기준"
            display_limit = top_n
        else:
            out2 = out.copy()
            title = f"현재표 {vendor_group_label.replace('명', '')}별 {amount_title_word}"
            query_summary = f"현재표 / {vendor_group_label.replace('명', '')}별 {amount_title_word} / 전체 {len(df):,}건 기준"
            display_limit = None

        log.info(
            "[chat.followup_table] purchase detail vendor purchase top built source_rows=%s rows=%s amount_label=%s table_key=%s",
            len(df),
            len(out2),
            amount_label,
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
            display_limit=display_limit,
        )

    # 2-2) 일자별 입고금액 / 입고금액 최고 일자
    # 예:
    # - 현재표 일자별 입고금액
    # - 현재표 일자별 입고금액 TOP 5
    # - 현재표 입고금액 최고 일자
    # - 현재표에서 입고금액이 가장 많은 일자와 요일
    wants_purchase_date_group = (
        any(
            w in compact
            for w in (
                "일자별입고",
                "일자별매입",
                "일별입고",
                "일별매입",
                "일자별입고금액",
                "일자별매입금액",
                "일별입고금액",
                "일별매입금액",
                "입고금액이가장많은일자",
                "매입금액이가장많은일자",
                "입고최고일자",
                "매입최고일자",
                "입고금액최고일자",
                "매입금액최고일자",
                "입고금액최고",
                "매입금액최고",                
            )
        )
        or (
            ("일자별" in t or "일별" in t)
            and any(w in t for w in ("입고", "매입", "금액", "요약", "집계", "TOP", "top", "상위"))
        )
        or ("요일" in t and any(w in t for w in ("입고", "매입", "금액", "최고", "가장")))
    )

    if wants_purchase_date_group:
        if not date_col:
            return push_notice(
                title="현재표 일자별 입고 분석 불가",
                action="현재표 일자별 입고 분석 불가",
                message="현재표에는 일자별 입고 분석에 필요한 입고일자/매입일자 컬럼이 없습니다.",
                query_summary="현재표 / 일자별 입고 분석 불가",
                source_query=t,
            )

        ymd_s = _date_ymd_series(date_col)
        work = pd.DataFrame(
            {
                "일자": ymd_s,
                "요일": _weekday_kr_series(ymd_s),
                "_qty": qty_s,
                "_supply": supply_s,
                "_tax": tax_s,
                "_amount": amount_s,
            },
            index=df.index,
        )
        work = work[work["일자"].astype(str).str.len().eq(8)].copy()

        if work.empty:
            return push_notice(
                title="현재표 일자별 입고 분석 불가",
                action="현재표 일자별 입고 분석 불가",
                message="현재표에서 유효한 입고일자를 찾지 못했습니다.",
                query_summary="현재표 / 일자별 입고 분석 불가",
                source_query=t,
            )

        daily = (
            work.groupby(["일자", "요일"], dropna=False)
            .agg(
                건수=("_amount", "size"),
                수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                입고금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values("입고금액", ascending=False)
        )

        weekday = (
            work.groupby("요일", dropna=False)
            .agg(
                건수=("_amount", "size"),
                수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                입고금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values("입고금액", ascending=False)
        )

        asks_best_day_week = (
            any(
                w in compact
                for w in (
                    "입고금액이가장많은일자",
                    "매입금액이가장많은일자",
                    "입고최고일자",
                    "매입최고일자",
                    "최고일자",
                    "가장많은일자",
                    "입고금액최고일자",
                    "매입금액최고일자",
                    "입고금액최고",
                    "매입금액최고",
                )
            )
            or ("요일" in t and any(w in t for w in ("최고", "가장", "많은")))
            or (
                "일자" in t
                and any(w in t for w in ("입고금액", "매입금액"))
                and any(w in t for w in ("최고", "가장", "많은"))
            )            
        )

        asks_best_weekday_only = (
            any(
                w in compact
                for w in (
                    "입고금액최고요일",
                    "매입금액최고요일",
                    "최고요일",
                    "가장많은요일",
                    "요일별입고금액최고",
                    "요일별매입금액최고",
                )
            )
            or (
                "요일" in t
                and "일자" not in t
                and any(w in t for w in ("입고금액", "매입금액", "입고", "매입"))
                and any(w in t for w in ("최고", "가장", "많은"))
            )
        )

        if asks_best_day_week:
            top_day = daily.head(1).copy()
            top_week = weekday.head(1).copy()

            if asks_best_weekday_only:
                out = pd.DataFrame(
                    [
                        {
                            "구분": "입고금액 최고 요일",
                            "값": str(top_week.iloc[0]["요일"]),
                            "요일": str(top_week.iloc[0]["요일"]),
                            "건수": top_week.iloc[0]["건수"],
                            "수량": top_week.iloc[0]["수량"],
                            "공급가액": top_week.iloc[0]["공급가액"],
                            "세액": top_week.iloc[0]["세액"],
                            "입고금액": top_week.iloc[0]["입고금액"],
                        }
                    ]
                )
                title = "현재표 입고금액 최고 요일"
                query_summary = f"현재표 / 입고금액 최고 요일 / 전체 {len(df):,}건 기준"
            else:
                out = pd.DataFrame(
                    [
                        {
                            "구분": "입고금액 최고 일자",
                            "일자": str(top_day.iloc[0]["일자"]),
                            "요일": str(top_day.iloc[0]["요일"]),
                            "건수": top_day.iloc[0]["건수"],
                            "수량": top_day.iloc[0]["수량"],
                            "공급가액": top_day.iloc[0]["공급가액"],
                            "세액": top_day.iloc[0]["세액"],
                            "입고금액": top_day.iloc[0]["입고금액"],
                        }
                    ]
                )
                title = "현재표 입고금액 최고 일자"
                query_summary = f"현재표 / 입고금액 최고 일자 / 전체 {len(df):,}건 기준"

            out.insert(0, "순번", range(1, len(out) + 1))
            display_limit = None

        else:
            out = daily.reset_index(drop=True)
            out.insert(0, "순번", range(1, len(out) + 1))

            has_explicit_top = bool(re.search(r"(?:TOP|top|상위)\s*(\d{1,4})", t))

            if has_explicit_top:
                out = out.head(top_n).copy()
                title = f"현재표 일자별 입고금액 TOP {top_n}"
                query_summary = f"현재표 / 일자별 입고금액 TOP {top_n} / 전체 {len(df):,}건 기준"
                display_limit = top_n
            else:
                title = "현재표 일자별 입고금액"
                query_summary = f"현재표 / 일자별 입고금액 / 전체 {len(df):,}건 기준"
                display_limit = None

        log.info(
            "[chat.followup_table] purchase detail daily purchase built source_rows=%s rows=%s table_key=%s",
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
        
    # 2-3) 월별 입고 요약
    wants_monthly_purchase = (
        any(w in compact for w in ("월별입고", "월별입고요약", "월별입고금액", "월별매입", "월별매입금액"))
        or ("월별" in t and any(w in t for w in ("입고", "매입", "금액", "요약", "집계")))
    )

    if wants_monthly_purchase:
        if not date_col:
            return push_notice(
                title="현재표 월별 입고 분석 불가",
                action="현재표 월별 입고 분석 불가",
                message="현재표에는 월별 입고 분석에 필요한 입고일자/매입일자 컬럼이 없습니다.",
                query_summary="현재표 / 월별 입고 분석 불가",
                source_query=t,
            )

        ymd_s = _date_ymd_series(date_col)
        month_s = ymd_s.str.slice(0, 6)

        work = pd.DataFrame(
            {
                "월": month_s,
                "_qty": qty_s,
                "_supply": supply_s,
                "_tax": tax_s,
                "_amount": amount_s,
            },
            index=df.index,
        )
        work = work[work["월"].astype(str).str.len().eq(6)].copy()

        out = (
            work.groupby("월", dropna=False)
            .agg(
                건수=("_amount", "size"),
                수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                입고금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values("월")
        )
        out.insert(0, "순번", range(1, len(out) + 1))

        explicit_top = bool(re.search(r"(?:TOP|top|상위)\s*(\d{1,4})", t))
        amount_title_word = "매입금액" if "매입" in t else "입고금액"

        if explicit_top:
            out = out.sort_values("입고금액", ascending=False).head(top_n).reset_index(drop=True)
            out.insert(0, "순번", range(1, len(out) + 1))
            title = f"현재표 월별 {amount_title_word} TOP {top_n}"
            query_summary = f"현재표 / 월별 {amount_title_word} TOP {top_n} / 전체 {len(df):,}건 기준"
            display_limit = top_n
        else:
            title = f"현재표 월별 {amount_title_word} 요약"
            query_summary = f"현재표 / 월별 {amount_title_word} 요약 / 전체 {len(df):,}건 기준"
            display_limit = None

        log.info(
            "[chat.followup_table] purchase detail monthly purchase built source_rows=%s rows=%s table_key=%s",
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

    # 3) 제품별 입고수량 TOP N
    wants_product_in_qty_top = (
        (
            ("제품별" in t or "품목별" in t)
            and "입고수량" in t
            and any(w in t for w in ("TOP", "top", "상위", "많은"))
        )
        or any(w in compact for w in ("입고수량이가장많은제품", "입고수량1위제품", "수량이가장많은제품", "수량1위제품"))
    )

    # 제품별 입고횟수 1위
    # 예:
    # - 현재표에서 가장 입고 횟수가 많은 제품
    # - 현재표에서 가장 입고 건수가 많은 제품
    # - 현재표 제품별 입고횟수 TOP 20
    wants_product_in_count_top = any(
        w in compact
        for w in (
            "입고횟수가가장많은제품",
            "가장입고횟수가많은제품",
            "입고회수가가장많은제품",
            "가장입고회수가많은제품",
            "입고건수가가장많은제품",
            "가장입고건수가많은제품",
            "매입횟수가가장많은제품",
            "가장매입횟수가많은제품",
            "매입건수가가장많은제품",
            "가장매입건수가많은제품",
        )
    ) or (
        ("제품별" in t or "품목별" in t)
        and any(w in t for w in ("입고횟수", "입고 횟수", "입고건수", "입고 건수", "매입횟수", "매입건수"))
        and any(w in t for w in ("TOP", "top", "상위", "1위", "가장"))
    )

    if wants_product_in_count_top:
        if not product_col:
            return push_notice(
                title="현재표 입고횟수 1위 제품 불가",
                action="현재표 입고횟수 1위 제품 불가",
                message="현재표에는 제품별 분석에 필요한 제품명/품목명 컬럼이 없습니다.",
                query_summary="현재표 / 입고횟수 1위 제품 불가",
                source_query=t,
            )

        work = _base_work(product_col, "제품명")
        work = _append_optional_cols(
            work,
            [
                (product_code_col, "제품코드"),
                (spec_col, "규격"),
            ],
        )

        group_cols = ["제품명"]
        if "제품코드" in work.columns:
            group_cols.insert(0, "제품코드")
        if "규격" in work.columns:
            group_cols.append("규격")

        out = (
            work.groupby(group_cols, dropna=False)
            .agg(
                입고횟수=("_amount", "size"),
                입고수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                매입금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values(["입고횟수", "매입금액"], ascending=[False, False])
        )

        out.insert(0, "순번", range(1, len(out) + 1))

        has_explicit_top = bool(re.search(r"(?:TOP|top|상위)\s*(\d{1,4})", t))
        if has_explicit_top:
            out2 = out.head(top_n).copy()
            title = f"현재표 제품별 입고횟수 TOP {top_n}"
            query_summary = f"현재표 / 제품별 입고횟수 TOP {top_n} / 전체 {len(df):,}건 기준"
            display_limit = top_n
        else:
            out2 = out.head(1).copy()
            title = "현재표 입고횟수 1위 제품"
            query_summary = f"현재표 / 입고횟수가 가장 많은 제품 / 전체 {len(df):,}건 기준"
            display_limit = 1

        log.info(
            "[chat.followup_table] purchase detail product count top built source_rows=%s rows=%s table_key=%s",
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
            display_limit=display_limit,
        )

    if wants_product_in_qty_top:
        if not product_col:
            return push_notice(
                title="현재표 제품별 입고수량 TOP 불가",
                action="현재표 제품별 입고수량 TOP 불가",
                message="현재표에는 제품별 분석에 필요한 제품명/품목명 컬럼이 없습니다.",
                query_summary="현재표 / 제품별 입고수량 TOP 불가",
                source_query=t,
            )

        work = _base_work(product_col, "제품명")
        work = _append_optional_cols(
            work,
            [
                (product_code_col, "제품코드"),
                (spec_col, "규격"),
            ],
        )

        group_cols = ["제품명"]
        if "제품코드" in work.columns:
            group_cols.insert(0, "제품코드")
        if "규격" in work.columns:
            group_cols.append("규격")

        out = (
            work.groupby(group_cols, dropna=False)
            .agg(
                건수=("_qty", "size"),
                입고수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                매입금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values("입고수량", ascending=False)
        )

        out.insert(0, "순번", range(1, len(out) + 1))

        has_explicit_top = bool(re.search(r"(?:TOP|top|상위)\s*(\d{1,4})", t))
        if has_explicit_top:
            out2 = out.head(top_n).copy()
            title = f"현재표 제품별 입고수량 TOP {top_n}"
            query_summary = f"현재표 / 제품별 입고수량 TOP {top_n} / 전체 {len(df):,}건 기준"
            display_limit = top_n
        else:
            out2 = out.head(1).copy()
            title = "현재표 입고수량 1위 제품"
            query_summary = f"현재표 / 입고수량이 가장 많은 제품 / 전체 {len(df):,}건 기준"
            display_limit = 1

        log.info(
            "[chat.followup_table] purchase detail product in qty top built source_rows=%s rows=%s table_key=%s",
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
            display_limit=display_limit,
        )

    # 4) 거래처별 입고수량 분석
    wants_vendor_in_qty = (
        "거래처별" in t
        and any(w in t for w in ("입고수량", "수량", "입고", "분석", "집계"))
    )

    if wants_vendor_in_qty:
        if not vendor_col:
            return push_notice(
                title="현재표 거래처별 입고수량 분석 불가",
                action="현재표 거래처별 입고수량 분석 불가",
                message="현재표에는 거래처별 분석에 필요한 거래처명 컬럼이 없습니다.",
                query_summary="현재표 / 거래처별 입고수량 분석 불가",
                source_query=t,
            )

        work = _base_work(vendor_col, "거래처명")
        work = _append_optional_cols(
            work,
            [
                (vendor_code_col, "거래처코드"),
            ],
        )

        group_cols = ["거래처명"]
        if "거래처코드" in work.columns:
            group_cols.insert(0, "거래처코드")

        out = (
            work.groupby(group_cols, dropna=False)
            .agg(
                건수=("_qty", "size"),
                입고수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                매입금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values("입고수량", ascending=False)
        )

        out.insert(0, "순번", range(1, len(out) + 1))

        log.info(
            "[chat.followup_table] purchase detail vendor in qty built source_rows=%s rows=%s table_key=%s",
            len(df),
            len(out),
            table_key,
        )

        return push_table(
            title="현재표 거래처별 입고수량 분석",
            action="현재표 거래처별 입고수량 분석",
            df=out,
            query_summary=f"현재표 / 거래처별 입고수량 분석 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
        )

    # 5) 입고/매입 횟수가 가장 많은 제품
    wants_product_count_top = any(
        w in compact
        for w in (
            "입고횟수가가장많은제품",
            "가장입고횟수가많은제품",
            "입고건수가가장많은제품",
            "가장입고건수가많은제품",
            "매입횟수가가장많은제품",
            "가장매입횟수가많은제품",
            "매입건수가가장많은제품",
            "가장매입건수가많은제품",
            "거래횟수가가장많은제품",
            "가장거래횟수가많은제품",
        )
    )

    if wants_product_count_top:
        if not product_col:
            return push_notice(
                title="현재표 입고횟수 1위 제품 불가",
                action="현재표 입고횟수 1위 제품 불가",
                message="현재표에는 제품별 분석에 필요한 제품명/품목명 컬럼이 없습니다.",
                query_summary="현재표 / 입고횟수 1위 제품 불가",
                source_query=t,
            )

        work = _base_work(product_col, "제품명")
        work = _append_optional_cols(
            work,
            [
                (product_code_col, "제품코드"),
                (spec_col, "규격"),
            ],
        )

        group_cols = ["제품명"]
        if "제품코드" in work.columns:
            group_cols.insert(0, "제품코드")
        if "규격" in work.columns:
            group_cols.append("규격")

        out = (
            work.groupby(group_cols, dropna=False)
            .agg(
                입고횟수=("_amount", "size"),
                수량합계=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                매입금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values(["입고횟수", "매입금액"], ascending=[False, False])
        )

        out.insert(0, "순번", range(1, len(out) + 1))
        out2 = out.head(1).copy()

        log.info(
            "[chat.followup_table] purchase detail product count top built source_rows=%s rows=%s table_key=%s",
            len(df),
            len(out2),
            table_key,
        )

        return push_table(
            title="현재표 입고횟수 1위 제품",
            action="현재표 입고횟수 1위 제품",
            df=out2,
            query_summary=f"현재표 / 입고횟수가 가장 많은 제품 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=1,
        )

    return False

   