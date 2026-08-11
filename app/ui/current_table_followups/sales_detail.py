# app/ui/current_table_followups/sales_detail.py
# Create 2026-06-08
# 출고명세 조회              → sales_detail

from __future__ import annotations

from typing import Any, Callable

import re
import pandas as pd


def handle_sales_detail_followup(
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
    출고명세 조회 현재표 전용 후속분석.

    출고명세는 매출/출고 기준 표로 본다.
    - 제품별 매출 TOP N
    - 거래처별 매출 TOP N
    - 매출 횟수가 가장 많은 제품
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
        exact=("거래처명", "매출처명", "출고처명", "거래처"),
        include_any=("거래처", "매출처", "출고처"),
        exclude_any=("코드", "번호", "분류", "구분"),
    )
    vendor_code_col = find_col(
        df,
        exact=("거래처코드", "매출처코드", "출고처코드"),
        include_any=("거래처코드", "매출처코드", "출고처코드"),
        exclude_any=("그룹", "분류", "구분"),
    )

    date_col = find_col(
        df,
        exact=("출고일자", "매출일자", "일자"),
        include_any=("출고일자", "매출일자", "일자", "날짜"),
        exclude_any=("등록", "수정", "유효", "제조", "명세서", "계산서", "전표"),
    )


    qty_col = find_col(
        df,
        exact=("수량", "출고수량", "매출수량"),
        include_any=("수량",),
        exclude_any=("할증", "금액", "단가", "코드"),
    )
    supply_col = find_col(
        df,
        exact=("공급가액", "출고공급가액", "매출공급가액"),
        include_any=("공급가액",),
        exclude_any=("확정", "상세합", "차이", "단가"),
    )
    tax_col = find_col(
        df,
        exact=("세액", "출고세액", "매출세액"),
        include_any=("세액",),
        exclude_any=("확정", "상세합", "차이", "단가"),
    )
    total_col = find_col(
        df,
        exact=("합계금액", "총금액", "매출금액", "출고금액"),
        include_any=("합계금액", "총금액", "매출금액", "출고금액"),
        exclude_any=("단가", "율"),
    )

    if not product_col and not vendor_col:
        return push_notice(
            title="현재표 출고명세 분석 불가",
            action="현재표 출고명세 분석 불가",
            message=(
                "현재표는 출고명세로 보이지만 제품명/거래처명 컬럼을 찾지 못했습니다.\n\n"
                f"현재표 기준 행수: {len(df):,}건\n"
                f"현재표 주요 컬럼: {', '.join(col_names[:40])}"
            ),
            query_summary="현재표 / 출고명세 분석 불가",
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


    # 1) 제품별 매출 TOP N
    # 주의: "제품별 출고수량 TOP 20"은 매출 TOP이 아니라 수량 TOP이므로 제외한다.
    wants_product_sales_top = (
        ("제품별" in t or "품목별" in t)
        and any(w in t for w in ("매출", "매출금액", "금액", "거래금액"))
        and not any(w in t for w in ("수량", "출고수량", "판매수량"))
    )

    if wants_product_sales_top:
        if not product_col:
            return push_notice(
                title="현재표 제품별 매출 TOP 불가",
                action="현재표 제품별 매출 TOP 불가",
                message="현재표에는 제품별 분석에 필요한 제품명/품목명 컬럼이 없습니다.",
                query_summary="현재표 / 제품별 매출 TOP 불가",
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
                매출금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values("매출금액", ascending=False)
        )

        out.insert(0, "순번", range(1, len(out) + 1))
        out2 = out.head(top_n).copy()

        log.info(
            "[chat.followup_table] sales detail product sales top built source_rows=%s rows=%s amount_label=%s table_key=%s",
            len(df),
            len(out2),
            amount_label,
            table_key,
        )

        return push_table(
            title=f"현재표 제품별 매출 TOP {top_n}",
            action=f"현재표 제품별 매출 TOP {top_n}",
            df=out2,
            query_summary=f"현재표 / 제품별 매출 TOP {top_n} / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=top_n,
        )

    # 2) 거래처/매출처/출고처별 매출금액 분석
    is_sales_vendor_group = any(w in t for w in ("거래처별", "매출처별", "출고처별", "실납처별", "납품처별"))
    has_explicit_top = any(w in t for w in ("TOP", "top", "상위"))

    if "매출처별" in t:
        vendor_group_label = "매출처명"
    elif "출고처별" in t:
        vendor_group_label = "출고처명"
    elif "실납처별" in t:
        vendor_group_label = "실납처명"
    elif "납품처별" in t:
        vendor_group_label = "납품처명"
    else:
        vendor_group_label = "거래처명"

    wants_vendor_sales_top = (
        is_sales_vendor_group
        and any(w in t for w in ("매출", "출고", "금액", "거래금액", "분석", "집계", "TOP", "top", "상위"))
    )

    if wants_vendor_sales_top:
        if not vendor_col:
            return push_notice(
                title=f"현재표 {vendor_group_label.replace('명', '')}별 매출 TOP 불가",
                action=f"현재표 {vendor_group_label.replace('명', '')}별 매출 TOP 불가",
                message=f"현재표에는 {vendor_group_label}별 분석에 필요한 {vendor_group_label} 컬럼이 없습니다.",
                query_summary=f"현재표 / {vendor_group_label.replace('명', '')}별 매출 TOP 불가",
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
                매출금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values("매출금액", ascending=False)
        )

        out.insert(0, "순번", range(1, len(out) + 1))

        if has_explicit_top:
            out2 = out.head(top_n).copy()
            title = f"현재표 {vendor_group_label.replace('명', '')}별 매출금액 TOP {top_n}"
            query_summary = f"현재표 / {vendor_group_label.replace('명', '')}별 매출금액 TOP {top_n} / 전체 {len(df):,}건 기준"
            display_limit = top_n
        else:
            out2 = out.copy()
            title = f"현재표 {vendor_group_label.replace('명', '')}별 매출금액"
            query_summary = f"현재표 / {vendor_group_label.replace('명', '')}별 매출금액 / 전체 {len(df):,}건 기준"
            display_limit = None

        log.info(
            "[chat.followup_table] sales detail vendor sales top built source_rows=%s rows=%s amount_label=%s table_key=%s",
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

    # 2-1) 영업사원/담당자별 매출금액 분석
    # 예:
    # - 현재표 영업사원별 매출금액
    # - 현재표 담당자별 매출금액
    # - 현재표 영업사원별 매출 TOP 20
    is_staff_sales_group = any(w in t for w in ("영업사원별", "영업담당자별", "담당자별", "사원별"))

    if is_staff_sales_group and any(w in t for w in ("매출", "금액", "분석", "집계", "TOP", "top", "상위")):
        staff_col = find_col(
            df,
            exact=("영업사원명", "영업담당자명", "담당자명", "사원명"),
            include_any=("영업사원", "영업담당", "담당자", "사원"),
            exclude_any=("코드", "번호", "ID", "아이디"),
        )
        if not staff_col:
            if log:
                log.warning("[chat.followup_table] sales detail staff col not found columns=%s", list(df.columns)[:30])
            return False

        # 금액/수량 컬럼 찾기
        supply_col = find_col(
            df,
            exact=("공급가액", "공급금액", "Rd12_Supply_Price"),
            include_any=("공급가액", "공급금액", "Supply_Price"),
        )
        tax_col = find_col(
            df,
            exact=("세액", "부가세", "Rd12_Tax_Price"),
            include_any=("세액", "부가세", "Tax_Price"),
        )
        amount_col = find_col(
            df,
            exact=("매출금액", "합계금액", "총금액", "금액"),
            include_any=("매출금액", "합계금액", "총금액", "Tot_Amt"),
        )
        qty_col = find_col(
            df,
            exact=("수량", "출고수량", "Rd12_Quantity"),
            include_any=("수량", "Quantity"),
            exclude_any=("할증", "재고", "이월", "입고"),
        )

        work = df.copy()
        work["__영업사원명__"] = (
            work[staff_col]
            .fillna("")
            .astype(str)
            .str.strip()
            .replace("", "(미지정)")
        )

        work["__수량__"] = to_num(work[qty_col]) if qty_col else 0

        if supply_col:
            work["__공급가액__"] = to_num(work[supply_col])
        else:
            work["__공급가액__"] = 0

        if tax_col:
            work["__세액__"] = to_num(work[tax_col])
        else:
            work["__세액__"] = 0

        if amount_col:
            work["__매출금액__"] = to_num(work[amount_col])
        else:
            work["__매출금액__"] = work["__공급가액__"] + work["__세액__"]

        g = work.groupby("__영업사원명__", dropna=False)

        out = pd.DataFrame(
            {
                "영업사원명": g.size().index.astype(str),
                "건수": g.size().values,
                "수량": g["__수량__"].sum().values,
                "공급가액": g["__공급가액__"].sum().values,
                "세액": g["__세액__"].sum().values,
                "매출금액": g["__매출금액__"].sum().values,
            }
        )

        out = out.sort_values("매출금액", ascending=False).reset_index(drop=True)
        out.insert(0, "순번", range(1, len(out) + 1))

        has_explicit_top = bool(re.search(r"(?:TOP|top|상위)\s*(\d{1,4})", t))

        if has_explicit_top:
            out2 = out.head(top_n).copy()
            title = f"현재표 영업사원별 매출금액 TOP {top_n}"
            query_summary = f"현재표 / 영업사원별 매출금액 TOP {top_n} / 전체 {len(df):,}건 기준"
            display_limit = top_n
        else:
            out2 = out.copy()
            title = "현재표 영업사원별 매출금액"
            query_summary = f"현재표 / 영업사원별 매출금액 / 전체 {len(df):,}건 기준"
            display_limit = None

        if log:
            log.info(
                "[chat.followup_table] sales detail staff sales built source_rows=%s rows=%s table_key=%s",
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

    # 2-2) 일자/요일별 매출 최고
    # 예:
    # - 현재표에서 매출이 가장 많은 일자와 요일
    # - 현재표 매출 최고 일자
    # - 현재표 일자별 매출 TOP 20
    wants_sales_date_top = (
        any(w in compact for w in ("매출이가장많은일자", "매출최고일자", "일자별매출", "일별매출"))
        or ("요일" in t and any(w in t for w in ("매출", "금액", "최고", "가장")))
    )

    if wants_sales_date_top:
        if not date_col:
            return push_notice(
                title="현재표 일자별 매출 분석 불가",
                action="현재표 일자별 매출 분석 불가",
                message="현재표에는 일자별 매출 분석에 필요한 출고일자/매출일자 컬럼이 없습니다.",
                query_summary="현재표 / 일자별 매출 분석 불가",
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
                title="현재표 일자별 매출 분석 불가",
                action="현재표 일자별 매출 분석 불가",
                message="현재표에서 유효한 출고일자를 찾지 못했습니다.",
                query_summary="현재표 / 일자별 매출 분석 불가",
                source_query=t,
            )

        daily = (
            work.groupby(["일자", "요일"], dropna=False)
            .agg(
                건수=("_amount", "size"),
                수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                매출금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values("매출금액", ascending=False)
        )

        weekday = (
            work.groupby("요일", dropna=False)
            .agg(
                건수=("_amount", "size"),
                수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                매출금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values("매출금액", ascending=False)
        )

        # "일자와 요일" 질문은 최고 일자 1건 + 최고 요일 1건을 한 표로 반환한다.
        if "요일" in t and any(w in t for w in ("최고", "가장", "많은")):
            top_day = daily.head(1).copy()
            top_week = weekday.head(1).copy()

            out = pd.DataFrame(
                [
                    {
                        "구분": "매출 최고 일자",
                        "값": str(top_day.iloc[0]["일자"]),
                        "요일": str(top_day.iloc[0]["요일"]),
                        "건수": top_day.iloc[0]["건수"],
                        "수량": top_day.iloc[0]["수량"],
                        "공급가액": top_day.iloc[0]["공급가액"],
                        "세액": top_day.iloc[0]["세액"],
                        "매출금액": top_day.iloc[0]["매출금액"],
                    },
                    {
                        "구분": "매출 최고 요일",
                        "값": str(top_week.iloc[0]["요일"]),
                        "요일": str(top_week.iloc[0]["요일"]),
                        "건수": top_week.iloc[0]["건수"],
                        "수량": top_week.iloc[0]["수량"],
                        "공급가액": top_week.iloc[0]["공급가액"],
                        "세액": top_week.iloc[0]["세액"],
                        "매출금액": top_week.iloc[0]["매출금액"],
                    },
                ]
            )
            out.insert(0, "순번", range(1, len(out) + 1))

            title = "현재표 매출 최고 일자/요일"
            query_summary = f"현재표 / 매출 최고 일자와 요일 / 전체 {len(df):,}건 기준"
            display_limit = None
        else:
            out = daily.reset_index(drop=True)
            out.insert(0, "순번", range(1, len(out) + 1))

            has_explicit_top = bool(re.search(r"(?:TOP|top|상위)\s*(\d{1,4})", t))
            if has_explicit_top:
                out = out.head(top_n).copy()
                title = f"현재표 일자별 매출 TOP {top_n}"
                query_summary = f"현재표 / 일자별 매출 TOP {top_n} / 전체 {len(df):,}건 기준"
                display_limit = top_n
            else:
                title = "현재표 일자별 매출"
                query_summary = f"현재표 / 일자별 매출 / 전체 {len(df):,}건 기준"
                display_limit = None

        log.info(
            "[chat.followup_table] sales detail daily sales built source_rows=%s rows=%s table_key=%s",
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

    # 2-3) 월별 매출/출고수량 요약
    # 예:
    # - 현재표 월별 매출 요약
    # - 현재표 월별 매출금액
    # - 현재표 월별 출고수량
    wants_monthly_sales = (
        any(
            w in compact
            for w in (
                "월별매출",
                "월별매출요약",
                "월별매출금액",
                "월별판매",
                "월별출고수량",
                "월별수량",
            )
        )
        or ("월별" in t and any(w in t for w in ("매출", "금액", "요약", "집계", "출고수량", "수량")))
    )

    if wants_monthly_sales:
        if not date_col:
            return push_notice(
                title="현재표 월별 매출 분석 불가",
                action="현재표 월별 매출 분석 불가",
                message="현재표에는 월별 매출 분석에 필요한 출고일자/매출일자 컬럼이 없습니다.",
                query_summary="현재표 / 월별 매출 분석 불가",
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

        if work.empty:
            return push_notice(
                title="현재표 월별 매출 분석 불가",
                action="현재표 월별 매출 분석 불가",
                message="현재표에서 유효한 출고월을 찾지 못했습니다.",
                query_summary="현재표 / 월별 매출 분석 불가",
                source_query=t,
                extra_meta={"execution_status": "no_data", "result_status": "no_data"},
            )

        out = (
            work.groupby("월", dropna=False)
            .agg(
                건수=("_amount", "size"),
                출고수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                매출금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values("월")
        )
        out.insert(0, "순번", range(1, len(out) + 1))

        wants_only_qty = (
            any(w in compact for w in ("월별출고수량", "월별수량"))
            and not any(w in compact for w in ("매출", "매출금액", "금액", "공급가액", "세액"))
        )

        wants_only_supply = "공급가액" in compact and not any(
            w in compact for w in ("매출금액", "합계금액", "거래금액")
        )

        if wants_only_qty:
            title = "현재표 월별 출고수량 요약"
            query_summary = f"현재표 / 월별 출고수량 요약 / 전체 {len(df):,}건 기준"
        elif wants_only_supply:
            title = "현재표 월별 공급가액 요약"
            query_summary = f"현재표 / 월별 공급가액 요약 / 전체 {len(df):,}건 기준"
        else:
            title = "현재표 월별 매출 요약"
            query_summary = f"현재표 / 월별 매출 요약 / 전체 {len(df):,}건 기준"

        log.info(
            "[chat.followup_table] sales detail monthly sales built source_rows=%s rows=%s table_key=%s title=%s",
            len(df),
            len(out),
            table_key,
            title,
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

    # 2-4) 수량이 가장 많은 제품
    # 예:
    # - 현재표에서 수량이 가장 많은 제품
    # - 현재표 제품별 수량 TOP 20
    # - 현재표 출고수량 1위 제품
    wants_product_qty_top = (
        any(w in compact for w in ("수량이가장많은제품", "출고수량이가장많은제품", "수량1위제품", "출고수량1위제품"))
        or (("제품별" in t or "품목별" in t) and any(w in t for w in ("수량", "출고수량")) and any(w in t for w in ("TOP", "top", "상위", "1위", "가장")))
    )

    if wants_product_qty_top:
        if not product_col:
            return push_notice(
                title="현재표 제품별 수량 분석 불가",
                action="현재표 제품별 수량 분석 불가",
                message="현재표에는 제품별 수량 분석에 필요한 제품명/품목명 컬럼이 없습니다.",
                query_summary="현재표 / 제품별 수량 분석 불가",
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
                출고수량=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                매출금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values(["출고수량", "매출금액"], ascending=[False, False])
        )

        out.insert(0, "순번", range(1, len(out) + 1))

        has_explicit_top = bool(re.search(r"(?:TOP|top|상위)\s*(\d{1,4})", t))
        if has_explicit_top:
            out2 = out.head(top_n).copy()
            title = f"현재표 제품별 출고수량 TOP {top_n}"
            query_summary = f"현재표 / 제품별 출고수량 TOP {top_n} / 전체 {len(df):,}건 기준"
            display_limit = top_n
        else:
            out2 = out.head(1).copy()
            title = "현재표 출고수량 1위 제품"
            query_summary = f"현재표 / 출고수량이 가장 많은 제품 / 전체 {len(df):,}건 기준"
            display_limit = 1

        log.info(
            "[chat.followup_table] sales detail product qty top built source_rows=%s rows=%s table_key=%s",
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

    # 3) 매출 횟수가 가장 많은 제품
    wants_product_count_top = any(
        w in compact
        for w in (
            "매출횟수가가장많은제품",
            "가장매출횟수가많은제품",
            "매출회수가가장많은제품",
            "가장매출회수가많은제품",
            "매출건수가가장많은제품",
            "가장매출건수가많은제품",
            "판매횟수가가장많은제품",
            "가장판매횟수가많은제품",
            "거래횟수가가장많은제품",
            "가장거래횟수가많은제품",
        )
    )

    if wants_product_count_top:
        if not product_col:
            return push_notice(
                title="현재표 매출횟수 1위 제품 불가",
                action="현재표 매출횟수 1위 제품 불가",
                message="현재표에는 제품별 분석에 필요한 제품명/품목명 컬럼이 없습니다.",
                query_summary="현재표 / 매출횟수 1위 제품 불가",
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
                매출횟수=("_amount", "size"),
                수량합계=("_qty", "sum"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                매출금액=("_amount", "sum"),
            )
            .reset_index()
            .sort_values(["매출횟수", "매출금액"], ascending=[False, False])
        )

        out.insert(0, "순번", range(1, len(out) + 1))
        out2 = out.head(1).copy()

        log.info(
            "[chat.followup_table] sales detail product count top built source_rows=%s rows=%s table_key=%s",
            len(df),
            len(out2),
            table_key,
        )

        return push_table(
            title="현재표 매출횟수 1위 제품",
            action="현재표 매출횟수 1위 제품",
            df=out2,
            query_summary=f"현재표 / 매출횟수가 가장 많은 제품 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=1,
        )

    return False
