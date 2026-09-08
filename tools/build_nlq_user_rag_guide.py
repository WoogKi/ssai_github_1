"""Build the user-facing SSAI business-question guide from the official casebook."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASEBOOK = ROOT / "docs" / "NLQ 정리 20260807.xlsx"
DEFAULT_OUTPUT = ROOT / "docs" / "03_runbook" / "SIMS_AI_업무질문_사용_예시.md"

SECTIONS = (
    {
        "source_action": "최종 계약단가 조회",
        "heading": "계약단가",
        "description": "제품, 제약사, 단가적용처 등을 조건으로 기준일까지 적용되는 최신 계약단가를 조회할 수 있습니다.",
        "conditions": (
            "제품코드 또는 제품명", "제약사", "단가적용처 코드 또는 이름",
            "제품그룹", "제품구분", "제품분류", "보험코드", "바코드",
            "전문약·일반약·ETC·OTC",
        ),
        "combination": "제품과 제약사, 단가적용처처럼 여러 조건을 한 문장에 함께 적을 수 있습니다.",
        "difference": "현재 적용되는 단가가 아니라 과거 변경내역이 필요하면 질문에 `계약단가 이력`을 명시합니다.",
    },
    {
        "source_action": "최종 매입단가 조회",
        "heading": "최종 매입단가",
        "description": "제품과 재고 조건에 맞는 현재 최종 매입단가와 관련 수량을 조회할 수 있습니다.",
        "conditions": (
            "제품코드 또는 제품명", "매입처", "재고위치", "재고적용처",
            "단가적용처", "전문약·일반약·ETC·OTC",
        ),
        "combination": "제품, 매입처, 재고위치와 적용처 조건을 필요한 만큼 조합할 수 있습니다.",
        "difference": "거래처와 합의한 계약단가를 찾을 때는 `계약단가 조회`라고 질문합니다.",
    },
    {
        "source_action": "발주조회",
        "heading": "발주 조회",
        "description": "발주일자, 발주처, 제품, 발주상태 등을 조건으로 발주내역을 조회할 수 있습니다.",
        "conditions": (
            "날짜 또는 기간", "발주처", "제품코드 또는 제품명",
            "전문약·일반약·ETC·OTC", "발주상태", "미입고 여부",
            "단가적용처", "재고적용처", "재고위치",
        ),
        "combination": "기간, 발주처, 제품 종류를 함께 적으면 원하는 범위로 좁혀 조회할 수 있습니다.",
        "difference": "현재 들어올 예정인 미입고 발주만 보려면 `입고예정 조회`를 사용합니다.",
    },
    {
        "source_action": "입고예정조회",
        "heading": "입고예정 조회",
        "description": "오늘과 최근 영업일에 발주되어 현재 입고가 예정된 내역을 조회할 수 있습니다.",
        "conditions": (
            "발주처", "제품코드 또는 제품명", "전문약·일반약·ETC·OTC",
            "단가적용처", "재고적용처",
        ),
        "combination": "발주처와 제품 종류처럼 여러 조건을 함께 적어 입고예정 범위를 좁힐 수 있습니다.",
        "difference": "완료된 건을 포함한 일반 발주내역이 필요하면 `발주 조회`라고 질문합니다.",
    },
)


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def build(casebook: Path, output: Path) -> tuple[Path, Path, dict[str, int]]:
    frame = pd.read_excel(casebook, sheet_name="NLQ 사례", dtype=str).fillna("")
    required = {"질문", "기준 기능 (action)", "의도일치"}
    if not required.issubset(frame.columns):
        raise ValueError(f"official casebook columns missing: {sorted(required - set(frame.columns))}")

    lines = [
        "# SSAI 업무질문 사용 예시",
        "",
        "SSAI에서 어떤 질문을 할 수 있는지, 어떤 업무를 조회할 수 있는지 안내하는 도움말입니다.",
        "아래 문장을 그대로 입력하거나 제품, 제약사, 발주처, 적용처와 날짜를 바꾸어 질문할 수 있습니다.",
        "조건은 한 가지만 사용해도 되고, 필요한 조건을 한 문장에 여러 개 함께 적어도 됩니다.",
        "",
        "도움말이 필요할 때는 `계약단가는 어떻게 물어보면 돼?`, `발주 조회 질문 예시 보여줘`처럼 질문할 수 있습니다.",
        "",
    ]
    counts: dict[str, int] = {}
    for section in SECTIONS:
        action = section["source_action"]
        rows = frame[
            (frame["기준 기능 (action)"].map(_clean) == action)
            & (frame["의도일치"].map(_clean).str.upper() == "PASS")
        ]
        questions = list(dict.fromkeys(_clean(value) for value in rows["질문"] if _clean(value)))
        counts[action] = len(questions)
        lines.extend((
            f"## {section['heading']}",
            "",
            str(section["description"]),
            "",
            "### 사용할 수 있는 주요 조건",
            "",
        ))
        lines.extend(f"- {condition}" for condition in section["conditions"])
        lines.extend((
            "",
            "### 여러 조건으로 조회하기",
            "",
            str(section["combination"]),
            "",
            "### 질문 예시",
            "",
        ))
        lines.extend(f"- `{question}`" for question in questions)
        lines.extend((
            "",
            "### 비슷한 조회와의 차이",
            "",
            str(section["difference"]),
        ))
        lines.append("")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    plan = output.with_suffix(".knowledge.json")
    plan.write_text(json.dumps({"items": [{
        "source_kind": "DOCUMENT",
        "source_name": "SSAI_업무질문_사용_예시.md",
        "source_key": "document:sims-ai-business-question-examples",
        "content_file": output.name,
        "scope": "GLOBAL",
        "company_id": None,
        "user_id": None,
        "version": 2,
        "knowledge_classification": "GENERAL",
        "search_aliases": [
            "SSAI에서 어떤 질문을 할 수 있어",
            "어떤 업무를 조회할 수 있어",
            "SSAI 질문 예시",
            "업무질문 도움말",
            "계약단가 조회 예시",
            "계약단가는 어떻게 물어보면 돼",
            "최종 매입단가 조회 예시",
            "최종 매입단가는 어떻게 조회해",
            "발주 조회 예시",
            "발주 조회는 어떻게 질문해",
            "입고예정 조회 예시",
            "입고예정은 어떻게 물어봐",
            "단가적용처로 조회하는 방법",
            "재고적용처로 조회하는 방법",
            "전문약만 조회하는 방법",
            "SIMS AI에서 어떤 질문을 할 수 있어",
            "SIMS에서 뭘 물어볼 수 있어",
            "SIMS 관련 프롬프트 알려줘",
            "SIMS 사용법 알려줘",
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
