from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ui.knowledge_chat_adapter import parse_business_help_knowledge_request  # noqa: E402


def main() -> None:
    help_phrases = (
        "SSAI 관련 프롬프트 알려줘",
        "SIMS 관련 프롬프트 알려줘",
        "SSAI에서 뭘 물어볼 수 있어?",
        "SIMS에서 뭘 물어볼 수 있어?",
        "질문 예시 알려줘",
        "SSAI 사용법 알려줘",
        "SIMS 사용법 알려줘",
        "SSAI에서 어떤 질문을 할 수 있어?",
        "SIMS AI에서 어떤 질문을 할 수 있어?",
    )
    for phrase in help_phrases:
        route = parse_business_help_knowledge_request(phrase)
        assert route is not None and route.retrieval_query == "업무질문 도움말", phrase

    ordinary_inputs = (
        "계약단가 조회",
        "발주 조회",
        "입고예정 조회",
        "오늘 입고현황",
        "현재표 거래처별 매출금액 분석",
        "/knowledge 계약단가 사용법",
        "/knowledge-tech Rddbc110",
        "현재 시간 알려줘",
        "SIMS 관련 질문이 있어요",
    )
    for ordinary_input in ordinary_inputs:
        assert parse_business_help_knowledge_request(ordinary_input) is None, ordinary_input

    main_source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    assert "parse_business_help_knowledge_request(user_input)" in main_source
    assert "_run_business_help_knowledge_chat(" in main_source
    assert "parse_sims_help_request" not in main_source
    assert "_run_sims_help_chat" not in main_source
    assert "sims_help_route" not in main_source
    assert not (ROOT / "app" / "ui" / "sims_help_adapter.py").exists()
    assert main_source.count("SIMS 업무를 더 잘 이해하고 도와드리겠습니다.") == 2
    assert "SSAI 업무를 더 잘 이해하고 도와드리겠습니다." not in main_source
    assert "### 🧩 SSAI 모드" in main_source
    assert 'st.toggle("SSAI 패널 열기"' in main_source
    assert "## 🧩 SSAI 결과" in main_source
    print(f"RESULT OK tests={len(help_phrases) + len(ordinary_inputs) + 11}")


if __name__ == "__main__":
    main()
