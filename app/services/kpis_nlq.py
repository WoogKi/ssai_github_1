"""Deterministic KPIS-only grammar. No I/O, inferred identifiers, or ERP fallback."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from calendar import monthrange
import re

from app.services.external_api_management import ApiManagementError
from app.services.kpis_rest_client import validate_query

TOKEN = re.compile(r"(?<![A-Za-z0-9_])KPIS(?![A-Za-z0-9_])", re.I)
DEFAULT_PRODUCT_LIMIT = 10
MAX_PRODUCT_LIMIT = 20
ALIASES = {
    "MA117": r"(?:의약품(?:정보)?센터\s*)?공지(?:사항)?",
    "MA101": r"표준코드(?:마스터)?(?:\s*이력)?",
    "MA102": r"거래처(?:\s*등록\s*여부)?",
    "MA109": r"회수(?:의약품)?",
    "MA110": r"공급\s*(?:내역|집계)",
    "MA116": r"(?:분기별\s*)?적기보고율",
}
HELP = "KPIS 지원 업무: 공지사항(MA117), 표준코드 이력(MA101), 거래처(MA102), 회수의약품(MA109), 공급내역(MA110). MA116은 준비 중입니다. 업무 하나와 조회조건을 지정해 주세요. 조회는 실행하지 않았습니다."
CANDIDATES = {
    "MA101": "표준코드 13자리가 필요합니다. KPIS 패널의 표준코드 이력조회에서 ERP 제품명/코드로 검색하고 제품과 표준코드를 명시적으로 선택해 주세요. 복수 코드의 첫 항목을 자동 선택하지 않으며, 이번 질문으로 API를 호출하지 않았습니다.",
    "MA102": (
        "거래처 구분 5(요양기관) / 6(의약품공급업체) / 8(안전상비의약품) / 9(특수의료시설)와 사업자번호 10자리가 필요합니다.\n\n"
        "KPIS 패널의 거래처 조회에서 ERP 거래처 후보를 직접 선택하고 구분을 확인해 주세요.\n\n"
        "이름으로 구분이나 번호를 추정하지 않으며, 이번 질문으로 API를 호출하지 않았습니다."
    ),
}


@dataclass(frozen=True)
class Plan:
    service: str = ""
    query: dict = field(default_factory=dict)
    status: str = "clarify"
    reason: str = "unsupported_or_ambiguous"
    message: str = HELP
    product_term: str = ""
    product_limit: int = DEFAULT_PRODUCT_LIMIT


def is_explicit_kpis(text):
    return isinstance(text, str) and TOKEN.search(text) is not None


def _blocked(service, reason="unresolved_condition"):
    return Plan(service=service, reason=reason, message=CANDIDATES.get(service,
        "KPIS 조회조건을 확정하지 못했습니다. 날짜는 YYYY-MM-DD(기간은 시작일 ~ 종료일)로 지정해 주세요. 지원하지 않는 조건을 생략하거나 잘못된 날짜를 보정하지 않았으며 조회는 실행하지 않았습니다."))


def _product_plan(body):
    limit = DEFAULT_PRODUCT_LIMIT
    counts = list(re.finditer(r"(?:상위\s*([0-9]+)\s*개|TOP\s*([0-9]+)(?:\s*개)?)", body, re.I))
    if len(counts) > 1:
        return _blocked("MA101")
    if counts:
        match = counts[0]
        limit = int(match.group(1) or match.group(2))
        body = body[:match.start()] + " " + body[match.end():]
    if limit < 1:
        return _blocked("MA101")
    if limit > MAX_PRODUCT_LIMIT:
        return Plan(service="MA101", reason="product_limit_exceeded",
                    message="MA101 자동 조회는 최대 20개 제품까지 가능합니다. 상위 20개 이하로 지정해 주세요. ERP 검색과 KPIS API를 호출하지 않았습니다.")
    body = re.sub(r"(?<![가-힣A-Za-z0-9_])(?:변경\s*이력(?:조회)?|이력조회|조회|보여줘|보여주세요|알려줘|알려주세요|해주세요)(?![가-힣A-Za-z0-9_])", " ", body)
    term = " ".join(body.split()).strip('"')
    term = re.sub(r"^제품(?:명|코드)\s*[:=]?\s*", "", term).strip()
    if re.fullmatch(r"[0-9]{13}", term):
        return Plan("MA101", {"msupStdCd": term}, "ready", "ready", "")
    if (not term or len(term) > 100 or re.search(r"[0-9]{6,}|최신|현재만|매출|판매|순위|하위|이상|이하|제외|기간|부터|까지|~", term)
            or not re.fullmatch(r"[가-힣A-Za-z0-9 ()_.%\-/]+", term)):
        return _blocked("MA101", "standard_selection_required")
    if re.fullmatch(r"[0-9 ]+", term) and not re.fullmatch(r"[0-9]{5}", term):
        return _blocked("MA101", "invalid_standard_code")
    return Plan(service="MA101", status="products", reason="product_search", message="",
                product_term=term, product_limit=limit)


def _expand_month_range(body):
    patterns = (
        r"(?<![0-9])([0-9]{4})([0-9]{2})\s*~\s*([0-9]{4})([0-9]{2})(?![0-9])",
        r"(?<![0-9-])([0-9]{4})-([0-9]{1,2})(?![-0-9])\s*~\s*([0-9]{4})-([0-9]{1,2})(?![-0-9])",
        r"([0-9]{4})년\s*([0-9]{1,2})월\s*~\s*(?:([0-9]{4})년\s*)?([0-9]{1,2})월(?!\s*[0-9]+일)",
    )
    matches = [match for pattern in patterns for match in re.finditer(pattern, body)]
    if not matches:
        return body
    if len(matches) != 1:
        raise ValueError()
    match = matches[0]
    year, month, end_year, end_month = match.groups()
    start = date(int(year), int(month), 1)
    end_year, end_month = int(end_year or year), int(end_month)
    end = date(end_year, end_month, monthrange(end_year, end_month)[1])
    if start > end:
        raise ValueError()
    return body[:match.start()] + start.strftime("%Y%m%d") + " ~ " + end.strftime("%Y%m%d") + body[match.end():]


def parse_request(text, *, today: date):
    if not is_explicit_kpis(text):
        return None
    if len(text) > 2000:
        return _blocked("", "input_limit")
    body = TOKEN.sub(" ", text)
    ids = re.findall(r"(?<![A-Za-z0-9_])MA[0-9]{3}(?![A-Za-z0-9_])", body, re.I)
    intent_body = re.sub(r"거래처\s*사업자(?:등록)?번호|거래처명", " ", body)
    services = {s for s, pattern in ALIASES.items() if re.search(pattern, intent_body)} | {s.upper() for s in ids}
    if len(services) != 1 or not services <= ALIASES.keys():
        return Plan()
    service = services.pop()
    if service == "MA116":
        return Plan(service=service, reason="jwt_pending", message="MA116은 JWT 실연동 준비 중입니다. API를 호출하지 않았습니다.")
    body = re.sub(r"(?<![A-Za-z0-9_])MA[0-9]{3}(?![A-Za-z0-9_])", " ", body, flags=re.I)
    # Extract labelled optional values before removing service words.
    query = {}
    if service in {"MA102", "MA110"}:
        def take(pattern, key):
            nonlocal body
            matches = list(re.finditer(pattern, body))
            if len(matches) > 1:
                raise ValueError()
            if matches:
                query[key] = matches[0].group(1)
                body = body[:matches[0].start()] + " " + body[matches[0].end():]
        try:
            take(r'(?:tkrpNm|거래처명)\s*[:=]?\s*"([^"\n]+)"', "tkrpNm")
            institution_labels = "ykiho|요양기관기호|요양기관코드" + ("|요양기관번호" if service == "MA102" else "")
            take(rf"(?:{institution_labels})\s*[:=]?\s*([0-9]+)", "ykiho")
            take(r"(?<![0-9-])([0-9]{3}-[0-9]{2}-[0-9]{5}|[0-9]{10})(?![0-9-])", "tkrHbin")
            business_label = r"사업자(?:\s*등록)?번호" if service == "MA102" else r"사업자(?:등록)?번호"
            body = re.sub(rf"(?:(?:거래처\s*)?{business_label}|tkrHbin)\s*[:=]?\s*", " ", body)
            if service == "MA102":
                take(r"(?:msupSplyFrmCd|(?:거래처\s*)?구분)\s*[:=]?\s*([0-9]+)", "msupSplyFrmCd")
            else:
                take(r"(?:splyTpCd|공급\s*구분)\s*[:=]?\s*([0-9]+)", "splyTpCd")
                types = {"출고 + 정정": None, "출고": "1", "반품": "2", "폐기": "3", "정정": "4"}
                matches = list(re.finditer(r"출고\s*\+\s*정정|출고|반품|폐기|정정", body))
                if len(matches) > 1 or (matches and "splyTpCd" in query):
                    raise ValueError()
                if matches:
                    match = matches[0]
                    value = "출고 + 정정" if "+" in match.group() else match.group()
                    query["splyTpCd"] = types[value]
                    body = body[:match.start()] + " " + body[match.end():]
                query.setdefault("splyTpCd", None)
        except ValueError:
            return _blocked(service)
    body = re.sub(ALIASES[service], " ", body)
    if service == "MA101":
        return _product_plan(body)
    if service == "MA109":
        try:
            body = _expand_month_range(body)
        except ValueError:
            return _blocked(service, "invalid_date_range")

    dates = []
    relative = []
    if service in {"MA117", "MA109", "MA110"}:
        if service == "MA110" and re.search(r"시작일|종료일|기간", body):
            return _blocked(service, "unresolved_condition")
        pattern = r"(?<![0-9])(?:[0-9]{4}[-./][0-9]{1,2}[-./][0-9]{1,2}|[0-9]{4}년\s*[0-9]{1,2}월\s*[0-9]{1,2}일|[0-9]{8})(?![0-9])"
        matches = list(re.finditer(pattern, body))
        roles = []
        try:
            for match in matches:
                label = re.search(r"(시작일|종료일)\s*[:=]?\s*$", body[:match.start()])
                roles.append(label.group(1) if label else "")
                digits = re.findall(r"[0-9]+", match.group())
                if len(digits) == 1:
                    raw = digits[0]
                    digits = [raw[:4], raw[4:6], raw[6:]]
                dates.append(date(*map(int, digits)).strftime("%Y%m%d"))
        except ValueError:
            return _blocked(service, "invalid_date")
        if any(roles):
            if len(roles) != 2 or set(roles) != {"시작일", "종료일"}:
                return _blocked(service, "missing_date_endpoint")
            dates = [dict(zip(roles, dates))[key] for key in ("시작일", "종료일")]
        body = re.sub(pattern, " ", body)
        for word, days in (("오늘", 0), ("어제", 1), ("그제", 2)):
            if word in body:
                relative.extend([(today - timedelta(days=days)).strftime("%Y%m%d")] * body.count(word))
                body = body.replace(word, " ")
        if dates and relative or len(relative) > 1:
            return _blocked(service)
        if relative:
            dates = relative if service == "MA110" else relative * 2
        if service == "MA109":
            years = re.findall(r"(?<![0-9])([0-9]{4})\s*년", body)
            if years:
                if len(years) != 1 or dates:
                    return _blocked(service)
                query["rpayCmmYr"] = years[0]
                body = re.sub(r"(?<![0-9])[0-9]{4}\s*년", " ", body)
        if dates:
            if service == "MA110" and len(dates) == 1:
                query["splyDd"] = dates[0]
            elif service != "MA110" and len(dates) == 2:
                keys = ("srchStaDd", "srchEndDd") if service == "MA117" else ("rpayCmmStaDd", "rpayCmmEndDd")
                query.update(zip(keys, dates))
            else:
                return _blocked(service, "missing_date_endpoint")
        if not dates and not query.get("rpayCmmYr") and re.search(r"시작일|종료일|공급일|날짜|연도|명령년", body):
            return _blocked(service, "missing_explicit_date")
        # Only the exact existing default periods are accepted without dates.
        if not dates and not query.get("rpayCmmYr"):
            default_words = {"MA117": r"최근\s*(?:1\s*주일|일주일|7\s*일)?", "MA109": r"최근\s*(?:1\s*개월|한\s*달)?",
                             "MA110": r"직전\s*영업일"}
            body = re.sub(default_words[service], " ", body)
    cleanup = r"조회|보여\s*줘|보여주세요|알려\s*줘|알려주세요|해\s*줘|해주세요|정보|등록\s*여부|이력|집계|명령|기간|시작일|종료일|공급일(?:자)?|부터|까지"
    if service == "MA102":
        cleanup = r"(?<![가-힣A-Za-z0-9_])(?:" + cleanup + r")(?![가-힣A-Za-z0-9_])"
    body = re.sub(cleanup, " ", body)
    if service == "MA102" and body.strip() and "tkrpNm" not in query:
        name = body.strip()
        if (re.fullmatch(r"[가-힣A-Za-z][가-힣A-Za-z0-9().·&_\-]*", name)
                and not re.search(r"최신|현재|상위|이상|이하|순위|제외|전체", name)):
            query["tkrpNm"] = name
            body = ""
    if re.sub(r"[\s~:：=,?.!]", "", body):
        return _blocked(service)
    if service in {"MA117", "MA109", "MA110"} and not any(k in query for k in ("splyDd", "srchStaDd", "rpayCmmStaDd", "rpayCmmYr")):
        return Plan(service, query, "defaults", "existing_default", "")
    try:
        checked = validate_query(service, query)
    except ApiManagementError as exc:
        return _blocked(service, str(exc))
    return Plan(service, checked, "ready", "ready", "")
