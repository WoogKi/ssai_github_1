# app/db/sql_utils.py
# -*- coding: utf-8 -*-
from __future__ import annotations

def sql_safe_int(expr: str) -> str:
    """
    SQL Server 구버전 호환(TRY_CONVERT/TRY_CAST 미사용):
    문자열(또는 숫자형도 포함)을 '안전하게' INT로 변환하는 SQL 표현식 생성.
    - NULL/공백/숫자 아닌 문자가 섞이면 NULL
    - 숫자만 있으면 INT로 CAST
    """
    e = (expr or "").strip()
    if not e:
        return "NULL"

    # 숫자형 컬럼이어도 안전하게 문자열로 다룰 수 있도록 CAST to NVARCHAR
    s = f"LTRIM(RTRIM(CAST({e} AS NVARCHAR(100))))"
    return (
        f"(CASE "
        f"WHEN {s} IS NULL OR {s} = '' THEN NULL "
        f"WHEN {s} LIKE '%[^0-9]%' THEN NULL "
        f"ELSE CAST({s} AS INT) END)"
    )
