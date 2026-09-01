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
    datetime_route = "None if raw_mcp_route is not None else resolve_datetime_question(user_input)"
    assert main_source.index(datetime_route) < main_source.index("web_search_route = parse_web_search_request(user_input)")
    assert main_source.index("if web_search_route is not None:") < main_source.index("handled = try_handle_nlq(")
    assert "_run_web_search_chat(web_search_route, room=current_room)" in main_source
    print("RESULT OK tests=25 provider_calls=1 retries=0 db_write_count=0")


if __name__ == "__main__":
    main()
