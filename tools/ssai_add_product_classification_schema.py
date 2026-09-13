"""Install the product-classification authority table in the shared SSAI DB.

Dry-run is the default. The schema is applied only with an explicit ``--apply``
after the common management DB target has been reviewed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ssai_auth_service import connect_ssai_db  # noqa: E402
from app.services.ssai_product_classification_repository import MAPPING_TABLE  # noqa: E402


CREATE_MAPPING_TABLE_SQL = f"""
CREATE TABLE dbo.{MAPPING_TABLE} (
    mapping_id BIGINT IDENTITY(1,1) NOT NULL
        CONSTRAINT PK_{MAPPING_TABLE} PRIMARY KEY,
    company_id INT NOT NULL,
    target_type NVARCHAR(40) NOT NULL,
    target_gcode NVARCHAR(40) NOT NULL,
    target_code NVARCHAR(100) NOT NULL,
    classification NVARCHAR(40) NOT NULL,
    adjustment_only BIT NOT NULL CONSTRAINT DF_{MAPPING_TABLE}_adjustment_only DEFAULT (0),
    reason_code NVARCHAR(100) NOT NULL,
    status NVARCHAR(20) NOT NULL CONSTRAINT DF_{MAPPING_TABLE}_status DEFAULT (N'draft'),
    effective_from DATE NOT NULL,
    effective_to DATE NULL,
    mapping_version NVARCHAR(100) NOT NULL,
    created_by NVARCHAR(100) NOT NULL,
    created_at DATETIME2(3) NOT NULL CONSTRAINT DF_{MAPPING_TABLE}_created_at DEFAULT SYSUTCDATETIME(),
    updated_by NVARCHAR(100) NOT NULL,
    updated_at DATETIME2(3) NOT NULL CONSTRAINT DF_{MAPPING_TABLE}_updated_at DEFAULT SYSUTCDATETIME(),
    approved_by NVARCHAR(100) NULL,
    approved_at DATETIME2(3) NULL,
    CONSTRAINT CK_{MAPPING_TABLE}_target_type CHECK
        (target_type IN (N'product_override', N'product_group', N'product_di', N'product_class', N'product_flag')),
    CONSTRAINT CK_{MAPPING_TABLE}_classification CHECK
        (classification IN (N'commercial', N'financial_adjustment', N'management_only', N'mixed', N'unknown')),
    CONSTRAINT CK_{MAPPING_TABLE}_status CHECK
        (status IN (N'draft', N'approved', N'rejected', N'retired')),
    CONSTRAINT CK_{MAPPING_TABLE}_effective_range CHECK
        (effective_to IS NULL OR effective_to >= effective_from),
    CONSTRAINT CK_{MAPPING_TABLE}_target_shape CHECK
        ((target_type = N'product_override' AND target_gcode = N'') OR
         (target_type <> N'product_override' AND target_gcode <> N'')),
    CONSTRAINT CK_{MAPPING_TABLE}_adjustment_only CHECK
        (adjustment_only = 0 OR classification IN (N'financial_adjustment', N'management_only')),
    CONSTRAINT UQ_{MAPPING_TABLE}_target_version UNIQUE
        (company_id, target_type, target_gcode, target_code, mapping_version)
)
""".strip()

CREATE_LOOKUP_INDEX_SQL = f"""
CREATE INDEX IX_{MAPPING_TABLE}_approved_effective
ON dbo.{MAPPING_TABLE} (company_id, status, effective_from, effective_to)
INCLUDE (target_type, target_gcode, target_code, classification, adjustment_only,
         reason_code, mapping_version, approved_by, approved_at)
""".strip()


def _table_exists(conn: Any) -> bool:
    row = conn.cursor().execute("SELECT OBJECT_ID(?, N'U')", f"dbo.{MAPPING_TABLE}").fetchone()
    return bool(row and row[0])


def _columns(conn: Any) -> list[str]:
    rows = conn.cursor().execute(
        """SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_SCHEMA = N'dbo' AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION""",
        MAPPING_TABLE,
    ).fetchall()
    return [str(row[0]) for row in rows]


def run(*, apply: bool) -> dict[str, object]:
    with connect_ssai_db() as conn:
        exists = _table_exists(conn)
        result: dict[str, object] = {
            "applied": bool(apply),
            "target": "shared_ssai_management_db",
            "table": MAPPING_TABLE,
            "table_exists": exists,
        }
        if exists:
            result["columns"] = _columns(conn)
        if not apply or exists:
            return result
        try:
            cur = conn.cursor()
            cur.execute(CREATE_MAPPING_TABLE_SQL)
            cur.execute(CREATE_LOOKUP_INDEX_SQL)
            conn.commit()
            result["table_created"] = True
            result["columns"] = _columns(conn)
            return result
        except Exception:
            conn.rollback()
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="install shared-DB schema")
    args = parser.parse_args()
    try:
        payload = run(apply=bool(args.apply))
        payload["ok"] = True
    except Exception as exc:
        payload = {"applied": bool(args.apply), "ok": False, "error_type": type(exc).__name__}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
