"""Small, fail-closed external web-search boundary for ordinary Chat."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import os
import re
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from app.services.datetime_tool import OPERATING_TIMEZONE_NAME, month_range, operating_now, week_range

BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
BRAVE_SEARCH_API_KEY_ENV = "BRAVE_SEARCH_API_KEY"
WEB_SEARCH_TIMEOUT_S = 8.0
WEB_SEARCH_RESULT_LIMIT = 5


@dataclass(frozen=True)
class WebSearchPeriod:
    kind: str
    start: date | None
    end: date | None
    provider_freshness: str | None = None


@dataclass(frozen=True)
class WebSearchRoute:
    query: str
    reference_at: datetime
    period: WebSearchPeriod


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    source: str
    snippet: str
    published_at: str = ""


@dataclass(frozen=True)
class WebSearchResponse:
    status: str
    query: str
    reference_at: datetime
    period: WebSearchPeriod
    results: tuple[WebSearchResult, ...] = ()
    reason_code: str = ""


Transport = Callable[[str, Mapping[str, str], float], Mapping[str, Any]]

_EXTERNAL_FRESHNESS_MARKERS = ("뉴스", "소식", "최신", "최근", "현재", "news", "latest", "current")
_INTERNAL_OR_ERP_MARKERS = (
    "입고",
    "출고",
    "재고",
    "제품수불",
    "현재표",
    "현재결과",
    "거래처",
    "발주처",
    "매입처",
    "erp",
    "sql",
    "테이블",
    "필드",
    "컬럼",
    "사내",
    "내부규정",
    "내부 지식",
)


def _compact(text: object) -> str:
    return re.sub(r"\s+", "", str(text or "").strip())


def _period_for_query(text: str, reference_at: datetime) -> WebSearchPeriod:
    compact = _compact(text)
    reference_day = reference_at.date()
    if "오늘" in compact:
        return WebSearchPeriod("today", reference_day, reference_day, "pd")
    if "지난주" in compact:
        period = week_range(reference_day, offset_weeks=-1)
        return WebSearchPeriod("last_week", period.start, period.end, "pw")
    if "이번주" in compact:
        period = week_range(reference_day)
        return WebSearchPeriod("this_week", period.start, period.end, "pw")
    if "지난달" in compact or "지난월" in compact:
        period = month_range(reference_day, offset_months=-1)
        return WebSearchPeriod("last_month", period.start, period.end, "pm")
    if "이번달" in compact or "이번월" in compact:
        period = month_range(reference_day)
        return WebSearchPeriod("this_month", period.start, period.end, "pm")
    return WebSearchPeriod("unspecified", None, None, None)


def parse_web_search_request(text: object, *, now: datetime | None = None) -> WebSearchRoute | None:
    """Recognize only explicit freshness/news questions; keep business NLQ local."""
    query = str(text or "").strip()
    compact = _compact(query)
    if not query or query.startswith("/"):
        return None
    if not any(marker in compact.lower() for marker in _EXTERNAL_FRESHNESS_MARKERS):
        return None
    if any(marker in compact.lower() for marker in _INTERNAL_OR_ERP_MARKERS):
        return None
    reference_at = operating_now(now=now)
    return WebSearchRoute(
        query=query,
        reference_at=reference_at,
        period=_period_for_query(query, reference_at),
    )


def _default_transport(url: str, headers: Mapping[str, str], timeout_s: float) -> Mapping[str, Any]:
    request = Request(url, headers=dict(headers), method="GET")
    with urlopen(request, timeout=timeout_s) as response:  # nosec B310 - fixed HTTPS provider endpoint
        payload = response.read().decode("utf-8", errors="strict")
    parsed = json.loads(payload)
    if not isinstance(parsed, Mapping):
        raise ValueError("Web Search provider returned a non-object response")
    return parsed


def _result_source(url: str) -> str:
    return str(urlparse(url).netloc or "").lower()


def _parse_results(payload: Mapping[str, Any]) -> tuple[WebSearchResult, ...]:
    web = payload.get("web") if isinstance(payload.get("web"), Mapping) else {}
    raw_results = web.get("results") if isinstance(web, Mapping) else []
    parsed: list[WebSearchResult] = []
    for raw in raw_results if isinstance(raw_results, list) else []:
        if not isinstance(raw, Mapping):
            continue
        title = str(raw.get("title") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not title or not url.startswith(("https://", "http://")):
            continue
        parsed.append(
            WebSearchResult(
                title=title,
                url=url,
                source=_result_source(url),
                snippet=str(raw.get("description") or raw.get("snippet") or "").strip(),
                published_at=str(raw.get("published") or raw.get("published_at") or raw.get("page_age") or "").strip(),
            )
        )
        if len(parsed) >= WEB_SEARCH_RESULT_LIMIT:
            break
    return tuple(parsed)


def search_web(
    route: WebSearchRoute,
    *,
    api_key: str | None = None,
    transport: Transport | None = None,
    timeout_s: float = WEB_SEARCH_TIMEOUT_S,
) -> WebSearchResponse:
    """Call Brave Search once. Missing configuration and provider failures never fall back to model knowledge."""
    key = str(api_key if api_key is not None else os.getenv(BRAVE_SEARCH_API_KEY_ENV, "")).strip()
    if not key:
        return WebSearchResponse("failed", route.query, route.reference_at, route.period, reason_code="configuration_missing")

    params: dict[str, str | int] = {"q": route.query, "count": WEB_SEARCH_RESULT_LIMIT}
    if route.period.provider_freshness:
        params["freshness"] = route.period.provider_freshness
    try:
        payload = (transport or _default_transport)(
            f"{BRAVE_SEARCH_ENDPOINT}?{urlencode(params)}",
            {"Accept": "application/json", "X-Subscription-Token": key},
            float(timeout_s),
        )
        results = _parse_results(payload)
    except Exception:
        return WebSearchResponse("failed", route.query, route.reference_at, route.period, reason_code="search_failed")
    if not results:
        return WebSearchResponse("no_match", route.query, route.reference_at, route.period, reason_code="no_results")
    return WebSearchResponse("ready", route.query, route.reference_at, route.period, results=results)


def build_web_search_prompt(*, route: WebSearchRoute, response: WebSearchResponse) -> list[dict[str, str]]:
    if response.status != "ready" or not response.results:
        raise ValueError("Web Search prompt requires ready results")
    evidence = "\n\n".join(
        f"[{index}] title={result.title}\nsource={result.source}\nurl={result.url}\npublished={result.published_at or 'unknown'}\nsnippet={result.snippet}"
        for index, result in enumerate(response.results, start=1)
    )
    period = "unspecified" if route.period.start is None else f"{route.period.start.isoformat()}..{route.period.end.isoformat()}"
    return [{
        "role": "user",
        "content": (
            "아래 외부 검색 결과만 근거로 한국어로 짧게 요약하세요. 근거에 없는 최신 사실을 보태지 마세요. "
            "출처 번호를 본문에서 만들지 말고, 제공된 검색 결과의 의미만 정리하세요.\n\n"
            f"질문: {route.query}\n검색 기준 시각: {route.reference_at:%Y-%m-%d %H:%M:%S} {route.reference_at.tzname() or 'KST'} ({OPERATING_TIMEZONE_NAME})\n"
            f"요청 기간: {period}\n\n검색 결과:\n{evidence}"
        ),
    }]


def render_web_search_answer(*, summary: str, response: WebSearchResponse) -> str:
    if response.status != "ready" or not response.results:
        raise ValueError("Web Search display requires ready results")
    lines = [str(summary or "").strip() or "검색 결과를 요약하지 못했습니다.", "", "**출처**"]
    for result in response.results:
        published = f" · {result.published_at}" if result.published_at else ""
        lines.append(f"- [{result.title}]({result.url}) · {result.source}{published}")
    period = "기간 미지정" if response.period.start is None else f"{response.period.start:%Y-%m-%d} ~ {response.period.end:%Y-%m-%d}"
    lines.extend(("", f"검색 기준 시각: {response.reference_at:%Y-%m-%d %H:%M:%S} {response.reference_at.tzname() or 'KST'} ({OPERATING_TIMEZONE_NAME})", f"요청 기간: {period}"))
    return "\n".join(lines)