"""Read-only, bounded DB3 date and negative-adjustment diagnostics."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

import pyodbc

from app.ml.integration import ml_storage_root
from app.services.ssai_auth_service import build_company_conn_str, get_company_db_config


KST = timezone(timedelta(hours=9))
DATES = ("20260920", "20260923", "20260924", "20260925",
         "20260928", "20260929", "20260930", "20261001")

R120_DATES = """
SELECT Rd12_Out_YyMmDd AS out_date, LEFT(LTRIM(RTRIM(Rd12_Io_Gu)), 1) AS io_family,
       COUNT_BIG(*) AS row_count,
       SUM(CAST(ISNULL(Rd12_Quantity, 0) AS decimal(38, 4))) AS quantity,
       SUM(CASE WHEN Rd12_Quantity < 0 THEN 1 ELSE 0 END) AS negative_rows,
       MIN(Rd12_Add_Time) AS first_add_time, MAX(Rd12_Add_Time) AS last_add_time,
       MAX(Rd12_Mod_Time) AS last_mod_time
FROM dbo.Rddbc120 WHERE Rd12_Out_YyMmDd IN ({marks})
GROUP BY Rd12_Out_YyMmDd, LEFT(LTRIM(RTRIM(Rd12_Io_Gu)), 1)
ORDER BY out_date, io_family
"""

R130_DATES = """
SELECT Rd13_Trans_YyMmDd AS out_date, COUNT_BIG(*) AS header_count
FROM dbo.Rddbc130 WHERE Rd13_Trans_Di = '3' AND Rd13_Trans_YyMmDd IN ({marks})
GROUP BY Rd13_Trans_YyMmDd ORDER BY out_date
"""

NEGATIVE_CANDIDATES = """
WITH N AS (
    SELECT TOP (40) Rd12_Out_YyMmDd, Rd12_Ven_Cd, Rd12_Out_Seq,
           Rd12_Physic_Cd, Rd12_Io_Gu, Rd12_Quantity, Rd12_Trans_Di,
           Rd12_Trans_YyMmDd, Rd12_Trans_Seq, Rd12_Add_Time, Rd12_Mod_Time
    FROM dbo.Rddbc120
    WHERE Rd12_Out_YyMmDd >= ? AND Rd12_Out_YyMmDd <= ?
      AND Rd12_Io_Gu LIKE '5%' AND Rd12_Quantity < 0
    ORDER BY Rd12_Out_YyMmDd, Rd12_Ven_Cd, Rd12_Out_Seq
)
SELECT N.Rd12_Out_YyMmDd AS negative_date, N.Rd12_Quantity AS negative_quantity,
       N.Rd12_Add_Time AS negative_add_time, N.Rd12_Mod_Time AS negative_mod_time,
       N.Rd12_Trans_Di AS negative_trans_di, N.Rd12_Trans_YyMmDd AS negative_trans_date,
       N.Rd12_Trans_Seq AS negative_trans_seq,
       P.candidate_count, P.first_candidate_date, P.last_candidate_date,
       P.candidate_trans_di, P.candidate_trans_date, P.candidate_trans_seq
FROM N OUTER APPLY (
    SELECT COUNT_BIG(*) AS candidate_count,
           MIN(O.Rd12_Out_YyMmDd) AS first_candidate_date,
           MAX(O.Rd12_Out_YyMmDd) AS last_candidate_date,
           MIN(O.Rd12_Trans_Di) AS candidate_trans_di,
           MIN(O.Rd12_Trans_YyMmDd) AS candidate_trans_date,
           MIN(O.Rd12_Trans_Seq) AS candidate_trans_seq
    FROM dbo.Rddbc120 AS O
    WHERE O.Rd12_Out_YyMmDd >= ? AND O.Rd12_Out_YyMmDd <= ?
      AND O.Rd12_Ven_Cd = N.Rd12_Ven_Cd AND O.Rd12_Physic_Cd = N.Rd12_Physic_Cd
      AND O.Rd12_Io_Gu = N.Rd12_Io_Gu AND O.Rd12_Quantity = -N.Rd12_Quantity
) AS P
"""


def summarize(daily120: list[dict], daily130: list[dict], negatives: list[dict]) -> dict:
    dates = {}
    for day in DATES:
        groups = [row for row in daily120 if str(row["out_date"]).strip() == day]
        headers = next((int(row["header_count"]) for row in daily130 if str(row["out_date"]).strip() == day), 0)
        dates[day] = {
            "r120_all_rows": sum(int(row["row_count"]) for row in groups),
            "r120_all_quantity": str(sum((row["quantity"] for row in groups), 0)),
            "r120_5xx_rows": sum(int(row["row_count"]) for row in groups if str(row["io_family"]).strip() == "5"),
            "r120_5xx_quantity": str(sum((row["quantity"] for row in groups if str(row["io_family"]).strip() == "5"), 0)),
            "r120_6xx_rows": sum(int(row["row_count"]) for row in groups if str(row["io_family"]).strip() == "6"),
            "r120_6xx_quantity": str(sum((row["quantity"] for row in groups if str(row["io_family"]).strip() == "6"), 0)),
            "r120_negative_rows_all_io": sum(int(row["negative_rows"]) for row in groups),
            "r130_direction3_headers": headers,
            "first_add_time": str(min((row["first_add_time"] for row in groups if row["first_add_time"]), default="")),
            "last_add_time": str(max((row["last_add_time"] for row in groups if row["last_add_time"]), default="")),
            "last_mod_time": str(max((row["last_mod_time"] for row in groups if row["last_mod_time"]), default="")),
            "completeness": "unverified",
        }
    return {"dates": dates, "negative_5xx_sample": {
        "rows": len(negatives),
        "no_opposite_quantity_candidate": sum(int(row["candidate_count"] or 0) == 0 for row in negatives),
        "single_candidate_not_confirmed": sum(int(row["candidate_count"] or 0) == 1 for row in negatives),
        "multiple_candidates": sum(int(row["candidate_count"] or 0) > 1 for row in negatives),
        "single_same_document_candidate_rows": sum(
            int(row["candidate_count"] or 0) == 1
            and (row["negative_trans_di"], row["negative_trans_date"], row["negative_trans_seq"])
            == (row["candidate_trans_di"], row["candidate_trans_date"], row["candidate_trans_seq"])
            for row in negatives
        ),
        "cross_date_candidate_rows": sum(
            int(row["candidate_count"] or 0) > 0
            and (str(row["first_candidate_date"]).strip() != str(row["negative_date"]).strip()
                 or str(row["last_candidate_date"]).strip() != str(row["negative_date"]).strip())
            for row in negatives
        ),
        "confirmed_pairs": 0,
    }}


def run() -> dict:
    cfg = get_company_db_config(3)
    if cfg.company_id != 3:
        raise ValueError("company 3 connection mismatch")
    marks = ", ".join("?" for _ in DATES)
    with pyodbc.connect(build_company_conn_str(cfg), timeout=10, autocommit=True) as connection:
        connection.timeout = 120
        def query(label, sql, params, cap):
            cursor = connection.cursor()
            try:
                cursor.execute(sql, *params)
            except pyodbc.Error as exc:
                raise RuntimeError(f"diagnostic_{label}_{type(exc).__name__}") from None
            names = [item[0] for item in cursor.description]
            rows = [dict(zip(names, row)) for row in cursor.fetchmany(cap + 1)]
            if len(rows) > cap:
                raise ValueError("diagnostic result exceeded row cap")
            return rows
        started = perf_counter()
        daily120 = query("r120_dates", R120_DATES.format(marks=marks), DATES, 80)
        daily130 = query("r130_dates", R130_DATES.format(marks=marks), DATES, 8)
        negatives = query("negative_candidates", NEGATIVE_CANDIDATES,
                          ("20260901", "20260923", "20260401", "20261001"), 40)
    result = summarize(daily120, daily130, negatives)
    result.update({"company_id": 3, "query_count": 3, "duration_ms": int((perf_counter() - started) * 1000),
                   "extracted_at_kst": datetime.now(KST).isoformat(),
                   "scope_warning": "all R120/R130 source; not the current-table display filter"})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-read-only", action="store_true")
    parser.add_argument("--company-id", type=int, required=True)
    args = parser.parse_args()
    if not args.live_read_only or args.company_id != 3:
        parser.error("explicit company 3 read-only scope required")
    try:
        result = run()
        target = ml_storage_root() / "diagnostics" / "db3-dates-20261028.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError("diagnostic already exists")
        target.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"company_id": 3, "query_count": 3, "duration_ms": result["duration_ms"],
                          "diagnostic_path": str(target), "negative_sample": result["negative_5xx_sample"]},
                         ensure_ascii=False))
    except Exception as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith("diagnostic_") else type(exc).__name__
        parser.exit(1, f"DB3 diagnostic stopped: {detail}\n")


if __name__ == "__main__":
    main()
