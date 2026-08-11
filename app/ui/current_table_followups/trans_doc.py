# app/ui/current_table_followups/trans_doc.py
# Create 2026-06-08
# 거래명세서 공통 조회       → trans_doc

from __future__ import annotations

from typing import Any, Callable
import re

import pandas as pd

from app.ui.current_table_followups.generic import semantic_boolean_mask


def handle_trans_doc_followup(
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
    거래명세서 공통 조회 현재표 전용 후속분석.

    거래명세서 공통은 헤더 기준 표로 본다.
    - 거래처별 거래금액
    - 월별 거래금액
    - 일자별 거래금액
    - 거래명세서구분별 거래금액
    - 상세합계 불일치 목록
    - 제품별 분석은 기본적으로 불가 안내
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
        exact=("거래명세서일자", "거래일자", "일자", "Rd13_Trans_YyMmDd"),
        include_any=("거래명세서일자", "거래일자", "Trans_YyMmDd", "일자"),
        exclude_any=("등록", "수정", "발행", "전표", "마감"),
    )
    vendor_col = find_col(
        df,
        exact=("거래처명", "매입처명", "매출처명"),
        include_any=("거래처", "매입처", "매출처"),
        exclude_any=("코드", "번호", "분류", "구분"),
    )
    vendor_code_col = find_col(
        df,
        exact=("거래처코드", "매입처코드", "매출처코드", "Rd13_Ven_Cd"),
        include_any=("거래처코드", "매입처코드", "매출처코드", "Ven_Cd"),
        exclude_any=("그룹", "분류", "구분"),
    )
    trans_type_col = find_col(
        df,
        exact=("거래명세서구분명", "거래명세서구분", "Rd13_Trans_Di"),
        include_any=("거래명세서구분", "Trans_Di"),
        exclude_any=("대분류코드", "코드번호"),
    )

    supply_col = find_col(
        df,
        exact=("공급가액", "Rd13_Supply_Price"),
        include_any=("공급가액", "Supply_Price"),
        exclude_any=("상세합", "확정", "차이", "단가", "율"),
    )
    tax_col = find_col(
        df,
        exact=("세액", "부가세", "Rd13_Tax_Price"),
        include_any=("세액", "부가세", "Tax_Price"),
        exclude_any=("상세합", "확정", "차이", "단가", "율"),
    )
    total_col = find_col(
        df,
        exact=("합계금액", "거래금액", "총금액", "Rd13_Tot_Amt"),
        include_any=("합계금액", "거래금액", "총금액", "Tot_Amt"),
        exclude_any=("상세합", "단가", "율"),
    )
    dc_col = find_col(
        df,
        exact=("할인금액", "Rd13_Dc_Amt"),
        include_any=("할인금액", "Dc_Amt"),
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
    detail_match_col = find_col(
        df,
        exact=("상세합계일치",),
        include_any=("상세합계일치", "상세합계", "일치"),
        exclude_any=("금액", "공급", "세액"),
    )

    if not supply_col and not tax_col and not total_col:
        return push_notice(
            title="현재표 거래금액 분석 불가",
            action="현재표 거래금액 분석 불가",
            message=(
                "현재표는 거래명세서 공통표로 보이지만 공급가액/세액/합계금액 컬럼을 찾지 못했습니다.\n\n"
                f"현재표 기준 행수: {len(df):,}건\n"
                f"현재표 주요 컬럼: {', '.join(col_names[:40])}"
            ),
            query_summary="현재표 / 거래명세서 거래금액 분석 불가",
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
            | name.str.contains(r"^(?:합계|총계|소계|합계금액|전체|TOTAL)$", case=False, regex=True, na=False)
            | name.str.contains(r"합계\s*금액", case=False, regex=True, na=False)
        )
        return ~bad

    def _type_name(raw: pd.Series) -> pd.Series:
        s = raw.fillna("").astype(str).str.strip()
        return s.replace(
            {
                "1": "매입분",
                "3": "매출분",
                "입고": "매입분",
                "출고": "매출분",
                "매입": "매입분",
                "매출": "매출분",
            }
        ).replace("", "(구분없음)")

    supply_s = _series(supply_col)
    tax_s = _series(tax_col)
    dc_s = _series(dc_col)

    if total_col:
        amount_s = _series(total_col)
        amount_label = total_col
    else:
        amount_s = supply_s + tax_s
        amount_label = "공급가액+세액"

    detail_supply_s = _series(detail_supply_col)
    detail_tax_s = _series(detail_tax_col)

    if trans_type_col:
        type_s = _type_name(_text_series(trans_type_col))
    else:
        type_s = pd.Series(["(구분없음)"] * len(df), index=df.index, dtype="object")

    # 현재표 전체가 거래명세서 공통이더라도 후속질문에서 매입/입고, 매출/출고를 붙이면
    # 현재표 안에서 한 번 더 구분 필터를 적용한다.
    base_mask = pd.Series([True] * len(df), index=df.index)
    if any(w in compact for w in ("매입", "입고")) and not any(w in compact for w in ("매출", "출고")):
        base_mask = type_s.astype(str).str.contains("매입|입고", regex=True, na=False)
    elif any(w in compact for w in ("매출", "출고")) and not any(w in compact for w in ("매입", "입고")):
        base_mask = type_s.astype(str).str.contains("매출|출고", regex=True, na=False)

    def _base_work(group_col: str | None = None, group_label: str | None = None) -> pd.DataFrame:
        work = pd.DataFrame(
            {
                "_supply": supply_s,
                "_tax": tax_s,
                "_amount": amount_s,
                "_dc": dc_s,
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

    def _with_vendor_code(out: pd.DataFrame) -> pd.DataFrame:
        if not vendor_code_col or not vendor_col or vendor_code_col not in df.columns or vendor_col not in df.columns:
            return out
        code_map = pd.DataFrame(
            {
                "거래처명": _text_series(vendor_col),
                "거래처코드": _text_series(vendor_code_col),
            },
            index=df.index,
        )
        code_map = (
            code_map[base_mask]
            .query("거래처명 != ''")
            .drop_duplicates("거래처명")
        )
        out2 = out.merge(code_map, on="거래처명", how="left")
        cols = ["순번", "거래처코드", "거래처명"] + [
            c for c in out2.columns if c not in ("순번", "거래처코드", "거래처명")
        ]
        return out2[cols]

    def _build_group_table(
        *,
        group_col: str,
        group_label: str,
        split_type: bool = False,
        split_rows: bool = False,
    ) -> pd.DataFrame:
        work = _base_work(group_col, group_label)
        if work.empty:
            return pd.DataFrame()

        # 거래명세서 공통표는 매입분/매출분이 한 표에 섞인다.
        # 따라서 기본 거래처별 집계는 거래처 + 거래명세서구분 행 단위로 분리한다.
        # 단, 사용자가 통합/합산/총합을 명시하면 거래처 1줄 + 매입/매출 보조 컬럼 방식으로 처리한다.
        if split_rows:
            work["거래명세서구분"] = work["_type"].astype(str).str.strip().replace("", "(구분없음)")
            group_keys = [group_label, "거래명세서구분"]
        else:
            group_keys = [group_label]

        out = (
            work.groupby(group_keys, dropna=False)
            .agg(
                건수=("_amount", "size"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                거래금액=("_amount", "sum"),
                할인금액=("_dc", "sum"),
                상세합_공급가액=("_detail_supply", "sum"),
                상세합_세액=("_detail_tax", "sum"),
                공급가액차이=("_supply_diff", "sum"),
                세액차이=("_tax_diff", "sum"),
            )
            .reset_index()
        )

        if split_type and not split_rows:
            buy = (
                work[work["_type"].astype(str).str.contains("매입|입고", regex=True, na=False)]
                .groupby(group_label, dropna=False)
                .agg(매입건수=("_amount", "size"), 매입거래금액=("_amount", "sum"))
                .reset_index()
            )
            sales = (
                work[work["_type"].astype(str).str.contains("매출|출고", regex=True, na=False)]
                .groupby(group_label, dropna=False)
                .agg(매출건수=("_amount", "size"), 매출거래금액=("_amount", "sum"))
                .reset_index()
            )
            out = out.merge(buy, on=group_label, how="left").merge(sales, on=group_label, how="left")
            for c in ("매입건수", "매입거래금액", "매출건수", "매출거래금액"):
                if c in out.columns:
                    out[c] = out[c].fillna(0)

            # 분리값이 하나도 없으면 무의미한 0 컬럼은 제거한다.
            try:
                split_cols = ["매입건수", "매입거래금액", "매출건수", "매출거래금액"]
                has_split_value = any(
                    c in out.columns and float(pd.to_numeric(out[c], errors="coerce").fillna(0).abs().sum()) > 0
                    for c in split_cols
                )
                if not has_split_value:
                    out = out.drop(columns=[c for c in split_cols if c in out.columns])
            except Exception:
                pass

        out = out.sort_values("거래금액", ascending=False).reset_index(drop=True)
        out.insert(0, "순번", range(1, len(out) + 1))
        return out

    explicit_top = bool(re.search(r"(?:TOP|top|상위)\s*(\d{1,4})", t))
    wants_first = any(w in compact for w in ("1위", "최고", "가장많은", "가장큰"))

    amount_title_word = (
        "매입금액" if ("매입" in t or "입고" in t)
        else "매출금액" if ("매출" in t or "출고" in t)
        else "거래금액"
    )

    def _wants_total_time_amount() -> bool:
        """
        월별/일자별 거래금액에서 매입분/매출분을 나누지 않고
        통합 합계로 보고 싶을 때 사용하는 판단.
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
            )
        )

    # 0) 제품별 거래금액은 거래명세서 공통표에서는 기본 불가 안내
    if "제품별" in t or "품목별" in t or "상품별" in t:
        return push_notice(
            title="현재표 제품별 분석 불가",
            action="현재표 제품별 분석 불가",
            message=(
                "현재표는 거래명세서 공통표이며, 제품별 거래금액 분석에 필요한 제품명/품목명 컬럼이 없습니다.\n\n"
                f"현재표 기준 행수: {len(df):,}건\n"
                f"현재표 주요 컬럼: {', '.join(col_names[:30])}\n\n"
                "제품별 분석은 입고명세 또는 출고명세 조회 후 실행해 주세요."
            ),
            query_summary="현재표 / 거래명세서 제품별 분석 불가",
            source_query=t,
        )

    # 1) 상세합계 불일치 목록
    detail_match_requested = "상세합계" in compact and any(
        w in compact for w in ("일치", "불일치", "차이", "안맞", "맞지않")
    )
    if detail_match_requested:
        if not detail_match_col:
            return push_notice(
                title="현재표 상세합계 불일치 분석 불가",
                action="현재표 상세합계 불일치 분석 불가",
                message="현재표에는 상세합계 일치 여부를 판단할 컬럼이 없습니다.",
                query_summary="현재표 / 거래명세서 상세합계 불일치 분석 불가",
                source_query=t,
            )

        wants_mismatch = any(w in compact for w in ("불일치", "차이", "안맞", "맞지않"))
        out = df[base_mask & semantic_boolean_mask(df[detail_match_col], not wants_mismatch)].copy()

        if out.empty:
            return push_notice(
                title="현재표 상세합계 조건 결과 없음",
                action="현재표 상세합계 조건 결과 없음",
                message="현재표에서 요청한 상세합계 일치 조건에 해당하는 행이 없습니다.",
                query_summary=f"현재표 / 거래명세서 상세합계 {'불일치' if wants_mismatch else '일치'} / 0건",
                source_query=t,
                extra_meta={"execution_status": "no_data", "result_status": "no_data"},
            )

        log.info(
            "[chat.followup_table] trans doc mismatch list built source_rows=%s rows=%s match_col=%s table_key=%s",
            len(df),
            len(out),
            detail_match_col,
            table_key,
        )

        return push_table(
            title=f"현재표 거래명세서 상세합계 {'불일치' if wants_mismatch else '일치'} 목록",
            action=f"현재표 거래명세서 상세합계 {'불일치' if wants_mismatch else '일치'} 목록",
            df=out,
            query_summary=f"현재표 / 거래명세서 상세합계 {'불일치' if wants_mismatch else '일치'} 목록 / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=None,
        )

    # 2) 거래처명 상세표
    detail_m = re.search(
        r"(?:현재\s*표|현재표|현재\s*조회\s*결과|현재조회결과)?\s*"
        r"(?:거래처명|거래처|매입처명|매출처명)\s*[:=]?\s*([^\s,]+)",
        t,
    )
    if detail_m and "거래처별" not in t and any(w in compact for w in ("상세", "상세표", "목록", "내역", "표로")):
        if not vendor_col:
            return push_notice(
                title="현재표 거래처 상세표 불가",
                action="현재표 거래처 상세표 불가",
                message="현재표에는 거래처 상세표를 만들 거래처명 컬럼이 없습니다.",
                query_summary="현재표 / 거래명세서 거래처 상세표 불가",
                source_query=t,
            )
        value = str(detail_m.group(1) or "").strip()
        out = df[base_mask & _text_series(vendor_col).str.contains(value, case=False, regex=False, na=False)].copy()
        return push_table(
            title=f"현재표 거래처명 {value} 거래명세서 상세",
            action=f"현재표 거래처명 {value} 거래명세서 상세",
            df=out,
            query_summary=f"현재표 / 거래명세서 / 거래처명 {value} 상세",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=None,
        )

    # 3) 거래처별 거래금액
    has_vendor_by = ("거래처별" in t) or ("거래처별" in compact)

    wants_vendor_amount = (
        has_vendor_by
        and any(
            w in t or w in compact
            for w in (
                "거래금액",
                "입고금액",
                "매입금액",
                "매출금액",
                "출고금액",
                "금액",
                "공급가액",
                "세액",
                "분석",
                "TOP",
                "top",
                "상위",
            )
        )
    ) or (has_vendor_by and wants_first)

    if wants_vendor_amount:
        if not vendor_col:
            return push_notice(
                title="현재표 거래처별 거래금액 불가",
                action="현재표 거래처별 거래금액 불가",
                message="현재표에는 거래처별 분석에 필요한 거래처명 컬럼이 없습니다.",
                query_summary="현재표 / 거래명세서 거래처별 거래금액 불가",
                source_query=t,
            )

        wants_total_vendor = any(w in compact for w in ("통합", "합산", "총합", "총계", "전체합계", "합계만", "총거래"))
        split_rows = bool(trans_type_col and not wants_total_vendor)

        out = _build_group_table(
            group_col=vendor_col,
            group_label="거래처명",
            split_type=not split_rows,
            split_rows=split_rows,
        )
        if out.empty:
            return False
        out = _with_vendor_code(out)

        split_label = "거래명세서구분 분리" if split_rows else "거래처 통합"

        if explicit_top:
            out2 = out.head(top_n).copy()
            title = f"현재표 거래처별 거래금액 TOP {top_n}"
            query_summary = f"현재표 / 거래처별 거래금액 TOP {top_n} / {split_label} / 전체 {len(df):,}건 기준"
            display_limit = top_n
        elif wants_first:
            out2 = out.head(1).copy()
            title = "현재표 거래처별 거래금액 1위"
            query_summary = f"현재표 / 거래처별 거래금액 1위 / {split_label} / 전체 {len(df):,}건 기준"
            display_limit = 1
        else:
            out2 = out
            title = "현재표 거래처별 거래금액"
            query_summary = f"현재표 / 거래처별 거래금액 / {split_label} / 전체 {len(df):,}건 기준"
            display_limit = None

        log.info(
            "[chat.followup_table] trans doc vendor amount built source_rows=%s rows=%s amount_label=%s table_key=%s",
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

    # 4) 월별 거래금액
    wants_month_amount = (
        "월별" in t
        and any(
            w in t
            for w in (
                "거래금액",
                "매입금액",
                "매출금액",
                "입고금액",
                "출고금액",
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

    if wants_month_amount:
        if not date_col:
            return push_notice(
                title="현재표 월별 거래금액 불가",
                action="현재표 월별 거래금액 불가",
                message="현재표에는 월별 분석에 필요한 거래명세서일자 컬럼이 없습니다.",
                query_summary="현재표 / 거래명세서 월별 거래금액 불가",
                source_query=t,
            )

        work = _base_work()
        raw_month = _text_series(date_col).str.replace(r"\D", "", regex=True).str[:6]
        work["월"] = raw_month.str[:4] + "-" + raw_month.str[4:6]
        work = work[work["월"].str.len() == 7].copy()
        if work.empty:
            return push_notice(
                title="현재표 월별 거래금액 결과 없음",
                action="현재표 월별 거래금액 결과 없음",
                message="현재표에서 유효한 거래명세서월을 찾지 못했습니다.",
                query_summary="현재표 / 월별 거래금액 결과 없음 / 0건",
                source_query=t,
                extra_meta={"execution_status": "no_data", "result_status": "no_data"},
            )

        split_rows = bool(trans_type_col and not _wants_total_time_amount())

        if split_rows:
            work["거래명세서구분"] = work["_type"].astype(str).str.strip().replace("", "(구분없음)")
            group_keys = ["월", "거래명세서구분"]
            sort_cols = ["월", "거래명세서구분"]
            split_label = "거래명세서구분 분리"
        else:
            group_keys = ["월"]
            sort_cols = ["월"]
            split_label = "통합"

        out = (
            work.groupby(group_keys, dropna=False)
            .agg(
                건수=("_amount", "size"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                거래금액=("_amount", "sum"),
                할인금액=("_dc", "sum"),
                상세합_공급가액=("_detail_supply", "sum"),
                상세합_세액=("_detail_tax", "sum"),
                공급가액차이=("_supply_diff", "sum"),
                세액차이=("_tax_diff", "sum"),
            )
            .reset_index()
        )

        amount_title_word = (
            "공급가액" if "공급가액" in t
            else "매입금액" if ("매입" in t or "입고" in t)
            else "매출금액" if ("매출" in t or "출고" in t)
            else "거래금액"
        )

        if explicit_top:
            sort_metric = "공급가액" if amount_title_word == "공급가액" else "거래금액"
            out = out.sort_values(sort_metric, ascending=False).head(top_n).reset_index(drop=True)
            title = f"현재표 월별 {amount_title_word} TOP {top_n}"
            query_summary = f"현재표 / 월별 {amount_title_word} TOP {top_n} / {split_label} / 전체 {len(df):,}건 기준"
            display_limit = top_n
        else:
            out = out.sort_values(sort_cols, ascending=True).reset_index(drop=True)
            title = f"현재표 월별 {amount_title_word} 요약"
            query_summary = f"현재표 / 월별 {amount_title_word} 요약 / {split_label} / 전체 {len(df):,}건 기준"
            display_limit = None

        out.insert(0, "순번", range(1, len(out) + 1))

        log.info(
            "[chat.followup_table] trans doc monthly amount built source_rows=%s rows=%s table_key=%s",
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
        
    # 5) 일자별/요일별 거래금액
    # 예:
    # - 현재표 일자별 거래금액
    # - 현재표 거래금액이 가장 많은 일자와 요일
    # - 현재표 거래금액 최고 일자
    wants_date_amount = (
        (
            ("일자별" in t or "날짜별" in t or "일별" in t)
            and any(
                w in t
                for w in (
                    "거래금액",
                    "매입금액",
                    "매출금액",
                    "입고금액",
                    "출고금액",
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
                "일별거래금액",
                "일별매입금액",
                "일별매출금액",
                "일별입고금액",
                "일별출고금액",
                "일자별거래금액",
                "일자별매입금액",
                "일자별매출금액",
                "거래금액이가장많은일자",
                "매입금액이가장많은일자",
                "매출금액이가장많은일자",
                "거래금액최고일자",
                "매입금액최고일자",
                "매출금액최고일자",
                "금액이가장많은일자",
                "금액최고일자",
            )
        )
        or (
            "요일" in t
            and any(w in t for w in ("거래금액", "매입금액", "매출금액", "입고금액", "출고금액", "금액"))
            and any(w in t for w in ("최고", "가장", "많은", "제일", "큰"))
        )
    )

    if wants_date_amount:
        if not date_col:
            return push_notice(
                title="현재표 일자별 거래금액 불가",
                action="현재표 일자별 거래금액 불가",
                message="현재표에는 일자별 분석에 필요한 거래명세서일자 컬럼이 없습니다.",
                query_summary="현재표 / 거래명세서 일자별 거래금액 불가",
                source_query=t,
            )

        work = _base_work()
        raw_day = _text_series(date_col).str.replace(r"\D", "", regex=True).str[:8]
        work["일자"] = raw_day.str[:4] + "-" + raw_day.str[4:6] + "-" + raw_day.str[6:8]
        work = work[work["일자"].str.len() == 10].copy()
        if work.empty:
            return False

        split_rows = bool(trans_type_col and not _wants_total_time_amount())

        if split_rows:
            work["거래명세서구분"] = work["_type"].astype(str).str.strip().replace("", "(구분없음)")
            group_keys = ["일자", "거래명세서구분"]
            sort_cols = ["일자", "거래명세서구분"]
            split_label = "거래명세서구분 분리"
            title = "현재표 일자별 거래금액"
        else:
            group_keys = ["일자"]
            sort_cols = ["일자"]
            split_label = "통합"
            title = "현재표 일자별 거래금액 통합"

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

        wants_weekday_amount = (
            ("요일별" in t or "요일별" in compact)
            and any(
                w in t or w in compact
                for w in (
                    "거래금액",
                    "매입금액",
                    "매출금액",
                    "입고금액",
                    "출고금액",
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
            and not any(w in compact for w in ("최고요일", "가장많은요일"))
        )

        asks_best_day_week = (
            any(
                w in compact
                for w in (
                    "거래금액이가장많은일자",
                    "매입금액이가장많은일자",
                    "매출금액이가장많은일자",
                    "입고금액이가장많은일자",
                    "출고금액이가장많은일자",
                    "거래금액최고일자",
                    "매입금액최고일자",
                    "매출금액최고일자",
                    "입고금액최고일자",
                    "출고금액최고일자",
                    "금액이가장많은일자",
                    "금액최고일자",
                )
            )
            or (
                "요일" in t
                and any(w in t for w in ("거래금액", "매입금액", "매출금액", "입고금액", "출고금액", "금액"))
                and any(w in t for w in ("최고", "가장", "많은", "제일", "큰"))
                and not wants_weekday_amount
            )
        )

        asks_best_weekday_only = (
            any(
                w in compact
                for w in (
                    "거래금액최고요일",
                    "매입금액최고요일",
                    "매출금액최고요일",
                    "입고금액최고요일",
                    "출고금액최고요일",
                    "최고요일",
                    "가장많은요일",
                )
            )
            or (
                "요일" in t
                and "일자" not in t
                and any(w in t for w in ("거래금액", "매입금액", "매출금액", "입고금액", "출고금액", "금액"))
                and any(w in t for w in ("최고", "가장", "많은", "제일", "큰"))
            )
        )

        if split_rows:
            work["거래명세서구분"] = work["_type"].astype(str).str.strip().replace("", "(구분없음)")

        def _agg_by(keys: list[str]) -> pd.DataFrame:
            return (
                work.groupby(keys, dropna=False)
                .agg(
                    건수=("_amount", "size"),
                    공급가액=("_supply", "sum"),
                    세액=("_tax", "sum"),
                    거래금액=("_amount", "sum"),
                    할인금액=("_dc", "sum"),
                    상세합_공급가액=("_detail_supply", "sum"),
                    상세합_세액=("_detail_tax", "sum"),
                    공급가액차이=("_supply_diff", "sum"),
                    세액차이=("_tax_diff", "sum"),
                )
                .reset_index()
            )

        if wants_weekday_amount:
            group_keys = ["요일"]
            if split_rows:
                group_keys.append("거래명세서구분")

            out = _agg_by(group_keys).sort_values("거래금액", ascending=False).reset_index(drop=True)
            out.insert(0, "순번", range(1, len(out) + 1))

            if explicit_top:
                out = out.head(top_n).copy()
                title = f"현재표 요일별 거래금액 TOP {top_n}"
                query_summary = f"현재표 / 요일별 거래금액 TOP {top_n} / {split_label} / 전체 {len(df):,}건 기준"
                display_limit = top_n
            else:
                title = "현재표 요일별 거래금액" if split_rows else "현재표 요일별 거래금액 통합"
                query_summary = f"현재표 / 요일별 거래금액 / {split_label} / 전체 {len(df):,}건 기준"
                display_limit = None

        else:
            group_keys = ["일자", "요일"]
            if split_rows:
                group_keys.append("거래명세서구분")

            daily = _agg_by(group_keys).sort_values("거래금액", ascending=False).reset_index(drop=True)

            weekday_keys = ["요일"]
            if split_rows:
                weekday_keys.append("거래명세서구분")
            weekday = _agg_by(weekday_keys).sort_values("거래금액", ascending=False).reset_index(drop=True)

            if asks_best_day_week:
                top_day = daily.head(1).copy()
                row = {
                    "구분": "거래금액 최고 일자",
                    "일자": str(top_day.iloc[0]["일자"]),
                    "요일": str(top_day.iloc[0]["요일"]),
                    "건수": top_day.iloc[0]["건수"],
                    "공급가액": top_day.iloc[0]["공급가액"],
                    "세액": top_day.iloc[0]["세액"],
                    "거래금액": top_day.iloc[0]["거래금액"],
                    "할인금액": top_day.iloc[0]["할인금액"],
                    "상세합_공급가액": top_day.iloc[0]["상세합_공급가액"],
                    "상세합_세액": top_day.iloc[0]["상세합_세액"],
                    "공급가액차이": top_day.iloc[0]["공급가액차이"],
                    "세액차이": top_day.iloc[0]["세액차이"],
                }
                if "거래명세서구분" in top_day.columns:
                    row["거래명세서구분"] = str(top_day.iloc[0]["거래명세서구분"])

                out = pd.DataFrame([row])
                out.insert(0, "순번", range(1, len(out) + 1))
                title = "현재표 거래금액 최고 일자"
                query_summary = f"현재표 / 거래금액 최고 일자 / {split_label} / 전체 {len(df):,}건 기준"
                display_limit = 1

            elif asks_best_weekday_only:
                top_week = weekday.head(1).copy()
                row = {
                    "구분": "거래금액 최고 요일",
                    "요일": str(top_week.iloc[0]["요일"]),
                    "건수": top_week.iloc[0]["건수"],
                    "공급가액": top_week.iloc[0]["공급가액"],
                    "세액": top_week.iloc[0]["세액"],
                    "거래금액": top_week.iloc[0]["거래금액"],
                    "할인금액": top_week.iloc[0]["할인금액"],
                    "상세합_공급가액": top_week.iloc[0]["상세합_공급가액"],
                    "상세합_세액": top_week.iloc[0]["상세합_세액"],
                    "공급가액차이": top_week.iloc[0]["공급가액차이"],
                    "세액차이": top_week.iloc[0]["세액차이"],
                }
                if "거래명세서구분" in top_week.columns:
                    row["거래명세서구분"] = str(top_week.iloc[0]["거래명세서구분"])

                out = pd.DataFrame([row])
                out.insert(0, "순번", range(1, len(out) + 1))
                title = "현재표 거래금액 최고 요일"
                query_summary = f"현재표 / 거래금액 최고 요일 / {split_label} / 전체 {len(df):,}건 기준"
                display_limit = 1

            else:
                out = daily.sort_values(group_keys, ascending=True).reset_index(drop=True)
                out.insert(0, "순번", range(1, len(out) + 1))

                if explicit_top:
                    out = daily.head(top_n).copy()
                    out.insert(0, "순번", range(1, len(out) + 1))
                    title = f"현재표 일자별 거래금액 TOP {top_n}"
                    query_summary = f"현재표 / 일자별 거래금액 TOP {top_n} / {split_label} / 전체 {len(df):,}건 기준"
                    display_limit = top_n
                else:
                    title = "현재표 일자별 거래금액" if split_rows else "현재표 일자별 거래금액 통합"
                    query_summary = f"현재표 / 일자별 거래금액 / {split_label} / 전체 {len(df):,}건 기준"
                    display_limit = None

                    
        log.info(
            "[chat.followup_table] trans doc daily amount built source_rows=%s rows=%s table_key=%s",
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
            
    # 6) 거래명세서구분별 거래금액
    wants_type_amount = (
        ("거래명세서구분별" in t or "명세서구분별" in t or "구분별" in t)
        and any(w in t for w in ("거래금액", "금액", "공급가액", "세액", "분석", "요약"))
    )

    if wants_type_amount:
        if not trans_type_col:
            return push_notice(
                title=f"현재표 거래명세서구분별 {amount_title_word} 불가",
                action=f"현재표 거래명세서구분별 {amount_title_word} 불가",
                message="현재표에는 거래명세서구분별 분석에 필요한 구분 컬럼이 없습니다.",
                query_summary=f"현재표 / 거래명세서구분별 {amount_title_word} 불가",
                source_query=t,
            )

        work = _base_work()
        work["거래명세서구분"] = work["_type"].astype(str).str.strip().replace("", "(구분없음)")
        out = (
            work.groupby("거래명세서구분", dropna=False)
            .agg(
                건수=("_amount", "size"),
                공급가액=("_supply", "sum"),
                세액=("_tax", "sum"),
                거래금액=("_amount", "sum"),
                할인금액=("_dc", "sum"),
                상세합_공급가액=("_detail_supply", "sum"),
                상세합_세액=("_detail_tax", "sum"),
                공급가액차이=("_supply_diff", "sum"),
                세액차이=("_tax_diff", "sum"),
            )
            .reset_index()
            .sort_values("거래금액", ascending=False)
        )
        out.insert(0, "순번", range(1, len(out) + 1))

        return push_table(
            title=f"현재표 거래명세서구분별 {amount_title_word}",
            action=f"현재표 거래명세서구분별 {amount_title_word}",
            df=out,
            query_summary=f"현재표 / 거래명세서구분별 {amount_title_word} / 전체 {len(df):,}건 기준",
            source_query=t,
            source_table_key=table_key,
            source_rows=len(df),
            display_limit=None,
        )

    return False
