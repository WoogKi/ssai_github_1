# app/services/rddbc_io_common.py

from __future__ import annotations

import math
import os
import re
from functools import lru_cache
from typing import Any, Dict, Iterable, Optional

import pandas as pd


OUTBOUND_TRANS_DI_PRIMARY = "3"
OUTBOUND_TRANS_DI_COMPAT = ("3", "2")


def _resolve_query_func():
    from app.db import mssql_client

    for name in (
        "query_to_df",
        "fetch_dataframe",
        "execute_query_df",
        "read_sql_df",
        "run_query_df",
        "query_df",
        "query",
    ):
        fn = getattr(mssql_client, name, None)
        if callable(fn):
            return fn

    raise AttributeError(
        "app.db.mssql_client 에서 DataFrame 반환 함수를 찾지 못했습니다. "
        "query_to_df / fetch_dataframe / execute_query_df / read_sql_df / run_query_df / query_df 중 하나를 준비해 주세요."
    )


def _quote_sql_value(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value) if math.isfinite(value) else "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def render_named_sql(sql: str, params: Optional[Dict[str, Any]] = None) -> str:
    params = params or {}

    def repl(match):
        key = match.group(1)
        return _quote_sql_value(params.get(key))

    return re.sub(r"%\(([^)]+)\)s", repl, sql)


def query_to_df(sql: str, params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    params = params or {}
    fn = _resolve_query_func()

    has_named_params = isinstance(params, dict) and bool(re.search(r"%\(([^)]+)\)s", sql))

    if has_named_params:
        rendered = render_named_sql(sql, params)
        try:
            return fn(rendered)
        except TypeError:
            try:
                return fn(sql=rendered)
            except TypeError:
                return fn(rendered, ())

    try:
        return fn(sql, params)
    except TypeError:
        try:
            return fn(sql=sql, params=params)
        except TypeError:
            rendered = render_named_sql(sql, params)
            try:
                return fn(rendered)
            except TypeError:
                return fn(sql=rendered)


def apply_labels_safe(df: pd.DataFrame, table: str) -> pd.DataFrame:
    table = str(table or "").lower()

    try:
        from app.services.utils import apply_labels
        out = apply_labels(df, table)
        if isinstance(out, pd.DataFrame):
            df = out
    except Exception:
        pass

    try:
        from app.db.labels_map import LABELS
        labels = LABELS.get(table, {}) or {}
        if labels:
            df = df.rename(columns=labels)
    except Exception:
        pass

    return df


def ensure_unique_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None:
        return df

    seen: Dict[str, int] = {}
    cols = []
    for c in list(df.columns):
        name = str(c)
        if name not in seen:
            seen[name] = 1
            cols.append(name)
        else:
            seen[name] += 1
            cols.append(f"{name}_{seen[name]}")

    out = df.copy()
    out.columns = cols
    return out


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default)) or default).strip())
    except Exception:
        return int(default)


def _default_query_max_rows(default: int = 30000) -> int:
    return max(
        1,
        _env_int(
            "SIMS_PANEL_DISPLAY_MAX_ROWS",
            _env_int("SIMS_CHAT_DISPLAY_MAX_ROWS", default),
        ),
    )


def normalize_top(value: Any, default: int = 200, max_value: Optional[int] = None) -> int:
    try:
        v = int(value)
    except Exception:
        v = int(default)

    if v < 1:
        return int(default)

    if max_value is None:
        max_value = _default_query_max_rows()

    return min(v, int(max_value))


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def like_value(value: Any) -> Optional[str]:
    text = clean_text(value)
    if not text:
        return None
    return f"%{text}%"


def coalesce_params(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return dict(params or {})


def add_filter(clauses: list[str], sql: str) -> None:
    if sql and sql.strip():
        clauses.append(sql.strip())


def add_unlabeled_name_like_filter(
    clauses: list[str],
    params: Dict[str, Any],
    *,
    vendor_name_exprs: Iterable[str] = (),
    product_name_expr: str = "",
    manufacturer_name_expr: str = "",
    manufacturer_predicate: str = "",
) -> bool:
    """Apply the NLQ label-free name contract without changing result cardinality.

    Detail and inventory searches intentionally keep every matching record.  The
    caller supplies already-joined name expressions so this helper can build a
    single OR predicate, rather than resolving a name to one arbitrary code.
    """
    pattern = like_value(params.get("nlq_unlabeled_name"))
    if not pattern:
        return False

    params["nlq_unlabeled_name_like"] = pattern
    expressions = [str(expr).strip() for expr in vendor_name_exprs if str(expr).strip()]
    if str(product_name_expr).strip():
        expressions.append(str(product_name_expr).strip())
    if str(manufacturer_name_expr).strip():
        expressions.append(str(manufacturer_name_expr).strip())
    predicates = [f"{expr} LIKE %(nlq_unlabeled_name_like)s" for expr in expressions]
    if str(manufacturer_predicate).strip():
        predicates.append(str(manufacturer_predicate).strip())
    if not predicates:
        return False

    add_filter(
        clauses,
        "(" + " OR ".join(predicates) + ")",
    )
    return True


def make_date_filters(field: str, params: Dict[str, Any], start_key: str = "date_from", end_key: str = "date_to") -> list[str]:
    clauses: list[str] = []
    if clean_text(params.get(start_key)):
        clauses.append(f"{field} >= %({start_key})s")
    if clean_text(params.get(end_key)):
        clauses.append(f"{field} <= %({end_key})s")
    return clauses


def make_month_filters(field: str, params: Dict[str, Any], start_key: str = "month_from", end_key: str = "month_to") -> list[str]:
    clauses: list[str] = []
    if clean_text(params.get(start_key)):
        clauses.append(f"{field} >= %({start_key})s")
    if clean_text(params.get(end_key)):
        clauses.append(f"{field} <= %({end_key})s")
    return clauses


def io_prefix_name(prefix: str) -> str:
    mapping = {
        "0": "정상입고",
        "1": "입고반품",
        "2": "장부입고",
        "3": "미결입고",
        "4": "기타입고",
        "5": "정상출고",
        "6": "출고반품",
        "7": "장부출고",
        "8": "미결출고",
        "9": "기타출고",
    }
    return mapping.get(str(prefix), str(prefix))


def _norm_series(sr: pd.Series) -> pd.Series:
    return (
        sr.fillna("")
        .astype(str)
        .replace({"None": "", "nan": "", "<NA>": ""})
        .str.strip()
    )


@lru_cache(maxsize=1)
def _load_rddbc010_lookup() -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """
    Rddbc010 전체 lookup 캐시
    - kind_by_gcode[gcode] = 코드종류명
    - code_name_by_pair[(gcode, tcode)] = 코드명
    """
    sql = """
    SELECT
        LTRIM(RTRIM(Cd.Rd01_Gcode)) AS Gcode,
        LTRIM(RTRIM(Cd.Rd01_Tcode)) AS Tcode,
        LTRIM(RTRIM(ISNULL(Kind.Rd01_Hnm, ''))) AS KindName,
        LTRIM(RTRIM(ISNULL(Cd.Rd01_Hnm, ''))) AS CodeName,
        LTRIM(RTRIM(ISNULL(Cd.Rd01_Snm, ''))) AS ShortName,
        LTRIM(RTRIM(ISNULL(Cd.Rd01_Enm, ''))) AS EnName
    FROM dbo.Rddbc010 AS Cd WITH (NOLOCK)
    LEFT JOIN dbo.Rddbc010 AS Kind WITH (NOLOCK)
           ON Kind.Rd01_Gcode = '9999'
          AND Kind.Rd01_Tcode = Cd.Rd01_Gcode
    WHERE ISNULL(NULLIF(LTRIM(RTRIM(Cd.Rd01_Del_Flag)), ''), '0') NOT IN ('1', 'Y')
    """
    try:
        df = query_to_df(sql)
    except Exception:
        return {}, {}

    kind_by_gcode: dict[str, str] = {}
    code_name_by_pair: dict[tuple[str, str], str] = {}

    if df is None or len(df) == 0:
        return kind_by_gcode, code_name_by_pair

    for row in df.itertuples(index=False):
        g = clean_text(getattr(row, "Gcode", ""))
        t = clean_text(getattr(row, "Tcode", ""))
        kind = clean_text(getattr(row, "KindName", ""))
        code_name = clean_text(getattr(row, "CodeName", ""))
        short_name = clean_text(getattr(row, "ShortName", ""))
        en_name = clean_text(getattr(row, "EnName", ""))

        if g and kind:
            kind_by_gcode[g] = kind
        elif g and g not in kind_by_gcode:
            kind_by_gcode[g] = g

        final_name = code_name or short_name or en_name or t
        if g and t:
            code_name_by_pair[(g, t)] = final_name

    return kind_by_gcode, code_name_by_pair


def _find_rddbc010_pairs(df_raw: pd.DataFrame) -> list[tuple[str, str]]:
    """
    Rdxx_..._Gcode 와 대응 코드 필드를 찾는다.
    우선순위:
    1) <base>_Tcode
    2) <base>      (예: Rd12_Io_Gu_Gcode + Rd12_Io_Gu)
    """
    cols = [str(c) for c in df_raw.columns]
    pairs: list[tuple[str, str]] = []

    for gcol in cols:
        if not gcol.endswith("_Gcode"):
            continue

        base = gcol[:-6]  # remove "_Gcode"
        candidates = [f"{base}_Tcode", base]
        tcol = next((c for c in candidates if c in cols), None)
        if not tcol:
            continue

        pairs.append((gcol, tcol))

    return pairs


def _display_col_name(df_raw: pd.DataFrame, df_disp: pd.DataFrame, raw_col: str) -> str:
    raw_cols = [str(c) for c in df_raw.columns]
    disp_cols = [str(c) for c in df_disp.columns]
    try:
        idx = raw_cols.index(str(raw_col))
    except ValueError:
        return str(raw_col)

    if 0 <= idx < len(disp_cols):
        return disp_cols[idx]
    return str(raw_col)


def _insert_or_assign(df: pd.DataFrame, after_col: str, new_col: str, values: Iterable[Any]) -> pd.DataFrame:
    out = df.copy()
    values_list = list(values)

    if new_col in out.columns:
        out[new_col] = values_list
        return out

    try:
        pos = list(out.columns).index(after_col) + 1
    except ValueError:
        pos = len(out.columns)

    out.insert(pos, new_col, values_list)
    return out


def enrich_rddbc010_pairs(df_raw: pd.DataFrame, df_disp: pd.DataFrame) -> pd.DataFrame:
    """
    입출고/명세서/재고 결과의 코드쌍 컬럼에 대해
    [코드종류], [코드명] 보조 컬럼을 자동 추가한다.
    """
    if df_raw is None or df_disp is None or len(df_raw.columns) == 0:
        return df_disp

    kind_by_gcode, code_name_by_pair = _load_rddbc010_lookup()
    if not kind_by_gcode and not code_name_by_pair:
        return df_disp

    out = df_disp.copy()

    for gcol, tcol in _find_rddbc010_pairs(df_raw):
        gvals = _norm_series(df_raw[gcol]) if gcol in df_raw.columns else pd.Series([""] * len(df_raw), index=df_raw.index)
        tvals = _norm_series(df_raw[tcol]) if tcol in df_raw.columns else pd.Series([""] * len(df_raw), index=df_raw.index)

        kind_vals: list[str] = []
        name_vals: list[str] = []

        for g, t in zip(gvals.tolist(), tvals.tolist()):
            kind_nm = kind_by_gcode.get(g, g)
            code_nm = code_name_by_pair.get((g, t), t)
            kind_vals.append(kind_nm)
            name_vals.append(code_nm)

        disp_base = _display_col_name(df_raw, out, tcol)
        kind_col = f"{disp_base}_코드종류"
        name_col = f"{disp_base}_코드명"

        out = _insert_or_assign(out, disp_base, kind_col, kind_vals)
        out = _insert_or_assign(out, kind_col, name_col, name_vals)

    return out


def result_payload(
    *,
    title: str,
    action: str,
    params: Dict[str, Any],
    df: pd.DataFrame,
    table: str,
    message: Optional[str] = None,
) -> Dict[str, Any]:
    df_raw = ensure_unique_columns(df.copy())

    labeled = apply_labels_safe(df_raw.copy(), table)
    labeled = ensure_unique_columns(labeled)

    try:
        labeled = enrich_rddbc010_pairs(df_raw, labeled)
        labeled = ensure_unique_columns(labeled)
    except Exception:
        pass

    payload: Dict[str, Any] = {
        "title": title,
        "action": action,
        "params": params,
        "columns": list(labeled.columns),
        "df_display": labeled,
        "records": labeled.to_dict(orient="records"),
        "final": True,
    }
    if message:
        payload["message"] = message
    return payload


def build_result_payload(
    *,
    table: str,
    title: str,
    action: str,
    params: Dict[str, Any],
    df: pd.DataFrame,
    message: Optional[str] = None,
) -> Dict[str, Any]:
    df_raw = ensure_unique_columns(df.copy())
    df_disp = df_raw.copy()

    try:
        df_disp = apply_labels_safe(df_disp, table)
    except Exception:
        pass

    try:
        df_disp = ensure_unique_columns(df_disp)
    except Exception:
        pass

    try:
        df_disp = enrich_rddbc010_pairs(df_raw, df_disp)
        df_disp = ensure_unique_columns(df_disp)
    except Exception:
        pass

    payload = {
        "title": title,
        "action": action,
        "params": params,
        "columns": list(df_disp.columns),
        "df_display": df_disp,
        "records": df_disp.to_dict(orient="records"),
        "final": True,
    }
    if message:
        payload["message"] = message
    return payload
