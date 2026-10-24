"""Bounded interpretation material computed from the in-memory source only."""
from __future__ import annotations

import json
import hashlib
import math
import marshal
import re
from numbers import Number

import pandas as pd

from app.ui.sims_analysis_profiles import (
    build_sims_analysis_profile,
    sanitize_sims_llm_dataframe,
    sanitize_llm_text,
)


_CURRENT_TABLE_INTERNAL_KEY_LABELS = {
    "top_group_share_pct": "상위 1개 그룹 점유율",
    "top5_group_share_pct": "상위 5개 그룹 점유율",
    "row_share_pct": "행 구성비",
    "valid_count": "유효 값 건수",
    "missing_or_invalid_count": "결측 또는 유효하지 않은 값 건수",
    "zero_count": "0 값 건수",
    "negative_count": "음수 값 건수",
    "numeric_metrics": "수치 지표",
    "unique_counts": "고유값 수",
    "group_distribution": "그룹 분포",
    "top_groups": "상위 그룹",
    "bottom_groups": "하위 그룹",
    "table_records": "표 내용",
    "representative_records": "참고 항목",
}


def sanitize_current_table_analysis_output(text: str, *, group_label: str = "") -> str:
    """Keep implementation-only JSON field names out of current-table answers."""
    label = str(group_label or "").strip()
    labels = dict(_CURRENT_TABLE_INTERNAL_KEY_LABELS)
    if label:
        labels["top_group_share_pct"] = f"상위 1개 {label} 점유율"
        labels["top5_group_share_pct"] = f"상위 5개 {label} 점유율"

    def replace(match: re.Match) -> str:
        return labels.get(match.group(1), "분석 항목")

    # Current-table analysis has no user-facing need for snake_case terms.
    # This is deliberately scoped to its final answer, not shared chat text.
    return re.sub(r"`?([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`?", replace, str(text or ""))


_CURRENT_TABLE_INTERNAL_KEY_LABELS = {
    "top_group_share_pct": "상위 1개 그룹 점유율",
    "top5_group_share_pct": "상위 5개 그룹 점유율",
    "row_share_pct": "행 구성비",
    "valid_count": "유효 값 건수",
    "missing_or_invalid_count": "결측 또는 유효하지 않은 값 건수",
    "zero_count": "0 값 건수",
    "negative_count": "음수 값 건수",
    "numeric_metrics": "수치 지표",
    "unique_counts": "고유값 수",
    "group_distribution": "그룹 분포",
    "top_groups": "상위 그룹",
    "bottom_groups": "하위 그룹",
    "table_records": "표 내용",
    "representative_records": "참고 항목",
}


def sanitize_current_table_analysis_output(text: str, *, group_label: str = "") -> str:
    """Keep implementation-only JSON field names out of current-table answers."""
    label = str(group_label or "").strip()
    labels = dict(_CURRENT_TABLE_INTERNAL_KEY_LABELS)
    if label:
        labels["top_group_share_pct"] = f"상위 1개 {label} 점유율"
        labels["top5_group_share_pct"] = f"상위 5개 {label} 점유율"

    def replace(match: re.Match) -> str:
        return labels.get(match.group(1), "분석 항목")

    # Current-table analysis has no user-facing need for snake_case terms.
    # This is deliberately scoped to its final answer, not shared chat text.
    return re.sub(r"`?([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`?", replace, str(text or ""))


def _build_operational_findings(safe: pd.DataFrame, action: str) -> dict:
    """Small, whole-source facts for the three receipt/shipment analysis views."""
    expected = "입고예정" in action
    detail = "입고명세" in action or "출고명세" in action
    if not (expected or detail):
        return {}

    def numeric(column: str) -> pd.Series:
        if column not in safe:
            return pd.Series(float("nan"), index=safe.index)
        return pd.to_numeric(safe[column].astype("string").str.replace(",", "", regex=False),
                             errors="coerce")

    def clean_number(value: float) -> int | float:
        value = float(value)
        return int(value) if value.is_integer() else round(value, 2)

    qty_column = "미입고수량" if expected else "수량"
    quantities = numeric(qty_column)
    positive = quantities.where(quantities.gt(0), 0).fillna(0)
    positive_total = float(positive.sum())

    def top(column: str) -> list[dict]:
        if column not in safe or positive_total <= 0:
            return []
        names = safe[column].astype("string").fillna("").str.strip().replace("", "미지정")
        grouped = pd.DataFrame({"name": names, "quantity": positive}).groupby(
            "name", dropna=False)["quantity"].sum()
        grouped = grouped[grouped.gt(0)].sort_values(ascending=False, kind="stable")
        return [{"name": sanitize_llm_text(name, label=column).strip()[:100],
                 "quantity": clean_number(value),
                 "share_pct": round(float(value) / positive_total * 100, 2)}
                for name, value in grouped.head(3).items()]

    findings = {
        "basis": "전체 확보 행의 양수 수량; 음수 거래는 집중도 분모에서 제외",
        "quantity_column": qty_column,
        "positive_quantity_total": clean_number(positive_total),
        "vendor_top": top("발주거래처명" if expected else "거래처명"),
        "product_top": top("제품명" if "제품명" in safe else "품목명"),
    }
    if expected:
        findings["pending_quantity"] = clean_number(quantities.sum()) if quantities.notna().any() else None
        findings["due_date_top"] = top("납기일자")
        if "납기일자" in safe:
            findings["due_date_missing_rows"] = int(
                safe["납기일자"].astype("string").fillna("").str.strip().eq("").sum())
        return findings

    findings["negative_quantity_rows"] = int(quantities.lt(0).sum())
    raw, final = numeric("공급가액"), numeric("확정공급가액")
    findings["negative_supply_rows"] = int(raw.lt(0).sum()) if "공급가액" in safe else None
    findings["negative_final_supply_rows"] = int(final.lt(0).sum()) if "확정공급가액" in safe else None
    if "공급가액" in safe and "확정공급가액" in safe:
        comparable = raw.notna() & final.notna()
        difference = (final - raw).where(comparable)
        findings["supply_comparable_rows"] = int(comparable.sum())
        findings["supply_difference_rows"] = int(difference.abs().gt(1e-6).sum())
        findings["supply_difference_sum"] = clean_number(difference.sum())

    def match_counts(column: str) -> dict | None:
        if column not in safe:
            return None
        values = safe[column].astype("string").fillna("").str.strip().str.upper()
        yes, no = int(values.eq("Y").sum()), int(values.eq("N").sum())
        return {"Y": yes, "N": no, "unknown": len(values) - yes - no}

    findings["document_match"] = match_counts("거래명세서금액일치")
    findings["tax_invoice_match"] = match_counts("세금계산서금액일치")
    findings["tax_invoice_comparison_available"] = bool(
        findings["tax_invoice_match"] and
        (findings["tax_invoice_match"]["Y"] or findings["tax_invoice_match"]["N"]))
    return findings


def build_whole_table_facts(df: pd.DataFrame, *, action: str, query: str,
                            group_column: str = "", complete_groups: bool = False) -> dict:
    profile = build_sims_analysis_profile(action, columns=list(df.columns))
    safe = sanitize_sims_llm_dataframe(df, profile)
    safe = safe.loc[:, ~safe.columns.duplicated()]
    if group_column not in safe.columns:
        group_column = ""

    def text(value, label=""):
        return sanitize_llm_text(value, label=label)[:100]

    def records(frame):
        return [{str(c): (None if pd.isna(v) else (number(v) if isinstance(v, Number) else text(v, c)))
                 for c, v in row.items()} for row in frame.to_dict("records")]

    def number(value):
        return float(value) if pd.notna(value) and math.isfinite(float(value)) else None

    # Identifiers, dates, prices and rates must not acquire additive semantics.
    candidates = [c for c in safe.columns
                  if re.search(r"수량|금액|매출|공급가액|세액|건수|횟수|발생수|단가|비율|률|율", str(c))
                  and not re.search(r"코드|번호|순번|일자|날짜", str(c))]
    focus = profile.get("analysis_focus") or []
    def metric_priority(column):
        label = str(column)
        if action == "입고예정조회" and label in {"발주수량", "입고수량", "미입고수량"}:
            return ({"발주수량": -3, "입고수량": -2, "미입고수량": -1}[label], 0, 0)
        if "매출" in action and "추세" in action:
            if label == "총매출액":
                return (-2, 0, 0)
            if label == "총출고수량":
                return (-1, 0, 0)
        relevant = any(label in f for f in focus)
        if profile["profile_id"] == "stock_risk":
            relevant = relevant or bool(re.search(r"재고|부족", label))
        if "예상" in group_column or "예상" in query:
            relevant = relevant or "예상" in label
        return (label not in query, not relevant,
                bool(re.search(r"단가|비율|률|율|평균", label)))
    candidates.sort(key=metric_priority)
    selected = candidates[:8]
    groups = (safe[group_column].astype("string").fillna("").str.strip()
              .replace("", "미지정")) if group_column else None
    result = {
        "screen_purpose": f"{action}: 현재 표에 실제 존재하는 수치와 분포 해석 (위험·원인 판단은 관련 컬럼이 있을 때만 가능)",
        "row_count": len(safe), "group_column": group_column,
        "numeric_metrics": {}, "unique_counts": {},
        "selected_metrics": list(map(str, selected)),
        "omitted_metric_columns": list(map(str, candidates[8:])),
        "interpretation_limits": ["상관이나 차이는 원인으로 단정하지 않음",
                                  "수량은 서로 다른 포장단위일 수 있음",
                                  "상하위 목록은 전체 그룹 중 일부이며 전체 분포는 집계값 기준"],
    }
    result["field_missing_counts"] = {
        str(column): int(missing.sum())
        for column in safe.columns
        for missing in [(safe[column].isna() |
                         safe[column].astype("string").str.strip().fillna("").eq(""))]
        if missing.any()
    }
    operational_findings = _build_operational_findings(safe, action)
    if operational_findings:
        result["operational_findings"] = operational_findings
    if re.sub(r"\s+", "", action) == "발주계산" and len(safe) == 1:
        decision_columns = (
            "제품코드", "제품명", "적용 필요예정수량", "재고수량", "입고예정수량",
            "계산 발주수량", "안전재고 기준수량", "발주trigger", "발주단위",
            "발주단위 근거", "수량조정정책", "수량조정단위", "수량정수화",
            "최소수량 적용", "기본 조정단위 적용", "고가 조정단위 적용",
            "추세판정", "추천 발주수량", "실제 발주수량",
            "추천대비수정수량", "발주사유/계산근거", "계산상태",
        )
        row = safe.iloc[0]
        result["order_decision_facts"] = {
            column: (None if pd.isna(row[column]) else sanitize_llm_text(str(row[column]), label=column)[:500])
            for column in decision_columns if column in safe.columns
        }
        result["order_registration_status"] = "추천 및 사용자 입력 수량이며 ERP 발주는 등록되지 않음"
    for c in safe.columns:
        if re.search(r"제품명|제품코드|품목명|제조사|거래처|등급|판정", str(c)):
            result["unique_counts"][str(c)] = int(safe[c].replace("", pd.NA).nunique())
    if groups is not None:
        counts = groups.value_counts(dropna=False)
        result["group_count"] = len(counts)
        result["group_rows_total"] = int(counts.sum())
        result["group_consistency_pass"] = result["group_rows_total"] == len(safe)
        result["group_distribution"] = [
            {"group": text(k, group_column), "rows": int(v), "row_share_pct": float(v / len(safe) * 100)}
            for k, v in counts.head(20).items()]
        result["group_distribution_omitted_count"] = max(0, len(counts) - 20)
    example_indices = []
    for c in selected:
        values = pd.to_numeric(safe[c].astype("string").str.replace(",", "", regex=False), errors="coerce")
        values = values.where(values.map(lambda v: pd.isna(v) or math.isfinite(float(v))))
        additive = not bool(re.search(r"단가|비율|률|율|평균", str(c)))
        stats = {
            "valid_count": int(values.notna().sum()), "missing_or_invalid_count": int(values.isna().sum()),
            "zero_count": int(values.eq(0).sum()), "negative_count": int(values.lt(0).sum()),
            "mean": number(values.mean()), "min": number(values.min()), "max": number(values.max()),
            "aggregation": "sum" if additive else "mean",
        }
        if additive:
            stats["sum"] = number(values.sum(min_count=1))
        if groups is not None:
            work = pd.DataFrame({"group": groups.to_numpy(), "value": values.to_numpy()})
            aggregated = work.groupby("group", dropna=False)["value"].agg(
                rows="size", valid_count="count", sum=lambda s: s.sum(min_count=1), mean="mean",
                negative_count=lambda s: int(s.lt(0).sum()), zero_count=lambda s: int(s.eq(0).sum()))
            if additive:
                stats["group_sum_total"] = number(aggregated["sum"].sum(min_count=1))
                stats["group_sum_matches_total"] = (
                    stats["sum"] is None and stats["group_sum_total"] is None
                ) or (
                    stats["sum"] is not None and stats["group_sum_total"] is not None
                    and math.isclose(stats["sum"], stats["group_sum_total"], rel_tol=1e-9, abs_tol=1e-6)
                )
                result["group_consistency_pass"] &= stats["group_sum_matches_total"]
            rank_col = "sum" if additive else "mean"
            ordered = aggregated.dropna(subset=[rank_col]).sort_values(rank_col, ascending=False, kind="stable")
            def group_records(frame):
                return [{"group": text(k, group_column), "value": number(row[rank_col]),
                         "rows": int(row["rows"]), "valid_count": int(row["valid_count"]),
                         "negative_count": int(row["negative_count"]), "zero_count": int(row["zero_count"])}
                        for k, row in frame.iterrows()]
            stats["top_groups"] = group_records(ordered.head(5))
            stats["bottom_groups"] = group_records(ordered.tail(5).iloc[::-1])
            if complete_groups and len(aggregated) <= 12:
                stats["group_values"] = group_records(aggregated.loc[counts.index])
                stats["group_values_complete"] = True
            if additive and values.notna().any() and values.dropna().ge(0).all() and values.sum() > 0:
                stats["top_group_share_pct"] = number(ordered[rank_col].head(1).sum() / values.sum() * 100)
                stats["top5_group_share_pct"] = number(ordered[rank_col].head(5).sum() / values.sum() * 100)
            else:
                stats["concentration_unavailable_reason"] = "비가산 지표, 음수 포함 또는 합계 0"
        result["numeric_metrics"][str(c)] = stats
        if values.notna().any():
            example_indices.extend(values.nlargest(1).index.tolist())
            example_indices.extend(values.nsmallest(1).index.tolist())
        example_indices.extend(values[values.isna() | values.lt(0)].head(1).index.tolist())
    columns = list(dict.fromkeys(([group_column] if group_column else []) +
                               [c for c in safe.columns if re.search(r"제품명|품목명|등급|판정", str(c))] + selected))[:12]
    if len(safe) <= 20 and len(safe.columns) <= 12:
        result["table_records"] = records(safe)
        result["table_records_complete"] = True
    else:
        indices = list(dict.fromkeys(example_indices))[:12]
        result["representative_records"] = records(safe.loc[safe.index.isin(indices), columns].head(12))
        result["table_records_complete"] = False
    return result


def build_verified_group_table(facts: dict, *, scope: str = "", display_rows: int = 0) -> str:
    """Render complete, reconciled additive groups without asking the model to do arithmetic."""
    if not isinstance(facts, dict) or not facts.get("group_column"):
        return ""
    if facts.get("group_consistency_pass") is False:
        raise ValueError("group_totals_mismatch")
    distribution = facts.get("group_distribution") or []
    if (not distribution or facts.get("group_distribution_omitted_count", 0)
            or len(distribution) != facts.get("group_count")):
        return ""
    total_rows = int(facts.get("row_count") or 0)
    if total_rows <= 0 or sum(int(item["rows"]) for item in distribution) != total_rows:
        raise ValueError("group_totals_mismatch")

    metrics = []
    for name, stats in (facts.get("numeric_metrics") or {}).items():
        if (stats.get("aggregation") != "sum" or not stats.get("group_values_complete")
                or stats.get("group_sum_matches_total") is False or stats.get("sum") is None
                or float(stats["sum"]) <= 0):
            continue
        values = {item["group"]: item for item in stats.get("group_values") or []}
        if set(values) != {item["group"] for item in distribution}:
            raise ValueError("group_totals_mismatch")
        if any(int(values[item["group"]]["rows"]) != int(item["rows"]) for item in distribution):
            raise ValueError("group_totals_mismatch")
        if any(item["value"] is not None and float(item["value"]) < 0 for item in values.values()):
            continue
        summed = math.fsum(float(item["value"] or 0) for item in values.values())
        if not math.isclose(summed, float(stats["sum"]), rel_tol=1e-9, abs_tol=1e-6):
            raise ValueError("group_totals_mismatch")
        metrics.append((str(name), stats, values))
        if len(metrics) == 2:
            break
    if not metrics:
        return ""

    def fmt(value: float | int | None) -> str:
        if value is None:
            return "자료없음"
        return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"

    def pct(value: float, total: float) -> str:
        return f"{value / total * 100:.2f}%" if total else "-"

    headings = [str(facts["group_column"]), "건수", "건수 비중"]
    for name, _, _ in metrics:
        headings.extend((name, f"{name} 비중"))
    limited_scope = facts.get("analysis_scope") in {"display_only", "partial_source"}
    lines = ["**검증된 확보 자료 집계**" if limited_scope else "**검증된 전체 집계**"]
    if scope:
        lines.append(f"조회조건: {scope}")
    if limited_scope:
        lines.append(f"확보된 {total_rows:,}건 기준 (전체 조회 합계 미검증)")
    elif display_rows and display_rows < total_rows:
        lines.append(f"전체 {total_rows:,}건 기준 (화면 표시 {display_rows:,}건)")
    lines.extend(("", "| " + " | ".join(headings) + " |",
                  "| " + " | ".join("---" for _ in headings) + " |"))

    def add_row(label: str, rows: int, values: list[float | None]) -> None:
        cells = [label.replace("|", "\\|"), f"{rows:,}", pct(rows, total_rows)]
        for value, (_, stats, _) in zip(values, metrics):
            cells.extend((fmt(value), pct(float(value), float(stats["sum"])) if value is not None else "-"))
        lines.append("| " + " | ".join(cells) + " |")

    if facts["group_column"] == "출고빈도등급":
        grade_order = {grade: index for index, grade in enumerate("ABCDEFX")}
        distribution = sorted(distribution, key=lambda item: grade_order.get(item["group"], 7))
    for item in distribution:
        label = item["group"]
        add_row(label, int(item["rows"]), [values[label]["value"] for _, _, values in metrics])
    optional = {item["group"]: item for item in distribution}
    missing_names = [name for name in optional if "자료부족" in name.replace(" ", "")]
    if facts["group_column"] == "출고빈도등급" and {"F", "X"}.issubset(optional) and len(missing_names) == 1:
        names = ("F", "X", missing_names[0])
        add_row("F·X·자료부족 소계", sum(int(optional[name]["rows"]) for name in names),
                [sum(float(values[name]["value"] or 0) for name in names) for _, _, values in metrics])
    add_row("전체", total_rows, [float(stats["sum"]) for _, stats, _ in metrics])
    for name, stats, _ in metrics:
        if stats.get("zero_count") or stats.get("missing_or_invalid_count"):
            lines.append(f"{name}: 실제 0값 {int(stats.get('zero_count') or 0):,}건, "
                         f"결측·비유효 {int(stats.get('missing_or_invalid_count') or 0):,}건")
    return "\n".join(lines)


def build_verified_whole_table(facts: dict, *, scope: str = "", display_rows: int = 0) -> str:
    """Render only additive whole-source facts when a reconciled group table is unavailable."""
    if not isinstance(facts, dict) or facts.get("group_consistency_pass") is False:
        raise ValueError("group_totals_mismatch")
    rows = int(facts.get("row_count") or 0)
    if rows <= 0:
        return ""
    limited = facts.get("analysis_scope") in {"display_only", "partial_source"}
    lines = ["**검증된 확보 자료 집계**" if limited else "**검증된 전체 집계**"]
    if scope:
        lines.append(f"조회조건: {scope}")
    if limited:
        lines.append(f"확보된 {rows:,}건 기준이며 전체 조회의 합계는 검증되지 않았습니다.")
    elif display_rows and display_rows < rows:
        lines.append(f"전체 {rows:,}건 기준 (화면 표시 {display_rows:,}건)")
    lines.extend(("", "| 지표 | 검증값 |", "| --- | ---: |", f"| 건수 | {rows:,} |"))
    for name in facts.get("selected_metrics") or list(facts.get("numeric_metrics") or {}):
        stats = (facts.get("numeric_metrics") or {}).get(name) or {}
        value = stats.get("sum")
        if (stats.get("aggregation") != "sum" or value is None
                or stats.get("group_sum_matches_total") is False):
            continue
        shown = f"{float(value):,.0f}" if float(value).is_integer() else f"{float(value):,.2f}"
        lines.append(f"| {str(name).replace('|', '\\|')} | {shown} |")
        if len(lines) >= 12:
            break
    if facts.get("group_column"):
        lines.append("\n그룹별 수치의 완전한 검증표가 없어 그룹별 합계·순위는 제시하지 않습니다.")
    return "\n".join(lines)


def _mentioned_metric_names(names, text: str) -> list[str]:
    compact = re.sub(r"\s+", "", text or "")
    matched = [name for name in names if re.sub(r"\s+", "", name) in compact]
    for phrase, label in (("매출합계", "총매출액"), ("매출액합계", "총매출액"),
                          ("매출세액", "총매출세액")):
        if phrase in compact and not any(phrase in re.sub(r"\s+", "", name) for name in matched):
            if label in names:
                matched.append(label)
    return matched


def _verified_interpretation_sentence(sentence: str, facts: dict | None) -> bool:
    """Check aggregate claims against the grouped metric they describe."""
    findings = (facts or {}).get("operational_findings") or {}
    if re.search(r"세금계산서\s*비교값\s*없", sentence):
        return findings.get("tax_invoice_match") is not None and not findings.get(
            "tax_invoice_comparison_available")
    if re.search(r"비어\s*있|공란|누락|결측|미입력|미기재|값이\s*없", sentence):
        missing = (facts or {}).get("field_missing_counts") or {}
        mentioned = [name for name in missing if
                     re.sub(r"\s+", "", name) in re.sub(r"\s+", "", sentence)]
        counts = re.findall(r"(?<!\d)(\d[\d,]*)\s*건", sentence)
        if (len(mentioned) != 1 or len(counts) != 1 or
                int(counts[0].replace(",", "")) != missing[mentioned[0]]):
            return False
        if ((facts or {}).get("analysis_scope") in {"display_only", "partial_source"}
                and not re.search(r"확보|표시", sentence)):
            return False
        return True
    if (re.search(r"금액|공급가액|수량", sentence) and re.search(r"차이|음수", sentence)
            and re.search(r"때문|원인(?:이|은|으로)|기인|가능성|추정|보입니다|수\s*있습니다|시사", sentence)
            and not re.search(r"원인[^.!?]*(?:원전표|확인|대조|판단하지)", sentence)):
        return False
    if (findings.get("supply_difference_rows") and re.search(r"^(?:이는|따라서)", sentence.strip())
            and re.search(r"시사|가능성|추정|때문|원인|보입니다", sentence)):
        return False
    if (findings and re.search(r"매입처|거래처|품목|제품", sentence)
            and re.search(r"집중|편중|의존|상위|가장\s*많", sentence)
            and re.search(r"높|많|차지|나타|보입|집중|편중|의존", sentence)
            and not re.search(r"확인|대조|검토|점검", sentence)):
        return False
    if ("세금계산서" in sentence and
            re.search(r"불일치|일치|차이|비교", sentence) and
            not re.search(r"비교값\s*없|자료\s*없|확인\s*불가|판정\s*불가", sentence) and
            not findings.get("tax_invoice_comparison_available")):
        return False
    if re.search(r"거래명세서[^.!?]*불일치", sentence):
        if not (findings.get("document_match") or {}).get("N"):
            return False
    if (re.search(r"(?:금액|수량|단가)[^.!?]*(?:차이|음수|불일치)", sentence)
            and re.search(r"(?:반품|조정|마감|세무|할인|정정)[^.!?]*(?:원인|때문|가능성|추정|판단|발생|처리)", sentence)):
        return False
    if re.search(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", sentence):
        return False
    if (re.search(r"반품|조정|입력\s*오류", sentence)
            and re.search(r"원인|때문|가능성|추정|판단|보입|의미|발생", sentence)):
        return False
    if (not (facts or {}).get("table_records_complete")
            and (re.search(r"(?:대부분|다수의|모든)\s*(?:품목|제품|데이터)", sentence)
                 or re.search(r"품목명에\s*.+\s*등\b", sentence))):
        return False
    if (re.search(r"(?:Top|상위)\s*\d+", sentence, flags=re.IGNORECASE)
            and not re.search(r"\d[\d,]*(?:\.\d+)?\s*(?:건|개|원|%)", sentence)):
        return False
    claims = list(re.finditer(
        r"(?<!\d)(\d[\d,]*(?:\.\d+)?)\s*(%|퍼센트|건|명|개(?!월|년)|억\s*원|만\s*원|원)",
        sentence,
    ))
    leader_claim = any(term in sentence for term in ("가장 높", "가장 많", "최대", "최고", "1위"))
    if not claims and not leader_claim:
        return True
    if not isinstance(facts, dict) or facts.get("group_consistency_pass") is False:
        return False
    distribution = facts.get("group_distribution") or []
    metrics = {
        name: stats for name, stats in (facts.get("numeric_metrics") or {}).items()
        if stats.get("aggregation") == "sum" and stats.get("sum") is not None
        and stats.get("group_sum_matches_total") is not False
    }
    groups = {str(item["group"]): item for item in distribution}
    row_total = int(facts.get("row_count") or 0)
    if row_total <= 0:
        return False
    distribution_complete = bool(distribution and not facts.get("group_distribution_omitted_count")
                                 and len(distribution) == facts.get("group_count")
                                 and sum(int(item["rows"]) for item in distribution) == row_total)
    complete_metrics = {
        name: stats for name, stats in metrics.items()
        if distribution_complete and stats.get("group_values_complete")
        and stats.get("group_sum_matches_total") is True
    }
    group_metrics = {
        name: {str(item["group"]): item["value"] for item in stats.get("group_values") or []}
        for name, stats in complete_metrics.items()
    }
    if any(set(values) != set(groups) for values in group_metrics.values()):
        return False

    grade_mentions = []
    if facts.get("group_column") == "출고빈도등급":
        grade_mentions = [(match.start(), match.group(1)) for match in re.finditer(
            r"(?<![A-Za-z])([A-FX])\s*등급", sentence) if match.group(1) in groups]
        missing = [name for name in groups if "자료부족" in name.replace(" ", "")]
        if len(missing) == 1:
            grade_mentions.extend((match.start(), missing[0]) for match in re.finditer(
                r"(?:빈도)?자료\s*부족", sentence))
    else:
        grade_mentions = [(match.start(), name) for name in groups if len(name) > 1
                          for match in re.finditer(re.escape(name), sentence)]
    grade_mentions.sort()

    if leader_claim:
        if not distribution_complete or not grade_mentions:
            return False
        mentioned_metrics = _mentioned_metric_names(complete_metrics, sentence)
        if any(term in sentence for term in ("건수", "제품 수", "제품수", "품목 수", "품목수")):
            mentioned_metrics.append("rows")
        if len({name for _, name in grade_mentions}) != 1 or not mentioned_metrics:
            return False
        grade = grade_mentions[0][1]
        for name in mentioned_metrics:
            values = ({key: int(item["rows"]) for key, item in groups.items()} if name == "rows"
                      else group_metrics[name])
            ranked = {key: value for key, value in values.items() if value is not None}
            if not ranked or float(ranked[grade]) < max(float(value) for value in ranked.values()):
                return False

    def metric_hints(prefix: str) -> list[str]:
        compact_prefix = re.sub(r"\s+", "", prefix)
        hints = [(compact_prefix.rfind(re.sub(r"\s+", "", name)) + len(re.sub(r"\s+", "", name)),
                  len(re.sub(r"\s+", "", name)), name)
                 for name in metrics if re.sub(r"\s+", "", name) in compact_prefix]
        for phrase, label in (("매출합계", "총매출액"), ("매출액합계", "총매출액"),
                              ("매출세액", "총매출세액")):
            if phrase in compact_prefix and label in metrics and not any(
                    phrase in re.sub(r"\s+", "", name) for _, _, name in hints):
                hints.append((compact_prefix.rfind(phrase) + len(phrase), len(phrase), label))
        quantity_names = [name for name in metrics if "수량" in name]
        amount_names = [name for name in metrics if "금액" in name]
        if len(quantity_names) == 1 and "수량" in compact_prefix:
            hints.append((compact_prefix.rfind("수량") + 2, 0, quantity_names[0]))
        if len(amount_names) == 1 and "금액" in compact_prefix:
            hints.append((compact_prefix.rfind("금액") + 2, 0, amount_names[0]))
        if any(term in prefix for term in ("건수", "제품 수", "제품수", "품목 수", "품목수")):
            hints.append((max(prefix.rfind(term) + len(term) for term in
                              ("건수", "제품 수", "제품수", "품목 수", "품목수")), 0, "rows"))
        return [name for _, _, name in sorted(hints, reverse=True)]

    def same_displayed_value(actual: float, shown: str) -> bool:
        places = len(shown.partition(".")[2]) if "." in shown else 0
        return f"{actual:.{places}f}" == shown.replace(",", "")

    for match in claims:
        shown, unit = match.group(1), match.group(2)
        preceding = [(pos, name) for pos, name in grade_mentions if pos < match.start()]
        group_name = preceding[-1][1] if preceding else ""
        if preceding and "전체" in sentence[preceding[-1][0]:match.start()]:
            group_name = ""
        if group_name:
            if not distribution_complete:
                return False
        elif not distribution_complete:
            # An incomplete group list cannot prove that an unlabeled number
            # is a group value. Only explicitly stated whole-table totals pass.
            if not re.search(r"전체|합계|총", sentence[:match.start()]):
                return False
            if grade_mentions:
                return False
        prefix = sentence[:match.start()]
        if "매출세액" in re.sub(r"\s+", "", prefix) and not any(
                name in metrics for name in ("매출세액", "총매출세액")):
            return False
        hints = metric_hints(prefix)
        if unit == "건":
            candidates = ["rows"]
        elif unit == "개":
            candidates = [name for name in metrics if "수량" in name]
        elif unit in {"원", "만 원", "억 원"} or unit.replace(" ", "") in {"만원", "억원"}:
            candidates = [name for name in metrics if any(word in name for word in
                          ("금액", "매출액", "매출합계", "세액", "원가", "공급가액"))]
        else:
            candidates = ["rows", *metrics]
        if hints:
            preferred = next((name for name in hints if name in candidates), None)
            if preferred is not None:
                candidates = [preferred]
        if group_name:
            candidates = [name for name in candidates if name == "rows" or name in complete_metrics]
        if len(candidates) != 1:
            return False
        metric = candidates[0]
        if metric == "rows":
            value = int(groups[group_name]["rows"]) if group_name else row_total
            total = row_total
        else:
            stats = metrics[metric]
            value = group_metrics[metric].get(group_name) if group_name else stats["sum"]
            total = stats["sum"]
        if value is None or total is None or (unit in {"%", "퍼센트"} and not total):
            return False
        is_percent = unit in {"%", "퍼센트"}
        expected = float(value) / float(total) * 100 if is_percent else float(value)
        if unit.replace(" ", "") == "만원":
            expected /= 10000
        elif unit.replace(" ", "") == "억원":
            expected /= 100000000
        if not same_displayed_value(expected, shown):
            return False
    return True


def _render_operational_findings(facts: dict | None) -> str:
    findings = (facts or {}).get("operational_findings") or {}
    if not findings or (facts or {}).get("group_consistency_pass") is False:
        return ""

    def fmt(value: int | float) -> str:
        return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"

    def ranked(label: str, key: str) -> str:
        items = findings.get(key) or []
        if not items:
            return ""
        values = ", ".join(
            f"{item['name']} {fmt(item['quantity'])}개 ({item['share_pct']:.2f}%)"
            for item in items)
        return (f"- {label} 상위 {len(items)}개: {values}. "
                "비중은 전체 확보 행의 양수 수량 기준이며, 이 목록은 전체 목록이 아닙니다.")

    lines = []
    if "pending_quantity" in findings:
        due = ranked("납기일별 미입고 잔량", "due_date_top")
        if due:
            lines.append(due)
        lines.extend(filter(None, (
            ranked("발주거래처별 미입고 잔량", "vendor_top"),
            ranked("품목별 미입고 잔량", "product_top"),
        )))
        if lines:
            lines.append("- 확인 우선순위: 잔량 상위 전표의 납기일과 실제 입고 상태를 대조하세요. "
                         "이 자료만으로 지연 원인은 판단하지 않습니다.")
    else:
        lines.extend(filter(None, (
            ranked("거래처별 양수 수량", "vendor_top"),
            ranked("품목별 양수 수량", "product_top"),
        )))
        qty_negative = findings.get("negative_quantity_rows")
        raw_negative = findings.get("negative_supply_rows")
        final_negative = findings.get("negative_final_supply_rows")
        if any(value is not None and value > 0 for value in
               (qty_negative, raw_negative, final_negative)):
            lines.append("- 음수 행: " + ", ".join(
                f"{label} {value:,}건" for label, value in (
                    ("수량", qty_negative), ("원본 공급가액", raw_negative),
                    ("확정 공급가액", final_negative)) if value is not None) +
                ". 해당 전표를 확인하세요. 음수 발생 원인은 이 자료만으로 판단하지 않습니다.")
        if "supply_difference_rows" in findings:
            difference = findings["supply_difference_sum"]
            lines.append(
                f"- 원본·확정 공급가액 차이: 비교 가능한 {findings['supply_comparable_rows']:,}건 중 "
                f"{findings['supply_difference_rows']:,}건; 확정-원본 합계 "
                f"{difference:+,.0f}원. 차이 사유는 원전표 확인이 필요합니다.")
        matches = findings.get("document_match")
        if matches:
            lines.append(f"- 거래명세서 금액 일치 표시: Y {matches['Y']:,}건, "
                         f"N {matches['N']:,}건, 미기재 {matches['unknown']:,}건. "
                         "원본·확정 금액 차이와는 별개 지표입니다.")
        if not findings.get("tax_invoice_comparison_available"):
            lines.append("- 세금계산서 비교값 없음: 일치 여부를 판단하지 않습니다.")
        if findings.get("vendor_top") or findings.get("product_top"):
            lines.append("- 확인 우선순위: 양수 수량 상위 거래처·품목과 금액 차이 행의 원전표를 "
                         "먼저 대조하세요. 수량에는 서로 다른 포장단위가 섞일 수 있습니다.")
    return "**확인된 특징과 확인사항**\n" + "\n".join(lines) if lines else ""


def build_structured_analysis_options(facts: dict | None) -> list[dict]:
    """Offer only whole-source evidence and predefined checks to the model."""
    if not isinstance(facts, dict) or facts.get("group_consistency_pass") is False:
        return []
    findings = facts.get("operational_findings") or {}
    options: list[dict] = []

    def fmt(value: int | float) -> str:
        return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"

    def add(evidence_id: str, topic: str, statement: str,
            check_id: str, check: str) -> None:
        options.append({"evidence_id": evidence_id, "topic": topic,
                        "statement": statement, "check_id": check_id, "check": check})

    if "pending_quantity" in findings:
        pending = findings.get("pending_quantity")
        if pending is not None:
            add("pending_total", "미입고 잔량", f"미입고수량 합계 {fmt(pending)}개.",
                "check_pending", "미입고 잔량의 원전표와 실제 입고 상태를 대조하세요.")

    ranked = (
        ("due_top", "납기일별 미입고 잔량", "due_date_top",
         "check_due", "잔량 상위 전표의 납기일과 실제 입고 상태를 확인하세요."),
        ("vendor_top", "발주거래처별 미입고 잔량" if "pending_quantity" in findings
         else "거래처별 양수 수량", "vendor_top", "check_vendor",
         "상위 거래처의 원전표 수량을 대조하세요."),
        ("product_top", "품목별 미입고 잔량" if "pending_quantity" in findings
         else "품목별 양수 수량", "product_top", "check_product",
         "상위 품목의 원전표 수량을 대조하세요."),
    )
    for evidence_id, topic, key, check_id, check in ranked:
        items = findings.get(key) or []
        if not items:
            continue
        values = ", ".join(
            f"{item['name']} {fmt(item['quantity'])}개 ({item['share_pct']:.2f}%)"
            for item in items)
        add(evidence_id, topic,
            f"{topic} 상위 {len(items)}개: {values}. 비중은 전체 확보 행의 양수 수량 기준이며, "
            "이 목록은 전체 목록이 아닙니다.", check_id, check)

    if "negative_quantity_rows" in findings:
        counts = [("수량", findings.get("negative_quantity_rows")),
                  ("원본 공급가액", findings.get("negative_supply_rows")),
                  ("확정 공급가액", findings.get("negative_final_supply_rows"))]
        if any(value is not None and value > 0 for _, value in counts):
            statement = "음수 행: " + ", ".join(
                f"{label} {value:,}건" for label, value in counts if value is not None) + "."
            add("negative_rows", "음수 행", statement,
                "check_negative", "해당 원전표를 확인하세요. 발생 원인은 이 자료만으로 판단하지 않습니다.")
    if "supply_difference_rows" in findings:
        add("supply_difference", "원본·확정 공급가액 차이",
            f"원본·확정 공급가액 차이: 비교 가능한 {findings['supply_comparable_rows']:,}건 중 "
            f"{findings['supply_difference_rows']:,}건, 확정-원본 합계 "
            f"{findings['supply_difference_sum']:+,.0f}원.",
            "check_difference", "차이 행의 원전표를 대조하세요. 차이 원인은 확인 전 단정하지 않습니다.")
    matches = findings.get("document_match")
    if matches:
        add("document_match", "거래명세서 금액 일치 표시",
            f"거래명세서 금액 일치 표시: Y {matches['Y']:,}건, N {matches['N']:,}건, "
            f"미기재 {matches['unknown']:,}건. 원본·확정 공급가액 차이와는 별개 지표입니다.",
            "check_document", "표시 상태가 다른 전표가 있으면 해당 원전표를 확인하세요.")
    if "negative_quantity_rows" in findings and not findings.get("tax_invoice_comparison_available"):
        add("tax_comparison", "세금계산서 비교 근거",
            "세금계산서 비교값 없음: 일치 여부를 판단하지 않습니다.",
            "check_tax", "비교 원천을 확보한 뒤 일치 여부를 확인하세요.")
    if not options:
        distribution = facts.get("group_distribution") or []
        row_count = int(facts.get("row_count") or 0)
        if (distribution and not facts.get("group_distribution_omitted_count")
                and len(distribution) == facts.get("group_count")
                and sum(int(item["rows"]) for item in distribution) == row_count):
            largest = max(distribution, key=lambda item: int(item["rows"]))
            add("largest_group", str(facts.get("group_column") or "그룹") + "별 건수",
                f"{facts.get('group_column') or '그룹'}별 건수 상위: "
                f"{largest['group']} {int(largest['rows']):,}건 "
                f"({float(largest['row_share_pct']):.2f}%).",
                "check_group", "해당 그룹의 원본과 지표 정의를 확인하세요.")
    if not options:
        count = int(facts.get("row_count") or 0)
        if count:
            limited = facts.get("analysis_scope") in {"display_only", "partial_source"}
            add("source_scope", "분석 자료 범위",
                f"{'확보된 자료' if limited else '전체 조회'} {count:,}건 기준"
                + ("이며 전체 조회 합계는 검증되지 않았습니다." if limited else "."),
                "check_scope", "조회조건과 검증된 지표의 적용 범위를 확인하세요.")
    return options


def compose_structured_analysis_output(table: str, model_text: str,
                                       *, facts: dict | None, options: list[dict]) -> str:
    """Render only Python-verified statements; the model may choose priority IDs."""
    allowed = {item["evidence_id"]: item for item in options}
    selected: list[str] = []
    try:
        response = json.loads(str(model_text or ""))
        if (not isinstance(response, dict) or set(response) != {"items"}
                or not isinstance(response.get("items"), list)):
            raise ValueError("invalid_structure")
        for item in response["items"][:len(allowed)]:
            if not isinstance(item, dict) or set(item) != {"evidence_id", "check_id"}:
                continue
            evidence_id = item.get("evidence_id")
            if (isinstance(evidence_id, str) and evidence_id in allowed
                    and item.get("check_id") == allowed[evidence_id]["check_id"]
                    and evidence_id not in selected):
                selected.append(evidence_id)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    ordered = [allowed[key] for key in selected]
    ordered.extend(item for item in options if item["evidence_id"] not in selected)
    sections = [table] if table else []
    if isinstance(facts, dict) and not table:
        row_count = int(facts.get("row_count") or 0)
        scope = str(facts.get("query_scope_summary") or "").strip()
        if scope:
            sections.append("조회조건: " + scope)
        if row_count:
            limited = facts.get("analysis_scope") in {"display_only", "partial_source"}
            display_count = int(facts.get("display_row_count") or 0)
            sections.append(f"검증된 {'확보 자료' if limited else '전체'} 현황: {row_count:,}건 기준"
                            + (f" (화면 표시 {display_count:,}건)" if 0 < display_count < row_count else "")
                            + ("이며 전체 조회 합계는 검증되지 않았습니다." if limited else "."))
        metrics = facts.get("numeric_metrics") or {}
        totals = []
        for name in facts.get("selected_metrics") or list(metrics):
            stats = metrics.get(name) or {}
            value = stats.get("sum")
            if (value is None or stats.get("aggregation") != "sum"
                    or stats.get("group_sum_matches_total") is False):
                continue
            shown = f"{float(value):,.0f}" if float(value).is_integer() else f"{float(value):,.2f}"
            suffix = "원" if re.search(r"금액|매출액|세액|원가|공급가액", name) else "개"
            totals.append(f"{name} {shown}{suffix}")
            if len(totals) == 4:
                break
        if totals:
            sections.append("검증된 지표: " + ", ".join(totals) + ".")
    if ordered:
        lines = ["**확인된 특징과 확인사항**"]
        for item in ordered:
            lines.append(f"- {item['statement']} {item['check']}")
        sections.append("\n".join(lines))
    elif not sections:
        sections.append("검증 가능한 집계 근거가 없어 분석 범위를 확정할 수 없습니다.")
    return "\n\n".join(sections)


def structured_analysis_code_id() -> str:
    payload = (build_structured_analysis_options.__code__,
               compose_structured_analysis_output.__code__)
    return hashlib.sha256(marshal.dumps(payload)).hexdigest()[:12]


def compose_verified_analysis_output(table: str, interpretation: str,
                                     *, facts: dict | None = None) -> str:
    """Preserve prose but expose only aggregate claims supported by Python facts."""
    source_lines = str(interpretation or "").splitlines()
    numbered_list_indices = set()
    for index in range(len(source_lines) - 1):
        first = re.match(r"^(\s*)(\d+)[.)]\s+\S", source_lines[index])
        second = re.match(r"^(\s*)(\d+)[.)]\s+\S", source_lines[index + 1])
        if not first or not second or first.group(1) != second.group(1):
            continue
        if int(second.group(2)) != int(first.group(2)) + 1:
            continue
        before = next((source_lines[prior].strip() for prior in range(index - 1, -1, -1)
                       if source_lines[prior].strip()), "")
        if (index in numbered_list_indices or
                re.match(r"^#{1,6}\s+|^\*\*[^*]+\*\*$", before)):
            numbered_list_indices.update((index, index + 1))
    lines = []
    numbered_list_output_indices = set()
    pending_headings = []
    pending_list_parents = []
    for source_index, line in enumerate(source_lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if "|" in line:
            continue  # The only aggregate table is the Python-verified one.
        if re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", stripped):
            continue
        if re.fullmatch(r"(?:\d+[.)]|[①-⑳]|[-•])\s*", stripped):
            continue
        if not stripped:
            if lines:
                lines.append("")
            continue
        list_item = re.match(r"[-•]\s+(.+)$", stripped)
        list_content = re.sub(r"\*\*", "", list_item.group(1)).strip() if list_item else ""
        empty_list = re.fullmatch(r"[^:：]{1,70}[:：]\s*((?:\d+[.)]\s*)*)", list_content)
        if empty_list and empty_list.group(1):
            continue
        if empty_list:
            if (indent == 0 and pending_headings and
                    re.fullmatch(r"[^:：.!?]{1,70}[:：]", pending_headings[-1][1].strip())):
                pending_headings.pop()
            pending_list_parents = [item for item in pending_list_parents if item[0] < indent]
            if _verified_interpretation_sentence(stripped, facts):
                pending_list_parents.append((indent, line))
            continue
        if re.fullmatch(r"[-•]?\s*다음과 같습니다[.:：]?", stripped):
            continue
        if (re.fullmatch(r"[-•]\s*[^:：]{1,70}[:：]\s*", stripped)
                or re.fullmatch(
                    r"[-•]?\s*[^:：]*(?:상위|Top\s*\d+)[^:：]*[:：]\s*(?:\d+[.)]?\s*)*",
                    stripped, flags=re.IGNORECASE)
                or re.fullmatch(r"\*\*[^*]*(?:상위|Top\s*\d+)[^*]*[:：]\*\*",
                                stripped, flags=re.IGNORECASE)):
            continue
        heading = (stripped.startswith("#") or re.fullmatch(r"\*\*[^*]+\*\*", stripped)
                   or bool(indent == 0 and source_index not in numbered_list_indices and
                           re.match(r"^(?:\d+\.|[①-⑳])\s*[^!?]+(?<!\.)$", stripped))
                   or bool(re.fullmatch(r"[-•]?\s*[^.!?]{1,35}(?:현황|분포|집중도|확인사항|관리 제안):?", stripped))
                   or bool(re.fullmatch(r"[^:：.!?]{1,70}[:：]", stripped)))
        if heading:
            pending_list_parents.clear()
            if _verified_interpretation_sentence(stripped, facts):
                markdown_heading = re.match(r"^(#{1,6})\s+", stripped)
                level = (len(markdown_heading.group(1)) if markdown_heading else
                         1 if re.match(r"^\d+\.\s+", stripped) else
                         2 if re.match(r"^[①-⑳]", stripped) else 3)
                while pending_headings and pending_headings[-1][0] >= level:
                    pending_headings.pop()
                pending_headings.append((level, line))
            continue
        sentences = re.split(r"(?<=[.!?])\s+", stripped)
        kept = [part for part in sentences if _verified_interpretation_sentence(part, facts)]
        if table:
            kept = [part for part in kept if not re.fullmatch(
                r"[-•]?\s*할증수량\s*(?:합계|은|는)?\s*0\s*(?:개|건)?[.]?", part.strip())]
        if (facts or {}).get("analysis_scope") in {"display_only", "partial_source"}:
            kept = [part for part in kept if not re.search(
                r"전체\s*[\d,]+\s*(?:건|개|원)", part)]
        group_values_complete = any(
            stats.get("group_values_complete")
            for stats in ((facts or {}).get("numeric_metrics") or {}).values())
        if (not table or (facts or {}).get("group_column") and not group_values_complete):
            kept = [part for part in kept if not re.search(
                r"집중|부족|급증|급감|많[다은]|적[다은]|높[다은]|낮[다은]|증가|감소", part)]
        if kept:
            lines.extend(text for _, text in pending_headings)
            pending_headings.clear()
            lines.extend(text for depth, text in pending_list_parents if depth < indent)
            pending_list_parents.clear()
            prefix = line[:indent] if re.match(r"^(?:[-•]|\d+[.)])\s+", stripped) else ""
            lines.append(prefix + " ".join(kept))
            if source_index in numbered_list_indices:
                numbered_list_output_indices.add(len(lines) - 1)
    prose = "\n".join(lines).strip()
    heading_numbers = {}
    numbered_lines = []
    for output_index, line in enumerate(lines):
        if output_index in numbered_list_output_indices:
            numbered_lines.append(line)
            continue
        markdown = re.match(r"^(#{1,6}\s+)(\d+)[.)]\s+(.+)$", line)
        bold = re.match(r"^(\*\*)(\d+)[.)]\s+(.+?)(\*\*)$", line)
        plain = re.match(r"^(\d+)[.)]\s+(.+)$", line)
        circled = re.match(r"^([①-⑳])\s+(.+)$", line)
        if markdown or bold or plain or circled:
            level = (len(markdown.group(1).strip()) if markdown else
                     2 if bold else 3 if circled else 1)
            heading_numbers = {key: count for key, count in heading_numbers.items() if key <= level}
            heading_numbers[level] = heading_numbers.get(level, 0) + 1
            number = heading_numbers[level]
            if markdown:
                line = f"{markdown.group(1)}{number}. {markdown.group(3)}"
            elif bold:
                line = f"**{number}. {bold.group(3)}**"
            elif circled:
                line = f"{chr(0x2460 + number - 1)} {circled.group(2)}"
            else:
                line = f"{number}. {plain.group(2)}"
        numbered_lines.append(line)
    prose = "\n".join(numbered_lines).strip()
    if not prose and not table:
        prose = "검증 가능한 전체 그룹 근거가 부족해 수치와 분포 판단을 보류합니다."
    elif not prose:
        distribution = (facts or {}).get("group_distribution") or []
        if distribution and any(
            stats.get("group_values_complete")
            for stats in ((facts or {}).get("numeric_metrics") or {}).values()
        ):
            largest = max(distribution, key=lambda item: int(item["rows"]))["group"]
            prose = (f"{facts['group_column']}별 건수는 {largest} 그룹이 가장 많습니다. "
                     "검증된 집계표에서 수량·금액의 집중도를 확인하고 관리 우선순위를 점검하세요.")
        else:
            prose = "검증된 합계를 확인하고 세부 거래 근거를 점검하세요."
    if isinstance(facts, dict) and facts.get("group_consistency_pass") is not False:
        limited_scope = facts.get("analysis_scope") in {"display_only", "partial_source"}
        summary = []
        row_count = int(facts.get("row_count") or 0)
        if row_count and not table and f"{row_count:,}" not in prose:
            summary.append(f"{'확보된 자료' if limited_scope else '전체'} {row_count:,}건")
        metric_names = facts.get("selected_metrics") or list((facts.get("numeric_metrics") or {}))
        for name in metric_names:
            stats = (facts.get("numeric_metrics") or {}).get(name) or {}
            value = stats.get("sum")
            if stats.get("aggregation") != "sum" or value is None or stats.get("group_sum_matches_total") is False:
                continue
            if table and float(value) == 0:
                continue
            shown = f"{float(value):,.0f}" if float(value).is_integer() else f"{float(value):,.2f}"
            if re.search(re.escape(str(name)) + r"[^\n]{0,40}" + re.escape(shown), prose):
                continue
            suffix = "원" if re.search(r"금액|매출액|세액|원가|공급가액", name) else ""
            summary.append(f"{name} 합계 {shown}{suffix}")
            if len(summary) >= (1 if table else 4):
                break
        distribution = facts.get("group_distribution") or []
        if (table and distribution and any(
                stats.get("group_values_complete")
                for stats in (facts.get("numeric_metrics") or {}).values())
                and not facts.get("group_distribution_omitted_count")
                and len(distribution) == facts.get("group_count") and
                sum(int(item["rows"]) for item in distribution) == row_count):
            missing_groups = [item for item in distribution[:3]
                              if f"{item['group']} {int(item['rows']):,}건" not in prose]
            if missing_groups:
                groups = ", ".join(f"{item['group']} {int(item['rows']):,}건" for item in missing_groups)
                summary.append(f"{facts['group_column']}별 건수: {groups}")
        if summary:
            label = "검증된 확보 자료 현황" if limited_scope else "검증된 전체 현황"
            prose = label + ": " + "; ".join(summary) + ".\n\n" + prose
    findings_text = _render_operational_findings(facts)
    sections = [part for part in (table, findings_text, "**해석 및 관리 제안**\n\n" + prose) if part]
    return "\n\n".join(sections)


def fit_analysis_context(context: dict, limit: int, *, question: str = "",
                         diagnostics: dict | None = None) -> dict:
    """Fit model evidence without changing the cached source or reconciled totals."""
    size = lambda value: len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")))
    if diagnostics is not None:
        diagnostics["original_chars"] = size(context)
        diagnostics["section_chars_before"] = {
            key: size(value) for key, value in context.items()
        }
        params_shape = context.get("params")
        if isinstance(params_shape, dict):
            diagnostics["params_shape"] = {
                str(key): {"type": type(value).__name__, "count": len(value)
                           if isinstance(value, (dict, list, tuple, str)) else 1,
                           "chars": size(value)}
                for key, value in list(params_shape.items())[:60]
            }
    if context.get("analysis_target") != "current_table_whole_facts" and size(context) <= limit:
        if diagnostics is not None:
            diagnostics["reduced_chars"] = size(context)
            diagnostics["reductions"] = []
        return context

    if context.get("analysis_target") != "current_table_whole_facts":
        out = json.loads(json.dumps(context, ensure_ascii=False, default=str))
        reductions = []
        source_samples = len(out.get("sample_records") or [])
        source_fact_records = {
            key: len((out.get("whole_table_facts") or {}).get(key) or [])
            for key in ("table_records", "representative_records")
        }
        omitted_profiles = {}
        group_details_compacted = False
        omitted_param_aliases = []
        omitted_group_breakdowns = []
        omitted_summary_lists = {}
        omitted_filter_lists = {}

        def fits(stage: str) -> bool:
            facts = out.get("whole_table_facts") or {}
            scope = {}
            if len(out.get("sample_records") or []) < source_samples:
                scope["sample_records"] = [len(out["sample_records"]), source_samples]
            for key, original in source_fact_records.items():
                if len(facts.get(key) or []) < original:
                    scope[key] = [len(facts.get(key) or []), original]
            if omitted_profiles:
                scope["profile_lists_omitted"] = dict(omitted_profiles)
            if group_details_compacted:
                scope["group_detail_fields_omitted"] = True
            if omitted_param_aliases:
                scope["transport_params_omitted"] = list(omitted_param_aliases)
            if omitted_group_breakdowns:
                scope["group_breakdowns_omitted"] = list(omitted_group_breakdowns)
            if omitted_summary_lists:
                scope["summary_lists_omitted"] = dict(omitted_summary_lists)
            if omitted_filter_lists:
                scope["applied_filter_values_omitted"] = dict(omitted_filter_lists)
            if scope:
                out["delivery_scope"] = {"source_rows": out.get("row_count"), **scope}
            else:
                out.pop("delivery_scope", None)
            current = size(out)
            if diagnostics is not None:
                diagnostics["reduced_chars"] = current
                diagnostics["stage"] = stage
                diagnostics["reductions"] = list(reductions)
                diagnostics["section_chars_after"] = {key: size(value) for key, value in out.items()}
            return current <= limit

        # These are transport instructions or cache identity, not analysis evidence.
        for key in ("llm_rules", "analysis_key", "sensitive_columns_removed"):
            if key in out:
                out.pop(key)
                reductions.append(key)
        meta = out.get("meta")
        if isinstance(meta, dict):
            for key in ("llm_summary_md", "summary_md", "in_detail_summary", "out_detail_summary",
                        "inventory_summary", "trans_doc_summary", "tax_doc_summary",
                        "monthly_stock_detail_summary"):
                if (key in meta and (meta[key] == out.get("llm_summary_md")
                                     or meta[key] == out.get("detail_summary"))):
                    meta.pop(key)
            if not meta:
                out.pop("meta", None)
        if fits("deduplicated"):
            return out

        params = out.get("params")
        if isinstance(params, dict):
            for key in ("_original_question", "_display_context"):
                if key in params:
                    params.pop(key)
                    omitted_param_aliases.append(key)
            policy = params.get("_period_source_policy")
            if isinstance(policy, dict) and (params.get("date_from") or params.get("month_from")):
                brief = {key: policy[key] for key in
                         ("default_policy", "source_mode", "evaluation_month", "evaluation_mode",
                          "use_hybrid", "basis_months")
                         if key in policy}
                params["_period_source_policy"] = brief
                omitted_param_aliases.append("_period_source_policy.diagnostics")
            for alias, canonical in (
                ("stock_cds", "stock_cd_list"),
                ("dashboard_product_group_list", "product_group_list"),
                ("dashboard_product_di_list", "product_di_list"),
                ("dashboard_product_class_list", "product_class_list"),
            ):
                if alias in params and canonical in params and params[alias] == params[canonical]:
                    params.pop(alias)
                    omitted_param_aliases.append(alias)
        if omitted_param_aliases:
            reductions.append("duplicated_transport_params")
            if fits("params_deduplicated"):
                return out

        detail = out.get("detail_summary")
        if isinstance(detail, dict):
            for key in ("top_vendors", "top_purchase_vendors", "top_sales_vendors"):
                if key == "top_vendors" and any(
                    detail.get(key) == detail.get(other) for other in
                    ("top_purchase_vendors", "top_sales_vendors") if other in detail
                ):
                    detail.pop(key)
                    reductions.append("repeated_vendor_summary")
                    break
            # For these detail actions the prose summary is rendered from the
            # same structured totals and top lists by the NLQ router.
            if (out.get("action") in {"입고명세 조회", "출고명세 조회"}
                    and all(key in detail for key in
                            ("query_summary", "qty_sum", "supply_sum", "tax_sum", "amount_sum"))):
                out.pop("llm_summary_md", None)
                reductions.append("rendered_detail_summary")
            if fits("detail_deduplicated"):
                return out

        facts = out.get("whole_table_facts") or {}
        for key in ("selected_metrics", "interpretation_limits"):
            facts.pop(key, None)
        for stats in (facts.get("numeric_metrics") or {}).values():
            if stats.get("group_values_complete"):
                stats.pop("top_groups", None)
                stats.pop("bottom_groups", None)
        reductions.append("repeated_fact_descriptions")
        if fits("facts_deduplicated"):
            return out

        for depth in (3, 1, 0):
            for stats in (facts.get("numeric_metrics") or {}).values():
                for key in ("top_groups", "bottom_groups"):
                    if isinstance(stats.get(key), list):
                        stats[key] = stats[key][:depth]
            facts["rank_depth_delivered"] = depth
            reductions.append(f"rank_depth_{depth}")
            if fits("rank_reduced"):
                return out

        for depth in (20, 12, 8, 4, 2):
            if isinstance(out.get("sample_records"), list):
                out["sample_records"] = out["sample_records"][:depth]
            for key in source_fact_records:
                if isinstance(facts.get(key), list):
                    facts[key] = facts[key][:depth]
                    if key == "table_records" and len(facts[key]) < source_fact_records[key]:
                        facts["table_records_complete"] = False
            reductions.append(f"detail_depth_{depth}")
            if fits("detail_reduced"):
                return out

        profile_keys = ("risk_products_top", "grade_samples", "sales_time_profile",
                        "sales_group_profile", "doc_time_profile", "doc_counterparty_profile")
        profile_source_counts = {}

        def collect_profile_counts(value, path: str) -> None:
            if isinstance(value, list):
                profile_source_counts[path] = len(value)
            elif isinstance(value, dict):
                for key, item in value.items():
                    collect_profile_counts(item, f"{path}.{key}")

        for key in profile_keys:
            collect_profile_counts(out.get(key), key)

        def trim_profile(value, depth: int, path: str):
            if isinstance(value, list):
                if len(value) > depth:
                    omitted_profiles[path] = profile_source_counts[path] - depth
                return value[:depth]
            if isinstance(value, dict):
                return {key: trim_profile(item, depth, f"{path}.{key}")
                        for key, item in value.items()}
            return value

        for depth in (8, 4, 2):
            omitted_profiles.clear()
            for key in profile_keys:
                if key in out:
                    out[key] = trim_profile(out[key], depth, key)
            reductions.append(f"profile_depth_{depth}")
            if fits("profiles_reduced"):
                return out

        for stats in (facts.get("numeric_metrics") or {}).values():
            if stats.get("group_values_complete") and isinstance(stats.get("group_values"), list):
                for item in stats["group_values"]:
                    for key in ("valid_count", "negative_count", "zero_count"):
                        if key in item:
                            item.pop(key)
                            group_details_compacted = True
        if group_details_compacted:
            reductions.append("repeated_group_details")
            if fits("group_details_compacted"):
                return out
        # The structured fields and system instruction already carry this prose.
        if "analysis_text" in out:
            out.pop("analysis_text")
            reductions.append("repeated_analysis_text")
            if fits("text_deduplicated"):
                return out
        if isinstance(out.get("sample_records"), list):
            out["sample_records"] = []
        for key in source_fact_records:
            if isinstance(facts.get(key), list):
                facts[key] = []
                if key == "table_records":
                    facts["table_records_complete"] = False
        reductions.append("detail_omitted")
        if fits("details_omitted"):
            return out

        if isinstance(detail, dict):
            source_list_counts = {key: len(value) for key, value in detail.items()
                                  if isinstance(value, list)}
            requested_lists = set()
            for term, keys in (
                ("매입처", ("top_purchase_vendors", "top_buy_vendors")),
                ("매출처", ("top_sales_vendors", "top_real_vendors")),
                ("거래처", ("top_vendors", "top_purchase_vendors", "top_sales_vendors")),
                ("제품", ("top_products",)), ("품목", ("top_products",)),
                ("재고위치", ("top_stock_locations",)),
                ("영업사원", ("top_sales_staff",)),
            ):
                if term in str(question or ""):
                    requested_lists.update(key for key in keys if key in source_list_counts)
            for depth in (5, 2, 0):
                for key, original in source_list_counts.items():
                    delivered_depth = max(depth, 2) if key in requested_lists else depth
                    detail[key] = detail[key][:delivered_depth]
                    if original > len(detail[key]):
                        omitted_summary_lists[key] = [len(detail[key]), original]
                reductions.append(f"summary_depth_{depth}")
                if fits("summary_reduced"):
                    return out

        metrics = (facts.get("numeric_metrics") or {})
        requested = _mentioned_metric_names(metrics, question)
        if requested:
            facts["numeric_metrics"] = {
                name: metrics[name] for name in [*requested, *(name for name in metrics if name not in requested)]
            }
        complete = [name for name in metrics if metrics[name].get("group_values_complete")]
        optional = [name for name in complete if name not in requested]
        for keep_count in (4, 2, 1, 0):
            keep = set(requested) | set(optional[:keep_count])
            for name in complete:
                if name in keep:
                    continue
                if metrics[name].get("group_values_complete"):
                    metrics[name].pop("group_values", None)
                    metrics[name]["group_values_complete"] = False
                    omitted_group_breakdowns.append(name)
            if omitted_group_breakdowns:
                reductions.append(f"group_breakdowns_{keep_count}")
                if fits("group_breakdowns_reduced"):
                    return out

        # Applied lists can dominate a detail context. Their cardinality remains
        # explicit, but the values are withheld only when the question does not
        # ask about that filter or one of its codes.
        filter_terms = {
            "product_group_list": ("제품그룹", "제품군"),
            "product_di_list": ("제품구분",),
            "product_class_list": ("제품분류",),
            "stock_cd_list": ("재고위치", "창고"),
            "io_gu_list": ("입출고구분", "입고구분", "출고구분"),
        }
        question_compact = re.sub(r"\s+", "", str(question or ""))
        asks_applied_codes = ("코드" in question_compact
                              and any(term in question_compact for term in ("적용", "사용", "선택", "설정"))
                              and any(term in question_compact for term in ("모두", "전체", "목록", "알려", "보여")))
        if isinstance(params, dict):
            for key, terms in filter_terms.items():
                values = params.get(key)
                if not isinstance(values, list) or len(values) <= 10:
                    continue
                if asks_applied_codes or any(term in question_compact for term in terms):
                    continue
                if any(len(str(value)) >= 3 and str(value) in question_compact for value in values):
                    continue
                omitted_filter_lists[key] = len(values)
                params.pop(key)
                reductions.append(f"filter_values_{key}")
                if fits("filter_values_omitted"):
                    return out
        raise ValueError("context_budget_exceeded")

    # Current-table evidence retains its existing, separate rank/record policy.
    out = json.loads(json.dumps(context, ensure_ascii=False, default=str))
    facts = out["whole_table_facts"]
    for depth in (5, 3, 1, 0):
        for stats in facts["numeric_metrics"].values():
            for key in ("top_groups", "bottom_groups"):
                if key in stats:
                    stats[key] = stats[key][:depth]
        for key in ("table_records", "representative_records"):
            if key in facts:
                original = len(facts[key])
                facts[key] = facts[key][:depth]
                if len(facts[key]) != original:
                    facts["table_records_complete"] = False
        facts["rank_depth_delivered"] = depth
        if len(json.dumps(out, ensure_ascii=False, default=str)) <= limit:
            if diagnostics is not None:
                diagnostics["reduced_chars"] = size(out)
                diagnostics["stage"] = f"current_table_depth_{depth}"
            return out
    # A very wide schema is still inspected, but transmitted names are bounded.
    facts["unique_counts"] = dict(list(facts["unique_counts"].items())[:20])
    facts["omitted_metric_columns"] = facts["omitted_metric_columns"][:20]
    if len(json.dumps(out, ensure_ascii=False, default=str)) > limit:
        if diagnostics is not None:
            diagnostics["reduced_chars"] = size(out)
            diagnostics["stage"] = "current_table_minimum"
        raise ValueError("Current-table analysis statistics exceed model context budget")
    if diagnostics is not None:
        diagnostics["reduced_chars"] = size(out)
        diagnostics["stage"] = "current_table_metadata_reduced"
    return out
