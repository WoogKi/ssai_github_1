# app/ui/current_table_followups/tax_doc.py
# Create 2026-06-08
# 세금계산서 공통 조회       → tax_doc

from __future__ import annotations

from typing import Any, Callable

import re

import pandas as pd


def handle_tax_doc_followup(
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
    세금계산서 공통 조회 현재표 전용 후속분석.

    세금계산서 공통은 계산서/거래처/일자 기준 표로 본다.
    - 거래처별 계산서금액
    - 월별 계산서금액
    - 일자별 계산서금액
    - 세금계산서구분별 계산서금액
    - 제품별 계산서금액은 기본적으로 불가 안내
    """
    t = str(query or "").strip()
    compact = re.sub(r"\s+", "", t)

    find_col = helpers["find_col"]
    to_num = helpers["to_num"]
    push_table = helpers["push_table"]
    push_notice = helpers["push_notice"]

    col_names = [str(c).strip() for c in df.columns]

    date_col = find_col(
        df,
        exact=("세금계산서일자", "계산서일자", "일자"),
        include_any=("계산서일자", "일자"),
        exclude_any=("등록", "수정", "발행"),
    )
    vendor_col = find_col(
        df,
        exact=("거래처명", "매입처명", "매출처명"),
        include_any=("거래처", "매입처", "매출처"),
        exclude_any=("코드", "번호", "분류", "구분"),
    )
    vendor_code_col = find_col(
        df,
        exact=("거래처코드", "매입처코드", "매출처코드"),
        include_any=("거래처코드", "매입처코드", "매출처코드"),
        exclude_any=("그룹", "분류", "구분"),
    )
    tax_type_col = find_col(
        df,
        exact=("세금계산서구분명", "세금계산서구분", "매입매출구분"),
        include_any=("세금계산서구분", "매입매출구분"),
        exclude_any=("대분류코드", "코드"),
    )

    supply_col = find_col(
        df,
        exact=("공급가액",),
        include_any=("공급가액",),
        exclude_any=("상세합", "확정", "차이", "단가"),
    )
    tax_col = find_col(
        df,
        exact=("세액",),
        include_any=("세액",),
        exclude_any=("상세합", "확정", "차이", "단가"),
    )
    total_col = find_col(
        df,
        exact=("합계금액", "계산서금액", "총금액"),
        include_any=("합계금액", "계산서금액", "총금액"),
        exclude_any=("단가", "율"),
    )

    detail_supply_col = find_col(
        df,
        exact=("상세합_공급가액", "상세합공급가액", "상세공급가액"),
        include_any=("상세합", "공급가액"),
        exclude_any=("단가", "율"),
    )
    detail_tax_col = find_col(
        df,
        exact=("상세합_세액", "상세합세액", "상세세액"),
        include_any=("상세합", "세액"),
        exclude_any=("단가", "율"),
    )

    if not supply_col and not tax_col and not total_col:
        return push_notice(
            title="현재표 계산서금액 분석 불가",
            action="현재표 계산서금액 분석 불가",
            message=(
                "현재표는 세금계산서 공통표로 보이지만 공급가액/세액/합계금액 컬럼을 찾지 못했습니다.\n\n"
                f"현재표 기준 행수: {len(df):,}건\n"
                f"현재표 주요 컬럼: {', '.join(col_names[:40])}"
            ),
            query_summary="현재표 / 계산서금액 분석 불가",
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

    supply_s = _series(supply_col)
    tax_s = _series(tax_col)

    if total_col:
        amount_s = _series(total_col)
        amount_label = total_col
    else:
        amount_s = supply_s + tax_s
        amount_label = "공급가액+세액"

    detail_supply_s = _series(detail_supply_col)
    detail_tax_s = _series(detail_tax_col)

    def _tax_type_name(raw: pd.Series) -> pd.Series:
        s = raw.fillna("").astype(str).str.strip()
        return s.replace(
            {
                "1": "매입분",
                "2": "회계매입",
                "3": "매출분",
                "4": "회계매출",
                "입고": "매입분",
                "출고": "매출분",
                "매입": "매입분",
                "매출": "매출분",
            }
        ).replace("", "(구분없음)")

    if tax_type_col:
        type_s = _tax_type_name(_text_series(tax_type_col))
    else:
        type_s = pd.Series(["(구분없음)"] * len(df), index=df.index, dtype="object")

    base_mask = pd.Series([True] * len(df), index=df.index)

    def _wants_total_amount() -> bool:
        """
        세금계산서 공통 후속분석에서 매입/매출 구분 없이 통합 집계를 원할 때.
        """
        return any(
            w in compact
            for w in (
                "통합",
                "합산",
                "총합",
                "총계",
                "전체합계",
                "합계만",
                "구분없이",
                "구분없",
                "매입매출합계",
                "총계산서",
            )
        )

    # 후속질문에서 매입/입고, 매출/출고를 붙이면 현재표 안에서 한 번 더 구분 필터 적용
    if any(w in compact for w in ("매입", "입고")) and not any(w in compact for w in ("매출", "출고")):
        base_mask = type_s.astype(str).str.contains("매입|입고", regex=True, na=False)
    elif any(w in compact for w in ("매출", "출고")) and not any(w in compact for w in ("매입", "입고")):
        base_mask = type_s.astype(str).str.contains("매출|출고", regex=True, na=False)

    def _valid_name_mask(s: pd.Series) -> pd.Series:
        name = s.fillna("").astype(str).str.strip()
        bad = (
            name.eq("")
            | name.str.contains(r"^(합계|총계|소계|합계금액|전체|TOTAL)$", case=False, regex=True, na=False)
            | name.str.contains(r"합계\s*금액", case=False, regex=True, na=False)
        )
        return ~bad

    def _base_work(group_col: str | None = None, group_label: str | None = None) -> pd.DataFrame:
        work = pd.DataFrame(
            {
                "_supply": supply_s,
                "_tax": tax_s,
                "_amount": amount_s,
                "_detail_supply": detail_supply_s,
                "_detail_tax": detail_tax_s,
                "_type": type_s,
            },
            index=df.index,
        )

        if group_col and group_label:
            work[group_label] = _text_series(group_col)
            work = work[_valid_name_mask(work[group_label])].copy()

        work = work[base_mask].copy()
        work["_supply_diff"] = work["_supply"] - work["_detail_supply"]
        work["_tax_diff"] = work["_tax"] - work["_detail_tax"]
        return work


    def _build_group_table(
        *,
        group_col: str,
        group_label: str,
        sort_col: str = "계산서금액",
        split_rows: bool = False,
    ) -> pd.DataFrame:
        work = _base_work(group_col, group_label)
        if work.empty:
            return pd.DataFrame()

        if split_rows:
            work["세금계산서구분"] = work["_type"].astype(str).str.strip().replace("", "(구분없음)")
            group_keys = [group_label, "세금계산서구분"]
        else:
            group_keys = [group_label]

        out = (
            work.groupby(group_keys, dropna=False)
            .agg(
                건수=("_amount", "size"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                계산서금액=("_amount", "sum"),
                상세합_공급가액=("_detail_supply", "sum"),
                상세합_세액=("_detail_tax", "sum"),
                공급가액차이=("_supply_diff", "sum"),
                세액차이=("_tax_diff", "sum"),
            )
            .reset_index()
            .sort_values(sort_col, ascending=False)
        )

        out.insert(0, "순번", range(1, len(out) + 1))
        return out
    
    # 0) 제품별 계산서금액은 세금계산서 공통표에서는 기본 불가 안내
    if "제품별" in t or "품목별" in t:
        return push_notice(
            title="현재표 제품별 분석 불가",
            action="현재표 제품별 분석 불가",
            message=(
                "현재표는 세금계산서 공통표이며, 제품별 계산서금액 분석에 필요한 제품명/품목명 컬럼이 없습니다.\n\n"
                f"현재표 기준 행수: {len(df):,}건\n"
                f"현재표 주요 컬럼: {', '.join(col_names[:30])}\n\n"
                "제품별 분석은 입고명세 또는 출고명세 조회 후 실행해 주세요."
            ),
            query_summary="현재표 / 제품별 분석 불가",
            source_query=t,
        )

    # 1) 거래처별 계산서금액
    wants_vendor_amount = (
        "거래처별" in t
        and any(w in t for w in ("계산서금액", "거래금액", "금액", "공급가액", "세액", "분석", "TOP", "top", "상위"))
    )

    if wants_vendor_amount:
        if not vendor_col:
            return push_notice(
                title="현재표 거래처별 계산서금액 불가",
                action="현재표 거래처별 계산서금액 불가",
                message="현재표에는 거래처별 분석에 필요한 거래처명 컬럼이 없습니다.",
                query_summary="현재표 / 거래처별 계산서금액 불가",
                source_query=t,
            )

        wants_total_vendor = _wants_total_amount()
        split_rows = bool(tax_type_col and not wants_total_vendor)

        out = _build_group_table(
            group_col=vendor_col,
            group_label="거래처명",
            split_rows=split_rows,
        )

        if out.empty:
            return False

        if vendor_code_col and vendor_code_col in df.columns:
            # 거래처코드는 거래처명만으로 group한 뒤 대표값을 붙이기보다,
            # 동일 거래처명 중 첫 번째 코드만 참고용으로 붙인다.
            code_map = (
                pd.DataFrame({
                    "거래처명": _text_series(vendor_col),
                    "거래처코드": _text_series(vendor_code_col),
                })
                .query("거래처명 != ''")
                .drop_duplicates("거래처명")
            )
            out = out.merge(code_map, on="거래처명", how="left")
            cols = ["순번", "거래처코드", "거래처명"] + [c for c in out.columns if c not in ("순번", "거래처코드", "거래처명")]
            out = out[cols]

        split_label = "세금계산서구분 분리" if split_rows else "거래처 통합"

        if "TOP" in t or "top" in t or "상위" in t:
            out2 = out.head(top_n).copy()
            if split_rows:
                title = f"현재표 거래처별 계산서금액 TOP {top_n}"
            else:
                title = f"현재표 거래처별 계산서금액 통합 TOP {top_n}"

            query_summary = f"현재표 / 거래처별 계산서금액 TOP {top_n} / {split_label} / 전체 {len(df):,}건 기준"
            display_limit = top_n
        else:
            out2 = out
            if split_rows:
                title = "현재표 거래처별 계산서금액"
            else:
                title = "현재표 거래처별 계산서금액 통합"

            query_summary = f"현재표 / 거래처별 계산서금액 / {split_label} / 전체 {len(df):,}건 기준"
            display_limit = None


        log.info(
            "[chat.followup_table] tax doc vendor amount built source_rows=%s rows=%s amount_label=%s table_key=%s",
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

    # 2) 월별 계산서금액
    wants_month_amount = (
        "월별" in t
        and any(w in t for w in ("계산서금액", "거래금액", "금액", "공급가액", "세액", "분석", "요약"))
    )

    if wants_month_amount:
        if not date_col:
            return push_notice(
                title="현재표 월별 계산서금액 불가",
                action="현재표 월별 계산서금액 불가",
                message="현재표에는 월별 분석에 필요한 세금계산서일자 컬럼이 없습니다.",
                query_summary="현재표 / 월별 계산서금액 불가",
                source_query=t,
            )

        raw_month = _text_series(date_col).str.replace(r"\D", "", regex=True).str[:6]

        work = _base_work()
        work["월"] = raw_month.str[:4] + "-" + raw_month.str[4:6]
        work = work[work["월"].str.len() == 7].copy()

        if work.empty:
            return False

        split_rows = bool(tax_type_col and not _wants_total_amount())

        if split_rows:
            work["세금계산서구분"] = work["_type"].astype(str).str.strip().replace("", "(구분없음)")
            group_keys = ["월", "세금계산서구분"]
            sort_cols = ["월", "세금계산서구분"]
            split_label = "세금계산서구분 분리"
            title = "현재표 월별 계산서금액"
        else:
            group_keys = ["월"]
            sort_cols = ["월"]
            split_label = "통합"
            title = "현재표 월별 계산서금액 통합"

        out = (
            work.groupby(group_keys, dropna=False)
            .agg(
                건수=("_amount", "size"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                계산서금액=("_amount", "sum"),
                상세합_공급가액=("_detail_supply", "sum"),
                상세합_세액=("_detail_tax", "sum"),
                공급가액차이=("_supply_diff", "sum"),
                세액차이=("_tax_diff", "sum"),
            )
            .reset_index()
            .sort_values(sort_cols, ascending=True)
        )
        out.insert(0, "순번", range(1, len(out) + 1))


        log.info(
            "[chat.followup_table] tax doc monthly amount built source_rows=%s rows=%s table_key=%s",
            len(df),
            len(out),
            table_key,
        )

        return push_table(
            title=title,
            action=title,
            df=out,
            query_summary=f"현재표 / 월별 계산서금액 / {split_label} / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=None,
        )

    # 3) 일자별/요일별 계산서금액
    # 예:
    # - 현재표 일자별 계산서금액
    # - 현재표 일별 계산서금액 TOP 10
    # - 현재표 계산서금액이 가장 많은 일자와 요일
    # - 현재표 계산서금액이 가장 많은 요일
    wants_date_amount = (
        (
            ("일자별" in t or "날짜별" in t or "일별" in t)
            and any(
                w in t
                for w in (
                    "계산서금액",
                    "세금계산서금액",
                    "거래금액",
                    "금액",
                    "공급가액",
                    "세액",
                    "분석",
                    "요약",
                    "TOP",
                    "top",
                    "상위",
                )
            )
        )
        or any(
            w in compact
            for w in (
                "일별계산서금액",
                "일별세금계산서금액",
                "일자별계산서금액",
                "일자별세금계산서금액",
                "계산서금액이가장많은일자",
                "계산서금액이제일많은일자",
                "계산서금액최고일자",
                "계산서금액가장큰일자",
                "세금계산서금액이가장많은일자",
                "세금계산서금액이제일많은일자",
                "세금계산서금액최고일자",
                "세금계산서금액가장큰일자",
                "금액이가장많은일자",
                "금액이제일많은일자",
                "금액최고일자",
                "금액가장큰일자",
            )
        )
        or (
            "요일" in t
            and any(w in t for w in ("계산서금액", "세금계산서금액", "거래금액", "금액"))
            and any(w in t for w in ("최고", "가장", "제일", "많은", "큰"))
        )
    )

    if wants_date_amount:
        if not date_col:
            return push_notice(
                title="현재표 일자별 계산서금액 불가",
                action="현재표 일자별 계산서금액 불가",
                message="현재표에는 일자별 분석에 필요한 세금계산서일자 컬럼이 없습니다.",
                query_summary="현재표 / 일자별 계산서금액 불가",
                source_query=t,
            )

        raw_day = _text_series(date_col).str.replace(r"\D", "", regex=True).str[:8]

        work = _base_work()
        work["일자"] = raw_day.str[:4] + "-" + raw_day.str[4:6] + "-" + raw_day.str[6:8]
        work = work[work["일자"].str.len() == 10].copy()

        if work.empty:
            return False

        dt = pd.to_datetime(work["일자"], errors="coerce")
        week_map = {
            0: "월요일",
            1: "화요일",
            2: "수요일",
            3: "목요일",
            4: "금요일",
            5: "토요일",
            6: "일요일",
        }
        work["요일"] = dt.dt.dayofweek.map(week_map).fillna("(일자없음)")

        daily_plain = (
            work.groupby(["일자", "요일"], dropna=False)
            .agg(
                건수=("_amount", "size"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                계산서금액=("_amount", "sum"),
                상세합_공급가액=("_detail_supply", "sum"),
                상세합_세액=("_detail_tax", "sum"),
                공급가액차이=("_supply_diff", "sum"),
                세액차이=("_tax_diff", "sum"),
            )
            .reset_index()
            .sort_values("계산서금액", ascending=False)
        )

        weekday = (
            work.groupby("요일", dropna=False)
            .agg(
                건수=("_amount", "size"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                계산서금액=("_amount", "sum"),
                상세합_공급가액=("_detail_supply", "sum"),
                상세합_세액=("_detail_tax", "sum"),
                공급가액차이=("_supply_diff", "sum"),
                세액차이=("_tax_diff", "sum"),
            )
            .reset_index()
            .sort_values("계산서금액", ascending=False)
        )

        wants_top_day_with_weekday = (
            (
                ("일자" in t or "날짜" in t)
                and "요일" in t
                and any(w in t for w in ("최고", "가장", "제일", "많은", "큰"))
            )
            or any(
                w in compact
                for w in (
                    "계산서금액이가장많은일자",
                    "계산서금액이제일많은일자",
                    "계산서금액최고일자",
                    "계산서금액가장큰일자",
                    "세금계산서금액이가장많은일자",
                    "세금계산서금액이제일많은일자",
                    "세금계산서금액최고일자",
                    "세금계산서금액가장큰일자",
                    "금액이가장많은일자",
                    "금액이제일많은일자",
                    "금액최고일자",
                    "금액가장큰일자",
                )
            )
        )

        wants_top_weekday_only = (
            "요일" in t
            and "일자" not in t
            and "날짜" not in t
            and any(w in t for w in ("최고", "가장", "제일", "많은", "큰"))
        )

        explicit_top = bool(re.search(r"(?:TOP|top|상위)\s*(\d{1,4})", t))
        wants_first = any(w in compact for w in ("1위", "최고", "가장많은", "제일많은", "가장큰"))

        if wants_top_day_with_weekday:
            if daily_plain.empty:
                return False

            top_day = daily_plain.iloc[0]

            out = pd.DataFrame(
                [
                    {
                        "구분": "계산서금액 최고 일자",
                        "일자": str(top_day["일자"]),
                        "요일": str(top_day["요일"]),
                        "건수": top_day["건수"],
                        "공급가액": top_day["공급가액"],
                        "세액": top_day["세액"],
                        "계산서금액": top_day["계산서금액"],
                        "상세합_공급가액": top_day["상세합_공급가액"],
                        "상세합_세액": top_day["상세합_세액"],
                        "공급가액차이": top_day["공급가액차이"],
                        "세액차이": top_day["세액차이"],
                    }
                ]
            )
            out.insert(0, "순번", range(1, len(out) + 1))

            title = "현재표 계산서금액 최고 일자/요일"
            query_summary = f"현재표 / 계산서금액 최고 일자와 요일 / 전체 {len(df):,}건 기준"
            display_limit = 1

        elif wants_top_weekday_only:
            if weekday.empty:
                return False

            top_week = weekday.iloc[0]

            out = pd.DataFrame(
                [
                    {
                        "구분": "계산서금액 최고 요일",
                        "요일": str(top_week["요일"]),
                        "건수": top_week["건수"],
                        "공급가액": top_week["공급가액"],
                        "세액": top_week["세액"],
                        "계산서금액": top_week["계산서금액"],
                        "상세합_공급가액": top_week["상세합_공급가액"],
                        "상세합_세액": top_week["상세합_세액"],
                        "공급가액차이": top_week["공급가액차이"],
                        "세액차이": top_week["세액차이"],
                    }
                ]
            )
            out.insert(0, "순번", range(1, len(out) + 1))

            title = "현재표 계산서금액 최고 요일"
            query_summary = f"현재표 / 계산서금액 최고 요일 / 전체 {len(df):,}건 기준"
            display_limit = 1

        else:
            split_rows = bool(tax_type_col and not _wants_total_amount())

            if split_rows:
                work["세금계산서구분"] = work["_type"].astype(str).str.strip().replace("", "(구분없음)")
                group_keys = ["일자", "세금계산서구분"]
                sort_cols = ["일자", "세금계산서구분"]
                split_label = "세금계산서구분 분리"
                title = "현재표 일자별 계산서금액"
            else:
                group_keys = ["일자"]
                sort_cols = ["일자"]
                split_label = "통합"
                title = "현재표 일자별 계산서금액 통합"

            out = (
                work.groupby(group_keys, dropna=False)
                .agg(
                    건수=("_amount", "size"),
                    공급가액=("_supply", "sum"),
                    세액=("_tax", "sum"),
                    계산서금액=("_amount", "sum"),
                    상세합_공급가액=("_detail_supply", "sum"),
                    상세합_세액=("_detail_tax", "sum"),
                    공급가액차이=("_supply_diff", "sum"),
                    세액차이=("_tax_diff", "sum"),
                )
                .reset_index()
            )

            if explicit_top:
                out = out.sort_values("계산서금액", ascending=False).head(top_n).reset_index(drop=True)
                out.insert(0, "순번", range(1, len(out) + 1))
                if split_rows:
                    title = f"현재표 일자별 계산서금액 TOP {top_n}"
                else:
                    title = f"현재표 일자별 계산서금액 통합 TOP {top_n}"
                query_summary = f"현재표 / 일자별 계산서금액 TOP {top_n} / {split_label} / 전체 {len(df):,}건 기준"
                display_limit = top_n

            elif wants_first:
                out = out.sort_values("계산서금액", ascending=False).head(1).reset_index(drop=True)
                out.insert(0, "순번", range(1, len(out) + 1))
                if split_rows:
                    title = "현재표 계산서금액 1위 일자"
                else:
                    title = "현재표 계산서금액 1위 일자 통합"
                query_summary = f"현재표 / 계산서금액 1위 일자 / {split_label} / 전체 {len(df):,}건 기준"
                display_limit = 1

            else:
                out = out.sort_values(sort_cols, ascending=True).reset_index(drop=True)
                out.insert(0, "순번", range(1, len(out) + 1))
                query_summary = f"현재표 / 일자별 계산서금액 / {split_label} / 전체 {len(df):,}건 기준"
                display_limit = None

        log.info(
            "[chat.followup_table] tax doc daily amount built source_rows=%s rows=%s table_key=%s",
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
    
        
    # 4) 세금계산서구분별 계산서금액
    wants_type_amount = (
        ("세금계산서구분별" in t or "계산서구분별" in t or "구분별" in t)
        and any(w in t for w in ("계산서금액", "거래금액", "금액", "공급가액", "세액", "분석", "요약"))
    )

    if wants_type_amount:
        if not tax_type_col:
            return push_notice(
                title="현재표 세금계산서구분별 계산서금액 불가",
                action="현재표 세금계산서구분별 계산서금액 불가",
                message="현재표에는 세금계산서구분별 분석에 필요한 구분 컬럼이 없습니다.",
                query_summary="현재표 / 세금계산서구분별 계산서금액 불가",
                source_query=t,
            )

        out = _build_group_table(group_col=tax_type_col, group_label="세금계산서구분")
        if out.empty:
            return False

        log.info(
            "[chat.followup_table] tax doc type amount built source_rows=%s rows=%s table_key=%s",
            len(df),
            len(out),
            table_key,
        )

        return push_table(
            title="현재표 세금계산서구분별 계산서금액",
            action="현재표 세금계산서구분별 계산서금액",
            df=out,
            query_summary=f"현재표 / 세금계산서구분별 계산서금액 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
        )

    return False