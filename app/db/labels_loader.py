# app/db/labels_loader.py
"""
Load column labels (MS_Description) from SQL Server extended properties at runtime.
Falls back to static LABELS when DB is unavailable by returning {}.
"""
from functools import lru_cache
from typing import Dict
from app.db.mssql_client import read_df

_SQL = """
SELECT
    t.name  AS table_name,
    c.name  AS column_name,
    CAST(ep.value AS nvarchar(4000)) AS label
FROM sys.tables t
JOIN sys.columns c ON c.object_id = t.object_id
LEFT JOIN sys.extended_properties ep
  ON ep.major_id = t.object_id AND ep.minor_id = c.column_id AND ep.name = 'MS_Description'
WHERE t.schema_id = SCHEMA_ID('dbo') AND t.name = ?;
"""

@lru_cache(maxsize=64)
def load_labels_for_table(table_name: str) -> Dict[str, str]:
    try:
        df = read_df(_SQL, (table_name,))
        if df is None or df.empty:
            return {}
        mapping: Dict[str, str] = {}
        for _, row in df.iterrows():
            col = row["column_name"]
            label = row["label"]
            if isinstance(col, str) and isinstance(label, str) and label.strip():
                mapping[col] = label
        return mapping
    except Exception:
        # DB 연결 불가/오류 시엔 조용히 빈 dict 반환 → 정적 LABELS 사용
        return {}
