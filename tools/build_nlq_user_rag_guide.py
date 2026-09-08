"""Build the user-facing SIMS NLQ guide from the official Excel casebook."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASEBOOK = ROOT / "docs" / "NLQ 정리 20260807.xlsx"
DEFAULT_OUTPUT = ROOT / "docs" / "03_runbook" / "SIMS_AI_업무질문_사용_예시.md"

SECTIONS = (
    ("최종 계약단가 조회", "계약단가 조회", "기준일까지 적용되는 최신 계약단가를 조건에 맞춰 조회합니다."),
    ("최종 매입단가 조회", "최종 매입단가 조회", "제품과 적용처 조건에 맞는 최종 매입단가를 조회합니다."),
    ("발주조회", "발주 조회", "날짜, 발주처, 제품, 상태와 적용처를 조합해 발주 내역을 조회합니다."),
    ("입고예정조회", "입고예정 조회", "오늘을 포함한 최근 영업일의 미입고 발주를 조회합니다."),
)


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def build(casebook: Path, output: Path) -> tuple[Path, Path, dict[str, int]]:
    frame = pd.read_excel(casebook, sheet_name="NLQ 사례", dtype=str).fillna("")
    required = {"질문", "기준 기능 (action)", "의도일치"}
    if not required.issubset(frame.columns):
        raise ValueError(f"official casebook columns missing: {sorted(required - set(frame.columns))}")

    lines = [
        "# SIMS AI 업무질문 사용 예시",
        "",
        "아래 문장을 그대로 입력하거나 제품, 제약사, 발주처, 적용처와 날짜를 바꾸어 질문할 수 있습니다.",
        "",
    ]
    counts: dict[str, int] = {}
    for action, heading, description in SECTIONS:
        rows = frame[
            (frame["기준 기능 (action)"].map(_clean) == action)
            & (frame["의도일치"].map(_clean).str.upper() == "PASS")
        ]
        questions = list(dict.fromkeys(_clean(value) for value in rows["질문"] if _clean(value)))
        counts[action] = len(questions)
        lines.extend((f"## {heading}", "", description, ""))
        lines.extend(f"- `{question}`" for question in questions)
        lines.append("")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    plan = output.with_suffix(".knowledge.json")
    plan.write_text(json.dumps({"items": [{
        "source_kind": "DOCUMENT",
        "source_name": output.name,
        "source_key": "document:sims-ai-business-question-examples",
        "content_file": output.name,
        "scope": "GLOBAL",
        "company_id": None,
        "user_id": None,
        "version": 1,
        "knowledge_classification": "GENERAL",
        "search_aliases": [
            "SIMS AI 질문 예시",
            "계약단가 조회 예시",
            "최종 매입단가 조회 예시",
            "발주 조회 예시",
            "입고예정 조회 예시",
        ],
    }]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output, plan, counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--casebook", type=Path, default=DEFAULT_CASEBOOK)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output, plan, counts = build(args.casebook, args.output)
    print(json.dumps({"output": str(output), "plan": str(plan), "counts": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
