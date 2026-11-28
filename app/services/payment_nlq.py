"""Grammar adapter for registered scope-bound ERP detail queries. No source I/O."""

from __future__ import annotations

import re
from decimal import Decimal
from functools import lru_cache

from app.services.erp_table_query_service import (
    calendar_month_period,
    normalize_detail_params,
)
from app.sims.meta.rddbc_payment_meta import TITLES
from app.services.io_nlq import NLQ_PERIOD_CLAUSE, match_nlq_period_prefix, resolve_nlq_period_clause

_DOMAIN = {"입출금": "310", "입금": "310", "출금": "310", "어음": "320", "담보": "330"}
_ACTION_END = r"(?=(?:조회|검색|내역|상세|목록)(?=\s|$|[?!.])|\s|$|[?!.])"
_ANCHOR = re.compile(
    r"(?<![가-힣A-Za-z0-9_])(입출금|입금|출금|어음|담보)(?:만)?" + _ACTION_END
)
_QUOTE = re.compile(r"""'[^']*'|"[^"]*" """.strip())
_GENERAL = re.compile(
    r"설명|차이|장단점|무엇|뭐야|무슨|어떻게|뜻|방법|이란|번역|사용법|정의|알고\s*싶|인가요|할까요"
)
_LABEL_ALIASES = {
    "vendor_name": ("주거래처명",),
    "vendor_cd": ("주거래처코드",),
    "reference_vendor_name": ("매출참조처명",),
    "transfer_vendor_name": ("외상매출대체처명",),
    "note_seq": ("어음번호 순번",),
    "slip_seq": ("전표번호 순번",),
    "permit_seq": ("허가번호 순번",),
    "amount_from": ("금액 하한",),
    "amount_to": ("금액 상한",),
    "flow_name": ("입출금 세부구분명",),
    "remark": ("비고내용",),
}
_ROLE_LABELS = {
    "Receive_YyMmDd": ("입출금일자", "입출금일"),
    "Slip_YyMmDd": ("전표일자", "전표일"),
    "Add_Date": ("등록일자", "등록일"),
    "Mod_Date": ("수정일자", "수정일"),
    "Draft_YyMmDd": ("발행년월일", "발행일"),
    "Maturit_YyMmDd": ("만기년월일", "만기일", "만기"),
}
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_MONEY = rf"-?{_NUMBER}(?:\s*(?:천만|억|만|천)(?:\s*{_NUMBER})?)*\s*원?"
_DAY = r"(?:\d{4}-\d{2}-\d{2}|\d{8})"
_END = r"(?=\s|$)"
_BAD = "업무·조회조건 또는 집계 기준을 확인해 주세요. 조회하지 않았습니다."


def _alternatives(words):
    return _label_pattern(tuple(sorted({w.replace(" ", "") for w in words}, key=lambda w: (-len(w), w))))


@lru_cache(maxsize=32)
def _label_pattern(words):
    return "|".join(r"\s*".join(map(re.escape, w)) for w in words)


class ConditionHint(ValueError):
    """User-facing hint constructed only from registered labels and fixed instructions."""


def _field_hint(spec, key):
    kind = spec["filters"][key][1]
    form = {"name": "명칭을 입력해 주세요. 공백이 있는 값은 따옴표로 묶어 주세요.",
            "text": "검색할 문자열을 입력해 주세요.", "code": "문자코드를 입력해 주세요. 선행 0은 유지합니다.",
            "money": "정수 금액 또는 금액 범위를 입력해 주세요.", "integer": "정수 또는 정수 범위를 입력해 주세요.",
            "date": "YYYY-MM-DD 또는 YYYYMMDD 날짜를 입력해 주세요."}
    return ConditionHint(f'{spec["filter_labels"][key]} 조건: {form[kind]}')


@lru_cache(maxsize=3)
def contract(table):
    from importlib import import_module
    from app.ui.current_table_followups.generic import _group_aliases

    spec = import_module(f"app.services.rddbc{table}_service").SPEC
    labels, groups, roles = {}, {}, {}
    for k, (_, kind, _) in spec["filters"].items():
        label = spec["filter_labels"][k].replace("거레처", "거래처")
        if k in {
            "flow_code",
            "note_type",
            "note_class",
            "collateral_kind",
        } and not label.endswith("코드"):
            label += "코드"
        for term in (label, *_LABEL_ALIASES.get(k, ()), *spec.get("filter_aliases", {}).get(k, ())):
            labels[term.replace(" ", "")] = k
        if kind == "name":
            for term in _group_aliases(label):
                groups[term] = label
    # Grouping roles must refer to actual output columns, not service filter ids.
    for term, col in spec.get("group_aliases", {}).items():
        if col in groups.values():
            groups[term] = col
    groups.update({"업무구분": spec["category_label"], "거래처": "거래처명"})
    if table == "310":
        groups.update(
            {
                "담당자": "거래처 담당자명",
                "입출금구분": "입출금구분명",
                "입출금": "입금/출금 구분",
                "입금/출금": "입금/출금 구분",
            }
        )
    if table == "330":
        groups["여수신구분"] = "여수신구분"
    for r in spec["dates"]:
        roles.update(dict.fromkeys(_ROLE_LABELS[r], r))
    for term in ("일자", "월", "연도", "요일"):
        groups[term] = term
    if table == "310":
        labels["구분코드"] = "flow_code"
    return spec, labels, {k.replace(" ", ""): v for k, v in groups.items()}, roles


@lru_cache(maxsize=1)
def _joined_date_clause():
    words = {term for roles in _ROLE_LABELS.values() for term in roles}
    return re.compile(r"(?<!\S)(?:" + _alternatives(words) + "|" + NLQ_PERIOD_CLAUSE
                      + r")(?=(?:" + "|".join(_DOMAIN) + ")" + _ACTION_END + ")", re.I)


def _separate_role(text):
    # Protect quoted/labeled values before separating date expressions from an action.
    pattern = _joined_date_clause()
    if not pattern.search(text):
        return text
    protected = [m.span() for p in (_QUOTE, _ownership_slots()) for m in p.finditer(text)]
    return pattern.sub(lambda m: m[0] if any(a <= m.start() < b for a, b in protected) else m[0] + " ", text)


def match_payment(text):
    raw = _separate_role(str(text or "").strip())
    if not _ANCHOR.search(raw) and not re.search(
        r"(?:담보종류|어음번호분류|어음구분)(?:명)?별\s+집계", raw
    ):
        return None
    scrub = _QUOTE.sub(" QUOTED_VALUE ", raw)
    # Compound condition labels are not business anchors. Mask labels and
    # literal text slots for ownership only; validation consumes the original.
    scrub = _ownership_slots().sub(" ", scrub)
    if (
        "\n" in raw
        or scrub.startswith(("/", "현재표", "현재 표"))
        or _GENERAL.search(scrub)
        or re.search(r"(?:뉴스|기사|소식|보도|인터넷\s*검색|웹\s*검색)", scrub)
        or re.search(r"(?:추천|조언|알려줄|괜찮|해야\s*할|가능한가|궁금|좋을까)", scrub)
        or re.search(r"(?i)(?<![A-Za-z0-9_])KPIS(?![A-Za-z0-9_])", raw)
    ):
        return None
    found = []
    for m in _ANCHOR.finditer(scrub):
        before = scrub[: m.start()].rstrip(" :=")
        if re.search(r"(?:명|코드|번호|비고|발행처|키워드|주소|사용자ID)$", before):
            continue
        if before.endswith("현재표"):
            return None
        found.append(_DOMAIN[m[1]])
    if found:
        return found[0]
    # A unique, explicitly grouped business role may own a source request.
    m = re.fullmatch(r"(담보종류|어음번호분류)(?:명)?별\s+집계[?]?", scrub)
    return {"담보종류": "330", "어음번호분류": "320"}[m[1]] if m else None


@lru_cache(maxsize=1)
def _ownership_slots():
    terms = set()
    for table in TITLES:
        spec, labels, _, _ = contract(table)
        for label, key in labels.items():
            if spec["filters"][key][1] in ("name", "text", "code"):
                terms.add(label)
                terms.add(spec["filter_labels"][key])
                terms.update(_LABEL_ALIASES.get(key, ()))
    return re.compile(
        r"(?<![가-힣\w])(?:" + _alternatives(terms) + r")(?:\s*[:=]\s*|\s+)\S+"
    )


def revision(text):
    raw = str(text or "").strip()
    if raw.startswith(("현재표", "현재 표")) or _GENERAL.search(raw):
        return None
    raw = re.sub(r"^(?:(?:입출금|입금|출금|어음|담보)\s+)?같은\s*조건으로\s*", "", raw)
    if raw == "다시 조회":
        return ""
    m = re.fullmatch(r"(입금|출금)만\s*보여\s*줘", raw)
    if m:
        return m[1]
    m = re.fullmatch(
        r"(.+?)(?:으로|로)\s*(?:변경(?:해서)?(?:\s*다시\s*조회)?|다시\s*조회)", raw
    )
    if m:
        return re.sub(r"^거래처\s+", "거래처명 ", m[1])
    return None


def is_detail_edit(table, phrase):
    """Only an explicit edit to a registered role can inherit an active request."""
    if not phrase or phrase in ("입금", "출금") or match_nlq_period_prefix(phrase):
        return True
    _, fields, _, dates = contract(table)
    return bool(
        re.match(
            r"(" + _alternatives(set(fields) | set(dates)) + r")(?=\s|[:=]|$)", phrase
        )
        or any(
            phrase.startswith(label + " ")
            for label in contract(table)[0]["filter_labels"].values()
        )
    )


def _money(raw):
    s = re.sub(r"\s+", "", raw).removesuffix("원")
    sign = -1 if s.startswith("-") else 1
    if sign < 0:
        s = s[1:]
    total, last = Decimal(0), 10**18
    while s:
        m = re.match(rf"({_NUMBER})(천만|억|만|천)?", s)
        if not m or len(m[1]) > 25:
            raise ValueError(_BAD)
        scale = {"억": 10**8, "천만": 10**7, "만": 10**4, "천": 1000, None: 1}[m[2]]
        if scale >= last:
            raise ValueError(_BAD)
        total += Decimal(m[1].replace(",", "")) * scale
        last, s = scale, s[m.end() :]
    if total != int(total) or total >= 10**18:
        raise ValueError(_BAD)
    return int(total) * sign


def parse_payment(text, table, *, today, previous=None):
    """Consume every token. Only normalized service input and a bounded operation leave here."""
    spec, labels, groups, roles = contract(table)
    terms = labels
    raw = _separate_role(text.strip().rstrip("?!")).strip()
    base = dict(previous or {})
    filters, patch, groups_used = dict(base.get("filters", {})), {}, []
    role = period = direction = rank = metric = None
    aggregate = False
    has_domain = False

    def set_filter(k, v):
        if k in patch and patch[k] != v:
            if k == "amount_from":
                v = max(v, patch[k])
            elif k == "amount_to":
                v = min(v, patch[k])
            else:
                raise ConditionHint(f'{spec["filter_labels"][k]} 조건이 서로 다릅니다. 하나로 지정해 주세요.')
        patch[k] = v

    def pop(pattern):
        nonlocal raw
        m = re.match(pattern, raw, re.I)
        if m:
            raw = raw[m.end() :].lstrip()
        return m

    while raw:
        if "amount_from" in patch or "amount_to" in patch:
            bound = pop(r"(" + _MONEY + r")\s*(이상|이하|초과|미만)" + _END)
            if bound:
                n = _money(bound[1])
                set_filter(
                    "amount_from" if bound[2] in ("이상", "초과") else "amount_to",
                    n + int(bound[2] == "초과") - int(bound[2] == "미만"),
                )
                continue
        m = pop(r"(" + _alternatives(groups) + r")\s*별" + _END)
        if m:
            col = groups[re.sub(r"\s+", "", m[1])]
            if not col or col in groups_used:
                raise ValueError(_BAD)
            groups_used.append(col)
            aggregate = True
            continue
        m = pop(r"(입금금액|출금금액|건수|총액)" + _END)
        if m:
            if metric and metric != m[1]:
                raise ValueError(_BAD)
            metric = m[1]
            continue
        m = pop(r"(TOP|상위|하위|LOW)\s*(\d+)(?:건|개)?" + _END)
        if m:
            if rank not in (None, "순위") or not 1 <= int(m[2]) <= 500:
                raise ValueError(_BAD)
            rank = ("상위" if m[1].upper() in ("TOP", "상위") else "하위") + m[2]
            continue
        if pop("순위" + _END):
            rank = rank or "순위"
            continue
        if pop(r"(?:집계|합계)" + _END):
            aggregate = True
            continue
        if (
            re.match(r"금액\s+(?:TOP|상위|LOW|하위|합계|집계|순위)", raw, re.I)
            or raw == "금액"
            and aggregate
        ):
            pop("금액")
            metric = "금액"
            continue
        m = re.match(r"(" + _alternatives(roles) + r")(?:\s*(기준))?" + _END, raw)
        if m and (m[2] or match_nlq_period_prefix(raw[m.end():].lstrip())
                  or (period and not re.match(r"(?:" + _alternatives(terms) + r")(?=\s|[:=]|$)", raw))):
            raw = raw[m.end():].lstrip()
            resolved_role = roles[re.sub(r"\s+", "", m[1])]
            if role and role != resolved_role:
                raise ValueError(_BAD)
            role = resolved_role
            continue
        m = match_nlq_period_prefix(raw)
        if m:
            raw = raw[m.end():].lstrip()
            try:
                p = resolve_nlq_period_clause(m, today=today, rolling_month_start=calendar_month_period(today)[0])
            except ValueError:
                raise ConditionHint("조회기간을 확인해 주세요. 예: 최근 한 달, 지난달, YYYY-MM-DD.") from None
            if period and p != period:
                raise ValueError(_BAD)
            period = p
            continue
        m = pop(r"(" + _alternatives(terms) + r")(?=\s|[:=]|$)\s*[:=]?\s*")
        if m:
            key = terms[re.sub(r"\s+", "", m[1])]
            kind = spec["filters"][key][1]
            if kind == "money":
                v = pop(r"(" + _MONEY + r")(?=\s|~|부터|까지|이상|이하|초과|미만|$)")
                if not v:
                    raise _field_hint(spec, key)
                n = _money(v[1])
                if pop(r"(?:~|부터)\s*"):
                    v = pop(r"(" + _MONEY + r")(?:까지)?" + _END)
                    if not v or key != "amount":
                        raise ValueError(_BAD)
                    set_filter("amount_from", n)
                    set_filter("amount_to", _money(v[1]))
                else:
                    op = pop(r"(이상|이하|초과|미만)" + _END)
                    if op:
                        if key != "amount":
                            raise ValueError(_BAD)
                        key = (
                            "amount_from" if op[1] in ("이상", "초과") else "amount_to"
                        )
                        n += int(op[1] == "초과") - int(op[1] == "미만")
                    set_filter(key, n)
                continue
            pattern = {
                "integer": r"(-?\d+(?:\s*~\s*-?\d+)?)",
                "date": r"(" + _DAY + r")",
            }.get(kind, r"""('[^']+'|"[^"]+"|[^\s'":=]+)""")
            v = pop(pattern + _END)
            if not v:
                raise _field_hint(spec, key)
            value = v[1]
            if value[0] in "'\"":
                value = value[1:-1]
                if not value.strip():
                    raise ValueError(_BAD)
            elif value in terms or value in (
                "조회",
                "집계",
                "검색",
                "변경",
                "및",
                "그리고",
                "또는",
            ):
                raise _field_hint(spec, key)
            if kind == "integer" and "~" in value:
                value = tuple(map(int, value.split("~")))
            set_filter(key, value)
            continue
        m = pop(r"(입출금|입금|출금|담보|어음)(?:만)?" + _ACTION_END)
        if m:
            if _DOMAIN[m[1]] != table:
                raise ValueError(_BAD)
            d = {"입금": "receipt", "출금": "payment"}.get(m[1])
            if d and direction and direction != d:
                raise ValueError(_BAD)
            direction = d or direction
            has_domain = True
            continue
        if pop(r"거래처(?=\s+입출금" + _ACTION_END + r")"):
            continue
        if pop(r"(?:조회|검색|내역|상세|목록|보여줘|다시|그리고|및)" + _END):
            continue
        raise ConditionHint("미등록 조건 또는 남은 표현이 있습니다. 조건명 뒤에 값을 입력해 주세요. 예: 거래처명 '검색할 이름', 금액 1만원 이상.")

    if not has_domain and not previous and not groups_used:
        raise ValueError(_BAD)
    if rank and metric == "건수" and not groups_used:
        raise ValueError("건수 순위의 집계 기준을 지정해 주세요.")
    if rank and not metric:
        raise ValueError("금액 또는 건수 중 순위 기준을 지정해 주세요.")
    for pair in (("vendor_cd", "vendor_name"), ("vendor_staff_cd", "vendor_staff_name"),
                 ("sales_user_cd", "sales_user_name")):
        if patch.keys() & set(pair):
            for key in pair:
                filters.pop(key, None)
    if patch.keys() & {"amount", "amount_from", "amount_to"}:
        filters.pop("amount", None)
        if "amount" in patch:
            filters.pop("amount_from", None)
            filters.pop("amount_to", None)
    filters.update(patch)
    if "amount" in filters and not filters.get("amount_from", -(10**18)) <= filters[
        "amount"
    ] <= filters.get("amount_to", 10**18):
        raise ValueError(_BAD)
    args = {
        **base,
        "filters": filters,
        "direction": direction or base.get("direction", "all"),
        "_display_context": "chat",
    }
    if role:
        args["date_role"] = role
    if period:
        args["date_from"], args["date_to"] = period
    try:
        args = normalize_detail_params(spec, args, today=today)
    except ValueError:
        if len(patch) == 1 and not direction:
            raise _field_hint(spec, next(iter(patch))) from None
        fields = "·".join(spec["filter_labels"][k] for k in patch)
        raise ConditionHint((fields or "조회기간") + " 조건의 형식, 범위와 조건 간 일치 여부를 확인해 주세요.") from None
    if metric in ("입금금액", "출금금액") and (
        table != "310"
        or args["direction"] == {"출금금액": "receipt", "입금금액": "payment"}[metric]
    ):
        raise ValueError(_BAD)
    if (
        table == "310"
        and rank
        and groups_used
        and metric not in ("입금금액", "출금금액", "건수")
        and args["direction"] == "all"
    ):
        raise ValueError("입금금액 또는 출금금액 중 한 가지 순위 기준을 지정해 주세요.")
    operation = " ".join(
        filter(
            None,
            [
                *(c + "별" for c in groups_used),
                metric,
                "집계" if aggregate else "",
                rank,
            ],
        )
    )
    return {
        "action": TITLES[table],
        "params": args,
        "operation": operation,
        "period_policy": {
            "action_class": "list_detail",
            "default_policy": "calendar_month",
            "explicit_period_present": bool(period),
            "auto_applied": not period and not previous,
            "policy_reason": "explicit"
            if period
            else "retained"
            if previous
            else "calendar_month",
        },
    }
