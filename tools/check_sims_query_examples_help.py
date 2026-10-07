"""Offline contract for deterministic SIMS example help."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.sims.nlq.action_inventory import implemented_actions  # noqa: E402
from app.ui.knowledge_chat_adapter import (  # noqa: E402
    _SIMS_QUERY_EXAMPLE_CATALOG,
    parse_sims_query_examples_request,
    sims_query_examples_text,
)


def main() -> None:
    help_inputs = (
        "SIMS 조회 예시 알려줘",
        "SIMS 사용 예시 알려줘",
        "SIMS 조회 사례 보여줘",
        "SIMS 사용법 알려줘",
        "심스 조회 예시 알려줘",
        "심스 사용 예시 알려줘",
    )
    for value in help_inputs:
        assert parse_sims_query_examples_request(value), value
    protected = (
        "SIMS 일일점검",
        "심스 일일점검",
        "SIMS 일일점검 제약사 삼진",
        "현재고 제조사명 한미",
        "제품정보 조회",
        "일반약 계약단가 조회",
        "조회 사례 보여줘",
    )
    for value in protected:
        assert not parse_sims_query_examples_request(value), value

    supported = {spec.canonical_action for spec in implemented_actions()}
    for _, entries in _SIMS_QUERY_EXAMPLE_CATALOG:
        for action, _ in entries:
            assert action in supported, action
    expected = sims_query_examples_text(allowed_actions=supported)
    assert expected == sims_query_examples_text(allowed_actions=supported)
    assert "[일일점검]" in expected and "[현재표 후속 질문]" in expected
    assert "제품수불현황 제품 00269 2024~2026 조회" in expected
    assert "Rddbc" not in expected and "router" not in expected and "DB" not in expected
    assert sims_query_examples_text(allowed_actions={"SIMS 일일점검"}).count("- ") == 3

    main_source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    assert main_source.index("sims_query_examples_help = (") < main_source.index(
        "parse_business_help_knowledge_request(user_input)"
    )
    assert main_source.index("if sims_query_examples_help:") < main_source.index(
        "if business_help_knowledge_route is not None:"
    )
    branch = main_source.split("if sims_query_examples_help:", 1)[1].split(
        "if incomplete_sims_help is not None:", 1
    )[0]
    assert "sims_query_examples_text(" in branch
    assert "try_handle_nlq(" not in branch
    assert "_select_approved_sims_help_action(" not in branch
    assert "_run_business_help_knowledge_chat(" not in branch
    print(f"RESULT OK help={len(help_inputs)} protected={len(protected)} catalog_actions={len(supported)}")


if __name__ == "__main__":
    main()
