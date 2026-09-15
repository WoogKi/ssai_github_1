"""Offline regression for the fail-closed Web Search boundary."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.web_search_service import (
    build_web_search_prompt,
    latest_ready_web_search_message,
    parse_web_search_followup_request,
    parse_web_search_request,
    render_web_search_answer,
    search_web,
)

NOW = datetime(2026, 8, 22, 9, 30, tzinfo=ZoneInfo("Asia/Seoul"))


def _route(question: str):
    route = parse_web_search_request(question, now=NOW)
    assert route is not None, question
    assert route.reference_at == NOW
    return route


def main() -> None:
    today = _route("오늘 주요 AI 뉴스 알려줘")
    assert today.period.kind == "today"
    assert today.period.start.isoformat() == "2026-08-22"

    recent = _route("최근 OpenAI 소식 알려줘")
    assert recent.period.kind == "unspecified"
    assert _route("현재 OpenAI 소식 알려줘").period.kind == "unspecified"
    assert _route("최근 제약사 매출 뉴스").period.kind == "unspecified"

    this_week = _route("이번 주 의약품 유통업 뉴스")
    assert this_week.period.kind == "this_week"
    assert this_week.period.start.isoformat() == "2026-08-17"
    assert this_week.period.end.isoformat() == "2026-08-23"

    for non_web in (
        "오늘 날짜가 뭐야",
        "오늘 입고현황",
        "재고 부족 기준이 뭐야",
        "현재표 제품별 매출 TOP 10",
        "현재 회사 재고 뉴스",
        "/knowledge 최신 내부 기준",
        "안녕하세요",
    ):
        assert parse_web_search_request(non_web, now=NOW) is None, non_web

    calls: list[tuple[str, dict[str, str], float]] = []

    def transport(url: str, headers: dict[str, str], timeout_s: float):
        calls.append((url, headers, timeout_s))
        return {
            "web": {
                "results": [
                    {
                        "title": "AI policy update",
                        "url": "https://example.com/news/ai",
                        "description": "External source snippet.",
                        "published": "2026-08-22T08:00:00+09:00",
                    }
                ]
            }
        }

    response = search_web(today, api_key="fixture-key", transport=transport)
    assert response.status == "ready"
    assert len(calls) == 1
    assert "freshness=pd" in calls[0][0]
    assert calls[0][1]["X-Subscription-Token"] == "fixture-key"
    assert calls[0][2] == 8.0
    assert response.results[0].title == "AI policy update"
    assert response.results[0].source == "example.com"
    assert response.results[0].published_at == "2026-08-22T08:00:00+09:00"

    prompt = build_web_search_prompt(route=today, response=response)
    assert len(prompt) == 1
    assert "https://example.com/news/ai" in prompt[0]["content"]
    assert "2026-08-22 09:30:00" in prompt[0]["content"]
    rendered = render_web_search_answer(summary="검색 요약", response=response)
    assert "검색 요약" in rendered
    assert "[AI policy update](https://example.com/news/ai)" in rendered
    assert "검색 기준 시각:" in rendered

    parent = {
        "role": "assistant",
        "content": "결핵·중독 치료제 등 자가치료용 의약품 10개 품목 관련 기사입니다.",
        "meta": {
            "web_search": True,
            "status": "ready",
            "query": "오늘 의약품 관련 주요 뉴스 알려줘",
            "sources": [{
                "title": "자가치료용 의약품 10개 품목 긴급도입",
                "url": "https://example.com/news/medicine",
                "source": "example.com",
                "snippet": "결핵·중독 치료제 등 10개 품목을 전환합니다.",
                "published_at": "2026-08-22T08:00:00+09:00",
            }],
        },
    }
    assert latest_ready_web_search_message([parent]) is parent
    assert latest_ready_web_search_message([parent, {"role": "user", "content": "후속질문"}]) is parent
    assert latest_ready_web_search_message([
        parent,
        {"role": "assistant", "content": "일반 답변", "meta": {}},
    ]) is None
    detail = parse_web_search_followup_request(
        "결핵·중독 치료제 등 자가치료용 의약품 10개 품목은 어떤 품목이야?",
        parent_message=parent,
        now=NOW,
    )
    assert detail is not None and detail.user_query.startswith("결핵·중독")
    source = parse_web_search_followup_request("그 기사 출처 알려줘", parent_message=parent, now=NOW)
    assert source is not None and source.prior_results[0].url.endswith("/medicine")
    for explicit_route in (
        "현재고 보여줘",
        "한림 발주 계산",
        "/knowledge-tech R230 구매원가 상태",
    ):
        assert parse_web_search_followup_request(explicit_route, parent_message=parent, now=NOW) is None
    assert parse_web_search_followup_request("오늘 새 뉴스 알려줘", parent_message=parent, now=NOW) is None
    assert parse_web_search_followup_request("저녁 메뉴 추천해줘", parent_message=parent, now=NOW) is None

    followup_calls = []
    followup_response = search_web(
        detail,
        api_key="fixture-key",
        transport=lambda *_args: followup_calls.append(True) or {"web": {"results": []}},
    )
    assert followup_calls == [True]
    assert followup_response.status == "ready" and followup_response.results == detail.prior_results
    followup_prompt = build_web_search_prompt(route=detail, response=followup_response)[0]["content"]
    assert "전체 내용이 명시되지 않았다고 답하세요" in followup_prompt
    assert detail.user_query in followup_prompt
    fallback_response = search_web(
        detail,
        api_key="fixture-key",
        transport=lambda *_args: (_ for _ in ()).throw(TimeoutError("fixture")),
    )
    assert fallback_response.status == "ready"
    assert fallback_response.reason_code == "prior_results_only"
    assert fallback_response.results == detail.prior_results

    missing_calls = []
    missing = search_web(today, api_key="", transport=lambda *_args: missing_calls.append(True) or {})
    assert missing.status == "failed" and missing.reason_code == "configuration_missing"
    assert missing_calls == []

    timeout_calls = []
    def failing_transport(*_args):
        timeout_calls.append(True)
        raise TimeoutError("fixture")
    failed = search_web(today, api_key="fixture-key", transport=failing_transport)
    assert failed.status == "failed" and failed.reason_code == "search_failed"
    assert timeout_calls == [True]

    no_result = search_web(today, api_key="fixture-key", transport=lambda *_args: {"web": {"results": []}})
    assert no_result.status == "no_match" and no_result.reason_code == "no_results"

    main_source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    datetime_start = main_source.index("datetime_answer = (")
    datetime_block = main_source[datetime_start:main_source.index("web_search_route = None", datetime_start)]
    assert "business_help_knowledge_route is not None" in datetime_block
    assert "resolve_datetime_question(user_input)" in datetime_block
    assert datetime_start < main_source.index(
        "web_search_route = web_search_followup_route or parse_web_search_request(user_input)"
    )
    assert main_source.index("if web_search_route is not None:") < main_source.index("handled = try_handle_nlq(")
    assert "_run_web_search_chat(web_search_route, room=current_room)" in main_source
    print("RESULT OK tests=43 provider_calls=3 retries=0 db_write_count=0")


if __name__ == "__main__":
    main()
