"""Shared read-only query/result helpers for registered ERP table features."""

from __future__ import annotations

from calendar import monthrange
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
import os
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd

from app.services.rddbc_io_common import build_result_payload, like_value, normalize_top


def execute_bound_select(sql: str, params: Iterable[Any], *, company_id=None) -> pd.DataFrame:
    """Execute a SELECT with driver-bound values.

    SQL identifiers are owned by feature code. Only values are accepted here.
    """
    if company_id is not None:
        from app.services.ssai_auth_service import connect_company_db
        try:
            with closing(connect_company_db(company_id)) as db:
                db.timeout = 10
                with closing(db.cursor()) as cursor:
                    cursor.execute(sql, *tuple(params))
                    return pd.DataFrame([tuple(row) for row in cursor.fetchall()],
                                        columns=[c[0] for c in cursor.description], dtype=object)
        except Exception:
            raise RuntimeError("ERP 조회 연결 또는 응답을 확인해 주세요.") from None
    from app.db.mssql_client import query_to_df

    return query_to_df(str(sql), tuple(params))


def export_top() -> int:
    try:
        return max(1, int(os.getenv("SIMS_EXPORT_MAX_ROWS", "100000") or 100000))
    except Exception:
        return 100000


def _positive_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)) or default))
    except Exception:
        return int(default)


@dataclass(frozen=True)
class RegisteredQueryLimits:
    display_top: int
    display_limit_rows: int
    display_limit_source: str
    requested_display_top: int | None
    source_top: int
    export_limit_rows: int
    source_safety_limit_rows: int
    source_limit_source: str


def registered_query_limits(
    params: dict[str, Any],
    *,
    display_context: str = "chat",
    source_limit_env: str = "SIMS_IO_QUERY_MAX_ROWS",
) -> RegisteredQueryLimits:
    """Resolve display and source limits without letting UI TOP widen source reads."""
    context = str(params.get("_display_context") or display_context).strip().lower()
    if context == "panel":
        display_env = "SIMS_PANEL_DISPLAY_MAX_ROWS"
        display_default = 2000
    else:
        display_env = "SIMS_CHAT_DISPLAY_MAX_ROWS"
        display_default = 2000
    display_limit = _positive_env(display_env, display_default)
    requested_raw = params.get("display_top", params.get("top"))
    requested: int | None = None
    if requested_raw not in (None, ""):
        requested = normalize_top(requested_raw, default=display_limit, max_value=display_limit)
    display_top = requested if requested is not None else display_limit

    export_limit = export_top()
    source_env = str(source_limit_env or "SIMS_IO_QUERY_MAX_ROWS").strip()
    if source_env not in {"SIMS_IO_QUERY_MAX_ROWS", "SIMS_EXPORT_MAX_ROWS"}:
        raise ValueError(f"지원하지 않는 source limit 환경계약입니다: {source_env}")
    if source_env == "SIMS_EXPORT_MAX_ROWS":
        source_safety_limit = export_limit
        source_top = export_limit
        source_limit_source = "SIMS_EXPORT_MAX_ROWS"
    else:
        source_safety_limit = _positive_env("SIMS_IO_QUERY_MAX_ROWS", 30000)
        source_top = source_safety_limit
        source_limit_source = "SIMS_IO_QUERY_MAX_ROWS"
    return RegisteredQueryLimits(
        display_top=display_top,
        display_limit_rows=display_limit,
        display_limit_source=display_env,
        requested_display_top=requested,
        source_top=source_top,
        export_limit_rows=export_limit,
        source_safety_limit_rows=source_safety_limit,
        source_limit_source=source_limit_source,
    )


def build_feature_result(
    *,
    table: str,
    title: str,
    params: dict[str, Any],
    df: pd.DataFrame,
    summary_md: str,
) -> dict[str, Any]:
    row_count = int(len(df)) if isinstance(df, pd.DataFrame) else 0
    if row_count == 0:
        return {
            "table": table,
            "title": title,
            "action": title,
            "params": dict(params),
            "data": "해당 자료가 없습니다.",
            "message": "해당 자료가 없습니다.",
            "final": True,
            "meta": {
                "row_count": 0,
                "row_count_total": 0,
                "result_status": "no_data",
                "source_call_count": 1,
                "summary_md": summary_md,
                "registered_erp_table": True,
                "semantic_styled_max_rows": 300,
            },
        }
    payload = build_result_payload(
        table=table,
        title=title,
        action=title,
        params=params,
        df=df,
        message=f"{title} {row_count:,}건",
    )
    meta = dict(payload.get("meta") or {})
    meta.update(
        {
            "row_count": row_count,
            "row_count_total": row_count,
            "result_status": "success",
            "source_call_count": 1,
            "summary_md": summary_md,
            "registered_erp_table": True,
            "semantic_styled_max_rows": 300,
        }
    )
    payload["meta"] = meta
    return payload




def calendar_month_period(today=None):
    today = today or date.today()
    year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return date(year, month, min(today.day, monthrange(year, month)[1])), today


def query_scope(permission, *, expected=None, export=False, fresh=True):
    from app.ui import ssai_login as login
    from app.services.external_api_session import marker

    user, company = login.get_current_user(), login.get_selected_company()
    needed = {permission, "EXPORT_EXCEL"} if export else {permission}
    denied = "조회 권한 또는 회사·사용자 세션이 변경되었습니다."
    if (not user or not company or login.st.session_state.get(login.SESSION_COMPANY_PICK_MODE)
            or not needed.issubset(login.get_current_permissions())):
        raise PermissionError(denied)
    scope = {"company_id": int(company["company_id"]), "user_id": str(user.user_id),
             "epoch": marker(login.st.session_state)}
    if expected is not None and expected != scope:
        raise PermissionError(denied)
    if fresh:
        from app.services.external_api_management import load_current_api_session
        session = load_current_api_session()
        if (not session or not needed.issubset(session.permissions) or
            (session.company_id, str(session.user_id), session.epoch) != tuple(scope.values())):
            raise PermissionError(denied)
    return scope


def scoped_action(action):
    from app.sims.meta.erp_table_feature_registry import get_action_spec
    spec = get_action_spec(action)
    return spec[0].required_permission if spec and spec[0].scope_bound else None


def allow_result(action, meta, *, export=False):
    permission = scoped_action(action)
    if not permission:
        return not (meta or {}).get("query_scope")
    scope = (meta or {}).get("query_scope")
    if not scope:
        return False
    try:
        query_scope(permission, expected=scope, export=export)
    except Exception:
        # A failed authority lookup never grants access to a cached result.
        return False
    return True


def _day(value):
    text = value.isoformat() if isinstance(value, date) else str(value)
    pattern = "%Y%m%d" if re.fullmatch(r"[0-9]{8}", text) else "%Y-%m-%d"
    return datetime.strptime(text, pattern).strftime("%Y%m%d")


def normalize_detail_params(spec, raw, *, today=None):
    raw = dict(raw or {})
    unknown = raw.keys() - {"date_role", "date_from", "date_to", "filters", "direction",
                            "_display_context", "display_top", "top"}
    if unknown:
        raise ValueError("지원하지 않는 조회조건입니다.")
    if ("date_from" in raw) != ("date_to" in raw):
        raise ValueError("시작일과 종료일을 함께 입력해 주세요.")
    start, end = calendar_month_period(today)
    role = raw.get("date_role", spec["dates"][0])
    start, end = _day(raw.get("date_from", start)), _day(raw.get("date_to", end))
    if role not in spec["dates"] or start > end:
        raise ValueError("조회 기준일 또는 날짜 범위를 확인해 주세요.")
    values = {}
    filters = raw.get("filters", {})
    if not isinstance(filters, dict) or filters.keys() - spec["filters"].keys():
        raise ValueError("지원하지 않는 조회조건입니다.")
    for key, val in filters.items():
        if val is None or val == "":
            continue
        column, kind, size = spec["filters"][key]
        if kind in ("integer", "money"):
            def convert(v):
                try:
                    n = Decimal(str(v).strip())
                except InvalidOperation:
                    raise ValueError("숫자를 확인해 주세요.") from None
                lo, hi = (-32768, 32767) if kind == "integer" else (-(10**18-1), 10**18-1)
                if not n.is_finite() or n != n.to_integral_value() or not lo <= n <= hi:
                    raise ValueError("정수 범위를 확인해 주세요.")
                return int(n)
            if isinstance(val, (tuple, list)):
                if key.endswith(("_from", "_to")) or len(val) != 2 or convert(val[0]) > convert(val[1]):
                    raise ValueError("숫자 범위를 확인해 주세요.")
                val = tuple(convert(v) for v in val)
            else:
                val = convert(val)
        elif kind == "date":
            val = _day(val)
        else:
            if not isinstance(val, str) or len(val.strip()) > size:
                raise ValueError("조회조건 형식을 확인해 주세요.")
            val = val.strip()
        if val != "":
            values[key] = val
    for stem in ("amount", "add_date", "mod_date"):
        lo, hi = values.get(stem+"_from"), values.get(stem+"_to")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("시작값과 종료값의 순서를 확인해 주세요.")
    direction = raw.get("direction", "all")
    if direction != "all" and (direction not in ("receipt", "payment") or not spec.get("direction_sql")):
        raise ValueError("지원하지 않는 계열입니다.")
    if direction != "all" and "flow_code" in values:
        code = values["flow_code"]
        if not code.isascii() or not code.isalnum() or (code < spec["direction_boundary"]) != (direction == "receipt"):
            raise ValueError("입금/출금 구분과 구분코드가 일치하지 않습니다.")
    return {**raw, "date_from": start, "date_to": end, "date_role": role,
            "direction": direction, "filters": values}


def build_detail_select(spec, params):
    from app.db.schema_map import SCHEMA
    limits = registered_query_limits(params)
    limit = min(limits.source_top, limits.export_limit_rows)
    day = spec["date_columns"][params["date_role"]]
    clauses, args = [f"S.[{day}] BETWEEN ? AND ?"], [params["date_from"], params["date_to"]]
    for key, value in sorted(params["filters"].items()):
        expr, kind, _ = spec["filters"][key]
        if isinstance(value, tuple):
            clauses.append(expr + " BETWEEN ? AND ?")
            args.extend(value)
        elif kind == "name":
            # MSSQL LIKE metacharacters in a name are data, not search operators.
            literal = value.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
            clauses.append(expr + " LIKE ?")
            args.append(like_value(literal))
        else:
            operator = " >= ?" if key.endswith("_from") else " <= ?" if key.endswith("_to") else " LIKE ?" if kind == "text" else " = ?"
            clauses.append(expr + operator)
            args.append(like_value(value) if kind == "text" else value)
    if params["direction"] != "all":
        clauses.append(spec["direction_sql"] + (" < ?" if params["direction"] == "receipt" else " >= ?"))
        args.append(spec["direction_boundary"])
    columns = [f"S.[{f.column}]" for f in spec["fields"]] + list(spec["extra_select"])
    order = ", ".join(f"S.[{k}]" for k in spec["pk"])
    sql = (f"SELECT TOP ({limit+1}) " + ", ".join(columns) + " FROM " + SCHEMA["tables"][spec["table"]]
           + " S " + " ".join(spec["joins"]) + " WHERE " + " AND ".join(clauses) + " ORDER BY " + order)
    return sql, tuple(args), limit, limits.display_top


def read_detail_result(spec, params, *, export=False, expected_scope=None):
    from app.services.utils import apply_labels
    from app.db.labels_map import LABELS

    sql, args, limit, display = build_detail_select(spec, params)
    permission = scoped_action(spec["title"])
    if not permission:
        raise PermissionError("등록된 조회 권한이 없습니다.")
    scope = query_scope(permission, expected=expected_scope, export=export)
    frame = execute_bound_select(sql, args, company_id=scope["company_id"])
    query_scope(permission, expected=scope, export=export)
    if len(frame) > limit:
        raise ValueError(f"조회 상한 {limit:,}건을 초과했습니다. 조회조건을 좁혀 주세요.")
    if (not set(f.column for f in spec["fields"]).issubset(frame.columns)
            or not frame.columns.is_unique or frame[list(spec["pk"])].isna().any().any()
            or frame.duplicated(list(spec["pk"])).any()):
        raise ValueError("원본 필드 또는 원본 키를 확인해 주세요.")
    for f in spec["fields"]:
        if f.kind == "decimal":
            for v in frame[f.column]:
                if v is not None and (type(v) not in (Decimal, int) or not Decimal(v).is_finite()
                                      or v != int(v) or abs(v) >= 10**18):
                    raise ValueError("금액 정밀도를 확인할 수 없습니다.")
    labels = LABELS[spec["table"]]
    full = apply_labels(frame, spec["table"])
    if full.empty:
        full = full.rename(columns=labels)
    full = full.loc[:, list(dict.fromkeys([c for c in spec["leading"] if c in full] + list(full.columns)))]
    if export:
        return full
    business_day = labels[spec["date_columns"][params["date_role"]]]
    criteria = [f'{business_day}: {params["date_from"]} ~ {params["date_to"]}']
    if params["direction"] != "all":
        criteria.append("입금" if params["direction"] == "receipt" else "출금")
    criteria.extend(f"{spec['filter_labels'][k]}: {v}" for k, v in params["filters"].items())
    summary = " / ".join(criteria)
    shown = full.head(display).copy()
    out = build_feature_result(table=spec["table"], title=spec["title"], params=params, df=shown, summary_md=summary)
    out.update(df=full, df_display=shown, columns=list(full.columns), records=shown.to_dict("records"))
    meta = out["meta"]
    meta.update(query_scope=scope, exact_columns=[f.label for f in spec["fields"] if f.kind == "decimal"],
                integer_columns=[f.label for f in spec["fields"] if f.kind == "smallint"],
                date_columns=[f.label for f in spec["fields"] if f.size == 8 and f.kind == "char"],
                analysis_roles={"date": business_day, "amount": spec["amount_label"], "vendor": "거래처명",
                                "category": spec["category_label"],
                                "staff": spec["filter_labels"].get("vendor_staff_name", ""),
                                "aliases": spec.get("group_aliases", {}),
                                "codes": [spec["filter_labels"][key] for key, (_, kind, _) in spec["filters"].items()
                                          if kind == "code" and spec["filter_labels"][key] in full],
                                "distinct_terms": spec.get("distinct_terms", {}),
                                "dimensions": [spec["filter_labels"][key] for key, (_, kind, _) in spec["filters"].items()
                                               if kind == "name" and spec["filter_labels"][key] in full]
                                              + [col for col in ("여수신구분", "입금/출금 구분") if col in full],
                                "split": "입금/출금 구분" if spec["direction_sql"] else "",
                                "split_values": ["입금", "출금"] if spec["direction_sql"] else []},
                pinned_columns=list(spec["leading"][:2]), query_summary=summary,
                row_count_total=len(full), row_count=len(full), displayed_rows=len(shown), row_count_display=len(shown),
                export_row_count=len(full), full_source_verified=True, source_table=spec["table"])
    return out
