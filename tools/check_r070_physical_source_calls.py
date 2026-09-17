"""Read-only R070 authority/source-call audit; never retries or writes ERP data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.mssql_client import read_only_request, set_current_company_id
from app.services.rddbc070_service import get_rddbc070_current_result


def _query_summary(state: dict) -> list[dict]:
    def table_names(value) -> list[str]:
        if isinstance(value, str):
            return [name.strip() for name in value.split(",") if name.strip()]
        return [str(name) for name in (value or [])]

    return [
        {
            "tables": table_names(row.get("tables")),
            "status": str(row.get("status") or ""),
            "rows": int(row.get("rows") or 0),
            "elapsed_ms": int(row.get("elapsed_ms") or 0),
        }
        for row in state.get("queries") or []
    ]


def _unlabeled(company_id: int, phrase: str) -> dict:
    set_current_company_id(company_id)
    with read_only_request(timeout_seconds=120) as state:
        payload = get_rddbc070_current_result({"_r070_unlabeled_name": phrase, "top": 100})
    meta = dict(payload.get("meta") or {})
    resolution_status = str(meta.get("entity_resolution_status") or "")
    frame = payload.get("df")
    row_count = len(frame) if frame is not None else 0
    duplicate_grain_rows = 0
    if frame is not None and {"단가적용거래처", "제품코드"}.issubset(frame.columns):
        duplicate_grain_rows = int(
            frame.duplicated(subset=["단가적용거래처", "제품코드"], keep=False).sum()
        )
    authority_matches = list(meta.get("authority_matches") or [])
    return {
        "case": phrase,
        "company_id": company_id,
        "resolution_status": resolution_status,
        "resolved_kind": meta.get("resolved_kind"),
        "candidate_count": int(meta.get("candidate_count") or 0),
        "authority_matches": authority_matches,
        "matched_cost_apply_count": sum(
            int(row.get("match_count") or 0)
            for row in authority_matches if row.get("match_type") == "cost_apply"
        ),
        "matched_manufacturer_count": sum(
            int(row.get("match_count") or 0)
            for row in authority_matches if row.get("match_type") == "manufacturer"
        ),
        "matched_product_count": sum(
            int(row.get("match_count") or 0)
            for row in authority_matches if row.get("match_type") == "product"
        ),
        "physical_select_count": len(state.get("queries") or []),
        "logical_source_call_count": meta.get("source_call_count"),
        "final_rows": row_count if resolution_status == "resolved" else 0,
        "duplicate_grain_rows": duplicate_grain_rows,
        "source_limit_hit": bool(meta.get("full_source_limit_hit")),
        "queries": _query_summary(state),
    }


def _explicit(company_id: int, label: str, params: dict) -> dict:
    set_current_company_id(company_id)
    with read_only_request(timeout_seconds=120) as state:
        payload = get_rddbc070_current_result(params)
    return {
        "case": label,
        "company_id": company_id,
        "resolution_status": "explicit_or_none",
        "candidate_count": 0,
        "physical_select_count": len(state.get("queries") or []),
        "logical_source_call_count": payload.get("meta", {}).get("source_call_count"),
        "final_rows": len(payload.get("df")) if payload.get("df") is not None else 0,
        "queries": _query_summary(state),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    reports = [
        _unlabeled(7, "50002"),
        _unlabeled(7, "이가탄"),
        _unlabeled(7, "보덕"),
        _unlabeled(7, "보덕메디팜"),
        _unlabeled(8, "태응약품"),
        _unlabeled(7, "존재하지않는단가적용처fixture"),
        _unlabeled(7, "999999999999"),
        _explicit(7, "단가적용처코드 50002 fixture", {"ven_cd": "50002", "top": 100}),
        _explicit(7, "제품코드 fixture", {"physic_cd": "64063", "top": 100}),
        _explicit(7, "제품명 fixture", {"physic_nm": "알잘정", "top": 100}),
        _explicit(7, "제조사 fixture", {"maker_nm": "삼일", "top": 100}),
        _explicit(7, "조건 없음 fixture", {"top": 100}),
    ]
    text = json.dumps(reports, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
