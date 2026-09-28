"""Bounded interpretation material computed from the in-memory source only."""
from __future__ import annotations

import json
import math
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


def build_whole_table_facts(df: pd.DataFrame, *, action: str, query: str,
                            group_column: str = "") -> dict:
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
    for c in safe.columns:
        if re.search(r"제품명|제품코드|품목명|제조사|거래처|등급|판정", str(c)):
            result["unique_counts"][str(c)] = int(safe[c].replace("", pd.NA).nunique())
    if groups is not None:
        counts = groups.value_counts(dropna=False)
        result["group_count"] = len(counts)
        result["group_distribution"] = [
            {"group": text(k, group_column), "rows": int(v), "row_share_pct": float(v / len(safe) * 100)}
            for k, v in counts.head(12).items()]
        result["group_distribution_omitted_count"] = max(0, len(counts) - 12)
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
            rank_col = "sum" if additive else "mean"
            ordered = aggregated.dropna(subset=[rank_col]).sort_values(rank_col, ascending=False, kind="stable")
            def group_records(frame):
                return [{"group": text(k, group_column), "value": number(row[rank_col]),
                         "rows": int(row["rows"]), "valid_count": int(row["valid_count"]),
                         "negative_count": int(row["negative_count"]), "zero_count": int(row["zero_count"])}
                        for k, row in frame.iterrows()]
            stats["top_groups"] = group_records(ordered.head(5))
            stats["bottom_groups"] = group_records(ordered.tail(5).iloc[::-1])
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


def fit_analysis_context(context: dict, limit: int) -> dict:
    """Reduce examples/rank depth, not full-table statistics or JSON syntax."""
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
            return out
    # A very wide schema is still inspected, but transmitted names are bounded.
    facts["unique_counts"] = dict(list(facts["unique_counts"].items())[:20])
    facts["omitted_metric_columns"] = facts["omitted_metric_columns"][:20]
    if len(json.dumps(out, ensure_ascii=False, default=str)) > limit:
        raise ValueError("Current-table analysis statistics exceed model context budget")
    return out
