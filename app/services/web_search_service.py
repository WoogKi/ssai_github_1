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
    user_query: str = ""
    prior_results: tuple[WebSearchResult, ...] = ()


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
    "발주",
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
_FOLLOWUP_INTERNAL_MARKERS = _INTERNAL_OR_ERP_MARKERS + (
    "계약단가",
    "구매원가",
    "매출",
    "매입",
    "수불",
    "제품정보",
    "제조사",
    "제약사",
)
_FOLLOWUP_REFERENCE_MARKERS = (
    "그중",
    "그 중",
    "그 기사",
    "이 기사",
    "해당 기사",
    "그 정책",
    "이 정책",
    "해당 정책",
    "그 내용",
    "그 자료",
    "이 자료",
    "해당 자료",
    "그 출처",
    "위 내용",
    "방금 내용",
    "앞의 내용",
)
_FOLLOWUP_TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣]+")
_FOLLOWUP_STOPWORDS = frozenset(
    {
        "그중", "기사", "정책", "내용", "관련", "대한", "어떤", "무엇", "뭐야",
        "알려줘", "알려주세요", "설명", "설명해줘", "어디야", "언제야", "품목은", "품목이야",
    }
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


def _web_result_from_message(value: object) -> WebSearchResult | None:
    if not isinstance(value, Mapping):
        return None
    title = str(value.get("title") or "").strip()
    url = str(value.get("url") or "").strip()
    if not title or not url.startswith(("https://", "http://")):
        return None
    return WebSearchResult(
        title=title,
        url=url,
        source=str(value.get("source") or _result_source(url)).strip(),
        snippet=str(value.get("snippet") or "").strip(),
        published_at=str(value.get("published_at") or "").strip(),
    )


def _meaningful_followup_tokens(value: object) -> tuple[str, ...]:
    tokens: list[str] = []
    for token in _FOLLOWUP_TOKEN_PATTERN.findall(str(value or "").casefold()):
        if len(token) < 2 or token in _FOLLOWUP_STOPWORDS:
            continue
        tokens.append(token)
    return tuple(dict.fromkeys(tokens))


def parse_web_search_followup_request(
    text: object,
    *,
    parent_message: object,
    now: datetime | None = None,
) -> WebSearchRoute | None:
    """Continue only an immediately preceding successful news/search answer."""
    query = str(text or "").strip()
    if not query or query.startswith("/") or not isinstance(parent_message, Mapping):
        return None
    if parse_web_search_request(query, now=now) is not None:
        return None
    compact = _compact(query).casefold()
    if any(marker in compact for marker in _FOLLOWUP_INTERNAL_MARKERS):
        return None
    meta = parent_message.get("meta")
    if not isinstance(meta, Mapping) or not bool(meta.get("web_search")) or meta.get("status") != "ready":
        return None
    prior_query = str(meta.get("search_query") or meta.get("query") or "").strip()
    prior_results = tuple(
        result
        for result in (_web_result_from_message(item) for item in (meta.get("sources") or ()))
        if result is not None
    )
    if not prior_query or not prior_results:
        return None
    context_text = " ".join(
        [prior_query, str(parent_message.get("content") or "")]
        + [f"{item.title} {item.snippet}" for item in prior_results]
    ).casefold()
    direct_reference = any(marker.replace(" ", "") in compact for marker in _FOLLOWUP_REFERENCE_MARKERS)
    overlap_count = sum(token in context_text for token in _meaningful_followup_tokens(query))
    if not direct_reference and overlap_count < 2:
        return None
    reference_at = operating_now(now=now)
    return WebSearchRoute(
        query=f"{prior_query} {query}".strip(),
        user_query=query,
        reference_at=reference_at,
        period=_period_for_query(prior_query, reference_at),
        prior_results=prior_results,
    )


def latest_ready_web_search_message(messages: object) -> Mapping[str, Any] | None:
    """Return the latest assistant only when it is a successful Web Search answer."""
    if not isinstance(messages, (list, tuple)):
        return None
    for message in reversed(messages):
        if not isinstance(message, Mapping):
            continue
        if str(message.get("role") or "").strip().lower() != "assistant":
            continue
        meta = message.get("meta")
        if isinstance(meta, Mapping) and bool(meta.get("web_search")) and meta.get("status") == "ready":
            return message
        return None
    return None


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
        if route.prior_results:
            return WebSearchResponse(
                "ready",
                route.query,
                route.reference_at,
                route.period,
                results=route.prior_results,
                reason_code="prior_results_only",
            )
        return WebSearchResponse("failed", route.query, route.reference_at, route.period, reason_code="search_failed")
    if route.prior_results:
        merged: list[WebSearchResult] = []
        seen_urls: set[str] = set()
        for item in (*results, *route.prior_results):
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            merged.append(item)
        results = tuple(merged[: WEB_SEARCH_RESULT_LIMIT * 2])
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
            "아래 외부 검색 결과만 근거로 한국어로 짧게 답하세요. 근거에 없는 최신 사실을 보태지 마세요. "
            "질문이 요구한 이름이나 목록이 검색 결과에 모두 없으면 추측하지 말고, 현재 확보한 기사에는 "
            "전체 내용이 명시되지 않았다고 답하세요. "
            "출처 번호를 본문에서 만들지 말고, 제공된 검색 결과의 의미만 정리하세요.\n\n"
            f"질문: {route.user_query or route.query}\n검색어: {route.query}\n"
            f"검색 기준 시각: {route.reference_at:%Y-%m-%d %H:%M:%S} {route.reference_at.tzname() or 'KST'} ({OPERATING_TIMEZONE_NAME})\n"
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
