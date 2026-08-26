# app/sims/nlq/nlq_vendors.py
# -*- coding: utf-8 -*-
# 2026/05/15
from __future__ import annotations

import re
import uuid
import datetime as dt
from typing import Any, Dict, Callable

import pandas as pd


from app.ui.chat_middleware import push_sims_result_to_chat
from app.sims.views.vendors import _prepare_vendor_display

try:
    from app.sims.views.vendors import _build_vendor_master_llm_summary
except Exception:
    _build_vendor_master_llm_summary = None


_TAIL_JOSA_RE = re.compile(r"(이|가|을|를|은|는|의|도|만|과|와)$")

_QUOTE_CHARS = "\"'“”‘’"

def _norm_kw(s: str) -> str:
    """키워드 정규화: trim + 내부 공백 제거"""
    s = (s or "").strip()
    s = re.sub(r"\s+", "", s)
    return s

def _strip_wrapping_quotes(s: str) -> str:
    """양끝 따옴표 제거: '"종합"' → '종합'"""
    s = (s or "").strip()
    # 양끝 따옴표 제거(연속으로 있을 수도 있어 반복)
    while len(s) >= 2 and (s[0] in _QUOTE_CHARS) and (s[-1] in _QUOTE_CHARS):
        s = s[1:-1].strip()
    # 혹시 중간에 남은 따옴표도 제거(보수적으로)
    s = s.replace("“", "").replace("”", "").replace("‘", "").replace("’", "")
    return s.strip()
def _strip_tail_request_words(s: str) -> str:
    """'찾아줘/조회/검색/보여줘' 같은 요청어가 붙어 들어온 경우 제거."""
    s = (s or "").strip()
    s = _strip_wrapping_quotes(s)
    for _ in range(4):
        s2 = re.sub(r"(?:검색|조회|찾아줘|찾아봐줘|찾아봐|찾아|보여줘|알려줘|해줘)$", "", s).strip()
        if s2 == s:
            break
        s = s2
    return s

def _strip_tail_josa(s: str) -> str:
    """한국어 조사(끝 1글자)를 제거해서 키워드 매칭 오탐/미탐을 줄인다. 예: '김이'→'김'"""
    s = _strip_wrapping_quotes((s or "").strip())
    if len(s) >= 2:
        s = _TAIL_JOSA_RE.sub("", s)
    return s.strip()


def _extract_quoted_or_token(txt: str, label_patterns: str) -> str | None:
    """
    label_patterns: '(?:거래처명|영업사원명|...)' 같은 정규식
    - 따옴표 우선: label "값"
    - 그 다음: label 값 포함/들어간/있는/인/같은...
    - 단순 요청형도 처리:
      "영업사원명 김 거래처 조회"
      "수정자 홍길동 거래처 조회"
      "단가적용처명 다라메 거래처 조회"
    """
    # 1) 포함/들어간/있는/인/같은
    m = re.search(
        rf"{label_patterns}\s*(?:에|이|가|은|는)?\s*([^\s,?.!]+?)(?:이|가|을|를|은|는)?\s*(?:포함|들어간|있는|인|같은)",
        txt,
    )
    if m:
        v = _strip_tail_request_words(_strip_tail_josa(m.group(1) or ""))
        return v or None

    # 2) 비따옴표 일반형
    m = re.search(
        rf"{label_patterns}\s*(?:에|이|가|은|는)?\s*([^\s,?.!]+)\s*(?:포함|들어간|있는|인|같은)",
        txt,
    )
    if m:
        v = _strip_tail_request_words(_strip_tail_josa(m.group(1) or ""))
        return v or None

    # 3) 단순 요청형
    #    - "대표자명 김 조회"
    #    - "영업사원명 김 거래처 조회"
    #    - "수정자 홍길동 거래처 검색"
    #    - "단가적용처명 다라메 거래처 조회"
    m = re.search(
        rf"{label_patterns}\s*(?:에|이|가|은|는)?\s*([^\s,?.!]+?)(?:이|가|을|를|은|는)?\s*(?:(?:거래처(?:명|코드)?|거래처목록|거래처\s*목록)\s*)?(?:조회|검색|찾아|찾아줘|찾아봐|보여줘|알려줘)?\s*$",
        txt,
    )
    if m:
        v = _strip_tail_request_words(_strip_tail_josa(m.group(1) or ""))
        return v or None

    return None

def _digits_only(s: str) -> str:
    return re.sub(r"[^0-9]", "", s or "")

_VENDOR_MASTER_STOP = {
    "조회", "검색", "찾아", "찾아줘", "찾아봐", "찾아봐줘",
    "보여줘", "알려줘", "해줘", "목록", "마스터", "거래처",
    "내역", "현황", "명세", "이력", "전표", "집계", "검증",
    "매입", "매출", "매입거래처", "매출거래처",
}

def _has_vendor_master_anchor(txt: str) -> bool:
    t = (txt or "").strip()
    return any(
        k in t
        for k in (
            "거래처",
            "거래처명",
            "거래처코드",
            "거래처 마스터",
            "거래처목록",
            "거래처 목록",
            "SIMS",
            "sims",
            "ERP",
            "erp",
            "사업자번호",
            "사업자등록번호",
            "주소",
            "소재지",

            "대표자",
            "대표자명",
            "전화",
            "전화번호",
            "연락처",
            "휴대폰",
            "핸드폰",
            "팩스",
            "팩스번호",


            "도로명주소",
            "도로명",
            "시도명",
            "시구군명",
            "법정읍면동명",
            "법정동명",
            "지역",

            "영업사원",
            "매입처",
            "매출처",
            "제조사",
            "발주처",
            "단가적용처",
            "단가적용처명",
            "재고적용처",
            "재고적용처명",
            "수정자",
            "수정자명",
            "수정일자",
        )
    )


def _is_unfiltered_vendor_list_request(txt: str) -> bool:
    """명시적인 거래처 master 목록 요청만 무조건부 source 조회로 허용한다."""
    normalized = re.sub(r"[\s,?.!！？…]", "", str(txt or ""))
    return bool(
        re.fullmatch(
            r"거래처(?:명|코드)?(?:목록|마스터)?(?:조회|검색|목록|마스터|찾아|찾아줘|보여줘|알려줘)?",
            normalized,
        )
    )


def _clean_master_token(s: str) -> str:
    s = _strip_tail_request_words(_strip_tail_josa(s or ""))
    s = _norm_kw(s)
    if not s or s in _VENDOR_MASTER_STOP:
        return ""
    return s


def _extract_relation_axis_keyword(txt: str) -> tuple[str | None, str | None]:
    """
    예:
      - '한미 매입처에서 거래처 조회'
      - '매입처 한미 거래처 조회'
      - '제조사 동아 거래처 목록'
      - '발주처 종근당 거래처'
    반환:
      ('매입처', '한미') 형태
    """
    axes = ("매입처", "매출처", "제조사", "발주처")

    # 1) "한미 매입처" 패턴
    m = re.search(r"([^\s,?.!]+)\s*(매입처|매출처|제조사|발주처)", txt)
    if m:
        kw = _clean_master_token(m.group(1))
        axis = (m.group(2) or "").strip()
        if kw:
            return axis, kw

    # 2) "매입처 한미" 패턴
    m = re.search(r"(매입처|매출처|제조사|발주처)\s*([^\s,?.!]+)", txt)
    if m:
        axis = (m.group(1) or "").strip()
        kw = _clean_master_token(m.group(2))
        if kw:
            return axis, kw

    return None, None

def _extract_vendor_scope_filter(txt: str) -> tuple[str, str]:
    """
    거래처 코드 대역 scope 전용 질의 추출.

    예:
      거래처 매출처전체 조회
      거래처 매출처 전체 조회
      거래처 매입처전체 조회
      매출처 전체 조회
      매입처 전체 조회
      매출 거래처 조회
      매입 거래처 조회
      회계매출처 조회
      회계매입처 조회
      거래처 제약사 조회
    """
    t = re.sub(r"\s+", "", str(txt or ""))

    if not t:
        return "", ""

    # 현재표/분석성 문장은 거래처마스터 scope로 해석하지 않는다.
    # 예: 거래처별 매출 분석, 현재표 거래처별 매출 TOP 20
    if any(k in t for k in ("현재표", "현재조회결과", "거래처별", "분석", "집계", "TOP", "top", "상위")):
        return "", ""

    # scope 조회는 조회성 표현일 때만 허용한다.
    # "매출 거래처 조회"처럼 사용자가 자연스럽게 말하는 경우를 보호한다.
    is_lookup = any(k in t for k in ("조회", "검색", "목록", "상세", "보여줘", "보여주세요"))

    # "제약사 검색"은 제품/제조사 검색으로 쓰는 경우가 많으므로
    # 거래처라는 단어가 있을 때만 거래처 제약사 scope로 본다.
    if "제약사" in t and "거래처" in t and is_lookup:
        return "maker", "제약사"

    if "회계매출처" in t or "영업외매출처" in t:
        return "account_sales", "회계매출처"

    if "회계매입처" in t or "영업외매입처" in t:
        return "account_purchase", "회계매입처"

    if "매출처" in t:
        return "sales", "매출처 전체"

    if "매입처" in t:
        return "purchase", "매입처 전체"

    # 자연어 축약형:
    #   매출 거래처 조회 / 거래처 매출 조회
    #   매입 거래처 조회 / 거래처 매입 조회
    if is_lookup and "거래처" in t:
        if "매출" in t:
            return "sales", "매출처 전체"
        if "매입" in t:
            return "purchase", "매입처 전체"

    return "", ""


def _vendor_params(
    *,
    owner_kw: str | None,
    ven_nm_kw: str | None,
    addr_kw: str | None,
    sm_kw: str | None,
    phone_kw: str | None,
    biz_kw: str | None,
    rel_axis: str | None,
    rel_kw: str | None,
    cost_apply_nm_kw: str | None,
    stock_apply_nm_kw: str | None,
    mod_user_kw: str | None,
    mod_date_from: str | None,
    mod_date_to: str | None,
) -> Dict[str, Any]:
    return {
        "owner_nm": owner_kw or "",
        "ven_nm": ven_nm_kw or "",
        "address_kw": addr_kw or "",
        "sales_man_nm": sm_kw or "",
        "phone_kw": phone_kw or "",
        "biz_no_kw": biz_kw or "",
        "rel_axis": rel_axis or "",
        "rel_kw": rel_kw or "",
        "cost_apply_nm": cost_apply_nm_kw or "",
        "stock_apply_nm": stock_apply_nm_kw or "",
        "mod_user_nm": mod_user_kw or "",
        "mod_date_from": mod_date_from or "",
        "mod_date_to": mod_date_to or "",
    }

def _vendor_query_summary(
    *,
    owner_kw: str | None,
    ven_nm_kw: str | None,
    addr_kw: str | None,
    sm_kw: str | None,
    phone_kw: str | None,
    biz_kw: str | None,
    rel_axis: str | None,
    rel_kw: str | None,
    cost_apply_nm_kw: str | None,
    stock_apply_nm_kw: str | None,
    mod_user_kw: str | None,
    mod_date_from: str | None,
    mod_date_to: str | None,
) -> str:
    parts = []
    if ven_nm_kw:
        parts.append(f"거래처명 {ven_nm_kw}")
    if owner_kw:
        parts.append(f"대표자명 {owner_kw}")
    if addr_kw:
        parts.append(f"주소 {addr_kw}")
    if sm_kw:
        parts.append(f"영업사원명 {sm_kw}")
    if phone_kw:
        parts.append(f"전화 {phone_kw}")
    if biz_kw:
        parts.append(f"사업자번호 {biz_kw}")
    if rel_axis and rel_kw:
        parts.append(f"{rel_axis} {rel_kw}")
    if cost_apply_nm_kw:
        parts.append(f"단가적용처명 {cost_apply_nm_kw}")
    if stock_apply_nm_kw:
        parts.append(f"재고적용처명 {stock_apply_nm_kw}")

    if mod_user_kw:
        parts.append(f"수정자 {mod_user_kw}")

    if mod_date_from or mod_date_to:
        if (mod_date_from or "") == (mod_date_to or "") and mod_date_from:
            parts.append(f"수정일자 {mod_date_from}")
        else:
            parts.append(f"수정일자 {mod_date_from or ''}~{mod_date_to or ''}")

    return " / ".join(parts)

def _vendor_summary_line(query_summary: str) -> str:
    qs = str(query_summary or "").strip()
    return f"조회조건: {qs}" if qs else "조회조건: 전체"


def _push_vendor_text(
    *,
    txt: str,
    session_state: Dict[str, Any],
    message: str,
    owner_kw: str | None,
    ven_nm_kw: str | None,
    addr_kw: str | None,
    sm_kw: str | None,
    phone_kw: str | None,
    biz_kw: str | None,
    rel_axis: str | None,
    rel_kw: str | None,
    cost_apply_nm_kw: str | None,
    stock_apply_nm_kw: str | None,
    mod_user_kw: str | None,
    mod_date_from: str | None,
    mod_date_to: str | None,
) -> bool:

    params_out = _vendor_params(
        owner_kw=owner_kw,
        ven_nm_kw=ven_nm_kw,
        addr_kw=addr_kw,
        sm_kw=sm_kw,
        phone_kw=phone_kw,
        biz_kw=biz_kw,
        rel_axis=rel_axis,
        rel_kw=rel_kw,
        cost_apply_nm_kw=cost_apply_nm_kw,
        stock_apply_nm_kw=stock_apply_nm_kw,
        mod_user_kw=mod_user_kw,
        mod_date_from=mod_date_from,
        mod_date_to=mod_date_to,
    )

    query_summary = _vendor_query_summary(
        owner_kw=owner_kw,
        ven_nm_kw=ven_nm_kw,
        addr_kw=addr_kw,
        sm_kw=sm_kw,
        phone_kw=phone_kw,
        biz_kw=biz_kw,
        rel_axis=rel_axis,
        rel_kw=rel_kw,
        cost_apply_nm_kw=cost_apply_nm_kw,
        stock_apply_nm_kw=stock_apply_nm_kw,
        mod_user_kw=mod_user_kw,
        mod_date_from=mod_date_from,
        mod_date_to=mod_date_to,        
    )

    summary_line = _vendor_summary_line(query_summary)
    display_message = f"{message}\n\n{summary_line}"

    result = {
        "title": "거래처 목록",
        "action": "거래처 목록",
        "type": "text",
        "final": True,
        "params": params_out,

        "data": display_message,
        "message": display_message,

        "meta": {
            "nlq": True,
            "master_nlq": True,
            "domain": "vendors",
            "nlq_query": txt,
            "_force_push": True,
            "_nlq_nonce": str(uuid.uuid4()),
            "row_count": 0,
            "row_count_total": 0,
            "source": "거래처마스터(Rddbc030)",
            "query_summary": query_summary,
            "summary_md": summary_line,
        },
    }
    push_sims_result_to_chat(result, "거래처 목록")
    session_state["__scroll_to_msg"] = (
        session_state.get("__sims_last_msg_id") or session_state.get("__scroll_to_msg")
    )
    return True

def _extract_owner_keyword(txt: str) -> str | None:
    """
    대표자명 포함 검색 키워드 추출
    예)
      - 대표자명이 "김" 들어간 거래처는?
      - 대표자명 김 포함 거래처
      - 대표자 김 들어간 거래처
      - 대표자명이 김인 거래처
    """
    if not any(k in txt for k in ("대표자", "대표자명", "사장", "오너")):
        return None

    # ✅ "대표자명 김 조회", "대표자 김 찾아줘" 처럼 '거래처' 단어 없이도 처리
    v = _extract_quoted_or_token(txt, r"(?:대표자명|대표자)")
    if v:
        return _strip_tail_josa(v) or None

    # 따옴표 우선
    m = re.search(r"(?:대표자명|대표자)\s*(?:에|이|가|은|는)?\s*[\"'“”]([^\"'“”]+)[\"'“”]", txt)
    if m:
        kw = (m.group(1) or "").strip()
        return kw or None

    # 비따옴표: '대표자명 김 포함/들어간/있는/인'
    m = re.search(
        r"(?:대표자명|대표자)\s*(?:에|이|가|은|는)?\s*([^\s,?.!]+?)(?:이|가|을|를|은|는)?\s*(?:포함|들어간|있는|인)",
        txt,
    )
    if m:
        kw = _strip_tail_josa(m.group(1) or "")
        return kw or None

    return None

def _extract_vendorname_keyword(txt: str) -> str | None:
    # "거래처명"이 명시된 경우만 강하게 처리
    if "거래처명" in txt:
        return _extract_quoted_or_token(txt, r"(?:거래처명)")

    if "거래처" not in txt:
        return None

    # 다른 구조화 속성 질의면 거래처명으로 해석하지 않음
    if any(k in txt for k in (
        "대표자", "대표자명",
        "전화", "연락처", "휴대폰", "핸드폰", "팩스",
        "사업자", "사업자번호", "사업자등록번호",
        "영업사원", "영업사원명",

        "주소", "소재지", "도로명주소", "도로명", "지번",
        "시도명", "시구군명", "법정읍면동명", "법정동명", "지역",

        "매입처", "매출처", "제조사", "발주처",
        "단가적용처", "단가적용처명",
        "재고적용처", "재고적용처명",
        "수정자", "수정자명", "수정일자",
    )):
        return None

    # UI/조작 의도 차단
    if any(k in txt for k in ("목록", "상세", "옵션", "초기화", "카테고리", "작업선택", "패널", "SIMS")):
        return None

    # 명시형 키워드
    if any(k in txt for k in ("포함", "들어간", "있는", "같은", "이름", "명")):
        m = re.search(r"거래처(?:\s*명|\s*이름)?\s*(?:에|이|가|은|는)?\s*[\"'“”]([^\"'“”]+)[\"'“”]", txt)
        if m:
            return _strip_tail_josa(m.group(1) or "") or None

        m = re.search(
            r"거래처(?:\s*명|\s*이름)?\s*(?:에|이|가|은|는)?\s*([^\s,?.!]+?)(?:이|가|을|를|은|는)?\s*(?:포함|들어간|있는|같은|인)",
            txt,
        )
        if m:
            return _strip_tail_josa(m.group(1) or "") or None

    STOP = {
        "거래처", "전체", "전부", "모두", "검색", "조회", "찾기", "찾아", "알려", "보여", "해줘",
        "찾아줘", "보여줘", "알려줘", "검색해줘", "조회해줘", "찾아봐", "찾아봐줘",
        "몇명", "몇", "몇개", "수", "개수",
        "매입", "매출", "매입거래처", "매출거래처",
        "매입처", "매출처", "제조사", "발주처",
        "단가적용처", "단가적용처명", "재고적용처", "재고적용처명",
        "영업사원", "영업사원명", "수정자", "수정자명", "수정일자",
    }

    def _ok_kw(s: str) -> bool:
        s = _strip_tail_josa(s or "")
        if not s:
            return False

        parts = [p for p in s.split() if p]
        for p in parts:
            if p in STOP:
                return False
            if len(p) < 2:
                return False
            if p.endswith("에서") or p.endswith("으로") or p.endswith("로"):
                return False

        if re.fullmatch(r"[^0-9A-Za-z가-힣]+", s):
            return False

        return True

    # "거래처 <키워드>"
    m = re.search(r"거래처\s+([^\s,?.!]+)(?:\s+([^\s,?.!]+))?", txt)
    if m:
        c1 = m.group(1) or ""
        c2 = m.group(2) or ""
        cand = f"{c1} {c2}".strip() if c2 else c1
        cand = _strip_tail_josa(cand)

        if c2 and _ok_kw(cand):
            return cand
        if _ok_kw(c1):
            return _strip_tail_josa(c1)

    # "<키워드> 거래처"
    m = re.search(r"([^\s,?.!]+)(?:\s+([^\s,?.!]+))?\s+거래처", txt)
    if m:
        c1 = m.group(1) or ""
        c2 = m.group(2) or ""

        if _strip_tail_josa(c2) in STOP:
            c2 = ""

        cand = f"{c1} {c2}".strip() if c2 else c1
        cand = _strip_tail_josa(cand)

        if c2 and _ok_kw(cand):
            return cand
        if _ok_kw(c1):
            return _strip_tail_josa(c1)

    return None
 
def _extract_address_keyword(txt: str) -> str | None:
    """
    구주소/상세주소 기반 필터 키워드 추출.
    도로명주소/도로명/시도명/시구군명/법정동명은 별도 필드로 처리한다.
    """
    if any(k in txt for k in ("도로명주소", "시도명", "시구군명", "법정읍면동명", "법정동명")):
        return None

    if "도로명" in txt:
        return None

    if not any(k in txt for k in ("주소", "소재지", "지번")):
        return None

    # 1) 라벨 기반(따옴표/비따옴표) 공통 추출
    kw = _extract_quoted_or_token(txt, r"(?:주소2|주소|소재지|지번)")

    if kw:
        return kw
 
    # 2) 보조 패턴: "종로가 들어간 주소"처럼 뒤에 라벨이 오는 경우
    m = re.search(r"([^\s,?.!]+?)(?:이|가|을|를|은|는)?\s*(?:포함|들어간|있는|인)\s*(?:주소2|주소|소재지|도로명|지번)", txt)
    if m:
        return _strip_tail_josa(m.group(1) or "") or None
 
    return None

def _extract_sido_keyword(txt: str) -> str | None:
    if not any(k in txt for k in ("시도명", "시도")):
        return None
    return _extract_quoted_or_token(txt, r"(?:시도명|시도)")


def _extract_gugun_keyword(txt: str) -> str | None:
    if not any(k in txt for k in ("시구군명", "시군구명", "시구군", "시군구", "구군")):
        return None
    return _extract_quoted_or_token(txt, r"(?:시구군명|시군구명|시구군|시군구|구군)")


def _extract_dong_keyword(txt: str) -> str | None:
    if not any(k in txt for k in ("법정읍면동명", "법정동명", "읍면동명", "동명")):
        return None
    return _extract_quoted_or_token(txt, r"(?:법정읍면동명|법정동명|읍면동명|동명)")


def _extract_road_name_keyword(txt: str) -> str | None:
    if "도로명" not in txt or "도로명주소" in txt:
        return None
    return _extract_quoted_or_token(txt, r"(?:도로명)")


def _extract_road_address_keyword(txt: str) -> str | None:
    if "도로명주소" not in txt:
        return None
    return _extract_quoted_or_token(txt, r"(?:도로명주소)")

def _has_explicit_vendor_condition(txt: str) -> bool:
    t = str(txt or "").strip()
    return any(
        k in t
        for k in (
            "거래처명",
            "거래처코드",
            "대표자",
            "대표자명",
            "사업자",
            "사업자번호",
            "사업자등록번호",
            "주소",
            "소재지",
            "도로명주소",
            "도로명",
            "시도명",
            "시구군명",
            "법정읍면동명",
            "법정동명",
            "영업사원",
            "영업사원명",
            "전화",
            "전화번호",
            "연락처",
            "휴대폰",
            "핸드폰",
            "팩스",
            "단가적용처",
            "단가적용처명",
            "재고적용처",
            "재고적용처명",
            "등록자",
            "등록자명",
            "등록일자",
            "등록일",
            "수정자",
            "수정자명",
            "수정일자",
            "수정일",
        )
    )


def _extract_loose_vendorname_keyword(txt: str) -> str | None:
    """
    라벨이 없는 기본 거래처 조회.
    예:
    - 경동 거래처 조회 -> 거래처명 경동

    지역명으로도 보일 수 있는 '경동' 같은 단어가 있으므로,
    라벨 없는 'OO 거래처 조회'는 기본적으로 거래처명으로 우선 해석한다.
    """
    t = str(txt or "").strip()
    if "거래처" not in t:
        return None

    if _has_explicit_vendor_condition(t):
        return None

    m = re.search(
        r"^\s*([^\s,?.!]+)\s*거래처(?:\s*(?:조회|검색|찾아|찾아줘|찾아봐|보여줘|알려줘))?\s*$",
        t,
    )
    if m:
        kw = _clean_master_token(m.group(1) or "")
        return kw or None

    m = re.search(
        r"^\s*거래처\s*([^\s,?.!]+)(?:\s*(?:조회|검색|찾아|찾아줘|찾아봐|보여줘|알려줘))?\s*$",
        t,
    )
    if m:
        kw = _clean_master_token(m.group(1) or "")
        return kw or None

    return None

def _extract_loose_region_vendor_keyword(txt: str) -> tuple[str | None, str | None]:
    """
    예:
      - 서울 거래처 조회       -> 시도명 서울
      - 강남구 거래처 조회     -> 시구군명 강남구
      - 역삼동 거래처 조회     -> 법정읍면동명 역삼동
      - 테헤란로 거래처 조회   -> 도로명 테헤란로
    명시 라벨이 없는 경우에만 보조적으로 사용한다.
    """
    t = str(txt or "").strip()
    if "거래처" not in t:
        return None, None

    # 거래처명/대표자/사업자번호 등 명시 조건이 있으면
    # 지역 보조해석을 하지 않는다.
    if _has_explicit_vendor_condition(t):
        return None, None

    # "경동 거래처 조회" 같은 라벨 없는 문장은
    # 거래처명 우선으로 본다. 지역으로 자동 해석하지 않는다.
    if _extract_loose_vendorname_keyword(t):
        return None, None

    # 명시 라벨이 있으면 여기서는 처리하지 않는다.
    if any(k in t for k in ("시도명", "시구군명", "법정읍면동명", "법정동명", "도로명주소", "도로명", "주소", "소재지")):
        return None, None

    m = re.search(r"([가-힣A-Za-z0-9]+)\s*거래처", t)
    if not m:
        return None, None

    kw = _clean_master_token(m.group(1) or "")
    if not kw:
        return None, None

    if kw.endswith(("시", "도", "특별시", "광역시")) or kw in {"서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"}:
        return "sido_nm", kw

    if kw.endswith(("구", "군", "시")):
        return "gugun_nm", kw

    if kw.endswith(("동", "읍", "면")):
        return "dong_nm", kw

    if kw.endswith(("로", "길", "대로")):
        return "road_nm", kw

    return None, None


def _extract_salesman_keyword(txt: str) -> str | None:
    if "영업사원" not in txt:
        return None
    return _extract_quoted_or_token(txt, r"(?:영업사원명|영업사원)")


def _extract_bizno_keyword(txt: str) -> str | None:
    if "사업자" not in txt:
        return None

    # 숫자 뭉치를 우선 추출
    m = re.search(r"(?:사업자등록번호|사업자번호|사업자).{0,10}([0-9][0-9\- ]{6,})", txt)
    if m:
        return (m.group(1) or "").strip()

    v = _extract_quoted_or_token(txt, r"(?:사업자등록번호|사업자번호|사업자)")
    if not v:
        return None
    return _strip_tail_josa(v) or None

def _extract_phone_keyword(txt: str) -> str | None:
    # 전화/연락처/휴대폰/핸드폰/팩스 키워드가 있어야만 처리
    if not any(k in txt for k in ("전화", "연락처", "휴대폰", "핸드폰", "팩스")):
        return None
    v = _extract_quoted_or_token(txt, r"(?:전화번호|전화|연락처|휴대폰|핸드폰|팩스번호|팩스)")
    if not v:
        # 숫자 뭉치 추출(최소 6자리 이상)
        m = re.search(r"([0-9][0-9\- ]{5,})", txt)
        if m:
            v = (m.group(1) or "").strip()
    return _strip_tail_josa(v) or None

def _extract_cost_apply_name_keyword(txt: str) -> str | None:
    if not any(k in txt for k in ("단가적용처", "단가적용처명")):
        return None
    return _extract_quoted_or_token(txt, r"(?:단가적용처명|단가적용처)")


def _extract_stock_apply_name_keyword(txt: str) -> str | None:
    if not any(k in txt for k in ("재고적용처", "재고적용처명")):
        return None
    return _extract_quoted_or_token(txt, r"(?:재고적용처명|재고적용처)")


def _extract_mod_user_keyword(txt: str) -> str | None:
    if not any(k in txt for k in ("수정자", "수정자명")):
        return None
    return _extract_quoted_or_token(txt, r"(?:수정자명|수정자)")


def _extract_mod_date_range(txt: str) -> tuple[str | None, str | None]:
    if "수정일자" not in txt and "수정일" not in txt:
        return None, None

    # YYYYMMDD ~ YYYYMMDD
    m = re.search(r"(?:수정일자|수정일)\s*([0-9]{8})\s*(?:~|\-)\s*([0-9]{8})", txt)
    if m:
        return m.group(1), m.group(2)

    # YYYYMM ~ YYYYMM
    m = re.search(r"(?:수정일자|수정일)\s*([0-9]{6})\s*(?:~|\-)\s*([0-9]{6})", txt)
    if m:
        return m.group(1), m.group(2)

    # YYYY ~ YYYY
    m = re.search(r"(?:수정일자|수정일)\s*([0-9]{4})\s*(?:~|\-)\s*([0-9]{4})", txt)
    if m:
        return m.group(1) + "0101", m.group(2) + "1231"

    # 단일 YYYYMMDD
    m = re.search(r"(?:수정일자|수정일)\s*([0-9]{8})", txt)
    if m:
        d = m.group(1)
        return d, d

    # 단일 YYYYMM
    m = re.search(r"(?:수정일자|수정일)\s*([0-9]{6})", txt)
    if m:
        ym = m.group(1)
        return ym, ym

    # 단일 YYYY
    m = re.search(r"(?:수정일자|수정일)\s*([0-9]{4})", txt)
    if m:
        y = m.group(1)
        return y + "0101", y + "1231"

    return None, None

def try_handle_vendors_nlq(
    txt: str,
    *,
    room: Dict[str, Any],
    session_state: Dict[str, Any],
    make_ts: Callable[[], str],
    next_seq: Callable[[], int],
    logger,
) -> bool:
    """
    거래처 관련 NLQ 자동 처리.
    - 거래처 목록 화면과 동일한 alias/컬럼 순서를 사용한다.
    - 등록자/등록일자/수정자/수정일자 NLQ를 지원한다.
    """
    import inspect

    def _extract_add_user_keyword(text: str) -> str | None:
        if not any(k in text for k in ("등록자", "등록자명")) and "등록한" not in text:
            return None

        v = _extract_quoted_or_token(text, r"(?:등록자명|등록자)")
        if v:
            return _norm_kw(_strip_tail_josa(v)) or None

        m = re.search(r"([^\s,?.!~\-]+?)(?:이|가)?\s*등록한\s*(?:거래처|거래처목록|거래처 목록)?", text)
        if m:
            return _norm_kw(_strip_tail_josa(m.group(1) or "")) or None

        return None

    def _extract_add_date_range(text: str) -> tuple[str | None, str | None]:
        if "등록일자" not in text and "등록일" not in text:
            return None, None

        # YYYYMMDD ~ YYYYMMDD
        m = re.search(r"(?:등록일자|등록일)\s*([0-9]{8})\s*(?:~|\-)\s*([0-9]{8})", text)
        if m:
            return m.group(1), m.group(2)

        # YYYYMM ~ YYYYMM
        m = re.search(r"(?:등록일자|등록일)\s*([0-9]{6})\s*(?:~|\-)\s*([0-9]{6})", text)
        if m:
            return m.group(1), m.group(2)

        # YYYY ~ YYYY
        m = re.search(r"(?:등록일자|등록일)\s*([0-9]{4})\s*(?:~|\-)\s*([0-9]{4})", text)
        if m:
            return m.group(1) + "0101", m.group(2) + "1231"

        # 단일 YYYYMMDD
        m = re.search(r"(?:등록일자|등록일)\s*([0-9]{8})", text)
        if m:
            d = m.group(1)
            return d, d

        # 단일 YYYYMM
        m = re.search(r"(?:등록일자|등록일)\s*([0-9]{6})", text)
        if m:
            ym = m.group(1)
            return ym, ym

        # 단일 YYYY
        m = re.search(r"(?:등록일자|등록일)\s*([0-9]{4})", text)
        if m:
            y = m.group(1)
            return y + "0101", y + "1231"

        return None, None
    
    def _digits_only_local(s: str | None) -> str:
        return re.sub(r"[^0-9]", "", str(s or ""))

    def _last_day_of_month_local(yyyymm: str) -> str:
        first = dt.datetime.strptime(yyyymm + "01", "%Y%m%d")
        if first.month == 12:
            next_first = first.replace(year=first.year + 1, month=1, day=1)
        else:
            next_first = first.replace(month=first.month + 1, day=1)
        return (next_first - dt.timedelta(days=1)).strftime("%Y%m%d")

    def _norm_date_from_local(value: str | None) -> str:
        digits = _digits_only_local(value)
        if len(digits) == 8:
            return digits
        if len(digits) == 6:
            return digits + "01"
        return ""

    def _norm_date_to_local(value: str | None) -> str:
        digits = _digits_only_local(value)
        if len(digits) == 8:
            return digits
        if len(digits) == 6:
            return _last_day_of_month_local(digits)
        return ""

    def _contains_no_space(series: pd.Series, kw: str) -> pd.Series:
        needle = _norm_kw(kw or "")
        if not needle:
            return pd.Series([True] * len(series), index=series.index)
        return (
            series.fillna("")
            .astype(str)
            .str.replace(" ", "", regex=False)
            .str.contains(needle, na=False)
        )

    def _apply_date_range(df_src: pd.DataFrame, col_name: str, date_from: str | None, date_to: str | None) -> pd.DataFrame:
        if col_name not in df_src.columns:
            return df_src
        out = df_src.copy()
        s = (
            out[col_name]
            .fillna("")
            .astype(str)
            .str.replace("-", "", regex=False)
            .str.replace("/", "", regex=False)
            .str.replace(".", "", regex=False)
            .str.replace(r"[^0-9]", "", regex=True)
            .str[:8]
        )
        d_from = _norm_date_from_local(date_from)
        d_to = _norm_date_to_local(date_to)
        if d_from:
            out = out[s >= d_from]
            s = (
                out[col_name]
                .fillna("")
                .astype(str)
                .str.replace("-", "", regex=False)
                .str.replace("/", "", regex=False)
                .str.replace(".", "", regex=False)
                .str.replace(r"[^0-9]", "", regex=True)
                .str[:8]
            )
        if d_to:
            out = out[s <= d_to]
        return out

    def _vendor_params_local(
        *,
        owner_kw: str | None,
        ven_nm_kw: str | None,
        addr_kw: str | None,
        sm_kw: str | None,
        phone_kw: str | None,
        biz_kw: str | None,
        rel_axis: str | None,
        rel_kw: str | None,
        cost_apply_nm_kw: str | None,
        stock_apply_nm_kw: str | None,
        add_user_kw: str | None,
        add_date_from: str | None,
        add_date_to: str | None,
        mod_user_kw: str | None,
        mod_date_from: str | None,
        mod_date_to: str | None,
    ) -> Dict[str, Any]:
        return {
            "대표자명": owner_kw or "",
            "거래처명": ven_nm_kw or "",
            "상세주소": addr_kw or "",
            "영업사원명": sm_kw or "",
            "전화번호": phone_kw or "",
            "사업자등록번호": biz_kw or "",
            "관계축": rel_axis or "",
            "관계키워드": rel_kw or "",
            "단가적용처명": cost_apply_nm_kw or "",
            "재고적용처명": stock_apply_nm_kw or "",
            "등록자": add_user_kw or "",
            "등록일자": (
                add_date_from
                if (add_date_from and add_date_from == add_date_to)
                else f"{add_date_from or ''}~{add_date_to or ''}" if (add_date_from or add_date_to) else ""
            ),
            "수정자": mod_user_kw or "",
            "수정일자": (
                mod_date_from
                if (mod_date_from and mod_date_from == mod_date_to)
                else f"{mod_date_from or ''}~{mod_date_to or ''}" if (mod_date_from or mod_date_to) else ""
            ),
        }

    def _vendor_query_summary_local(
        *,
        owner_kw: str | None,
        ven_nm_kw: str | None,
        addr_kw: str | None,
        sm_kw: str | None,
        phone_kw: str | None,
        biz_kw: str | None,
        rel_axis: str | None,
        rel_kw: str | None,
        cost_apply_nm_kw: str | None,
        stock_apply_nm_kw: str | None,
        add_user_kw: str | None,
        add_date_from: str | None,
        add_date_to: str | None,
        mod_user_kw: str | None,
        mod_date_from: str | None,
        mod_date_to: str | None,
    ) -> str:
        parts = []
        if ven_nm_kw:
            parts.append(f"거래처명 {ven_nm_kw}")
        if owner_kw:
            parts.append(f"대표자명 {owner_kw}")
        if addr_kw:
            parts.append(f"상세주소 {addr_kw}")
        if sm_kw:
            parts.append(f"영업사원명 {sm_kw}")
        if phone_kw:
            parts.append(f"전화번호 {phone_kw}")
        if biz_kw:
            parts.append(f"사업자등록번호 {biz_kw}")
        if rel_axis and rel_kw:
            parts.append(f"{rel_axis} {rel_kw}")
        if cost_apply_nm_kw:
            parts.append(f"단가적용처명 {cost_apply_nm_kw}")
        if stock_apply_nm_kw:
            parts.append(f"재고적용처명 {stock_apply_nm_kw}")
        if add_user_kw:
            parts.append(f"등록자 {add_user_kw}")
        if add_date_from or add_date_to:
            if (add_date_from or "") == (add_date_to or "") and add_date_from:
                parts.append(f"등록일자 {add_date_from}")
            else:
                parts.append(f"등록일자 {add_date_from or ''}~{add_date_to or ''}")
        if mod_user_kw:
            parts.append(f"수정자 {mod_user_kw}")
        if mod_date_from or mod_date_to:
            if (mod_date_from or "") == (mod_date_to or "") and mod_date_from:
                parts.append(f"수정일자 {mod_date_from}")
            else:
                parts.append(f"수정일자 {mod_date_from or ''}~{mod_date_to or ''}")
        return " / ".join(parts)

    def _push_vendor_text_local(
        *,
        message: str,
        owner_kw: str | None,
        ven_nm_kw: str | None,
        addr_kw: str | None,
        sm_kw: str | None,
        phone_kw: str | None,
        biz_kw: str | None,
        rel_axis: str | None,
        rel_kw: str | None,
        cost_apply_nm_kw: str | None,
        stock_apply_nm_kw: str | None,
        add_user_kw: str | None,
        add_date_from: str | None,
        add_date_to: str | None,
        mod_user_kw: str | None,
        mod_date_from: str | None,
        mod_date_to: str | None,
    ) -> bool:
        params_out = _vendor_params_local(
            owner_kw=owner_kw,
            ven_nm_kw=ven_nm_kw,
            addr_kw=addr_kw,
            sm_kw=sm_kw,
            phone_kw=phone_kw,
            biz_kw=biz_kw,
            rel_axis=rel_axis,
            rel_kw=rel_kw,
            cost_apply_nm_kw=cost_apply_nm_kw,
            stock_apply_nm_kw=stock_apply_nm_kw,
            add_user_kw=add_user_kw,
            add_date_from=add_date_from,
            add_date_to=add_date_to,
            mod_user_kw=mod_user_kw,
            mod_date_from=mod_date_from,
            mod_date_to=mod_date_to,
        )

        try:
            params_out.update({
                "시도명": sido_nm or "",
                "시구군명": gugun_nm or "",
                "법정읍면동명": dong_nm or "",
                "도로명": road_nm or "",
                "도로명주소": road_addr_kw or "",
            })

            if vendor_scope:
                params_out.update({
                    "거래처코드구분": vendor_scope_label,
                })
        except NameError:
            pass

        query_summary = _vendor_query_summary_local(
            owner_kw=owner_kw,
            ven_nm_kw=ven_nm_kw,
            addr_kw=addr_kw,
            sm_kw=sm_kw,
            phone_kw=phone_kw,
            biz_kw=biz_kw,
            rel_axis=rel_axis,
            rel_kw=rel_kw,
            cost_apply_nm_kw=cost_apply_nm_kw,
            stock_apply_nm_kw=stock_apply_nm_kw,
            add_user_kw=add_user_kw,
            add_date_from=add_date_from,
            add_date_to=add_date_to,
            mod_user_kw=mod_user_kw,
            mod_date_from=mod_date_from,
            mod_date_to=mod_date_to,
        )

        road_summary_parts = []
        if sido_nm:
            road_summary_parts.append(f"시도명 {sido_nm}")
        if gugun_nm:
            road_summary_parts.append(f"시구군명 {gugun_nm}")
        if dong_nm:
            road_summary_parts.append(f"법정읍면동명 {dong_nm}")
        if road_nm:
            road_summary_parts.append(f"도로명 {road_nm}")
        if road_addr_kw:
            road_summary_parts.append(f"도로명주소 {road_addr_kw}")

        if road_summary_parts:
            query_summary = " / ".join(
                [x for x in [query_summary, " / ".join(road_summary_parts)] if x]
            )

        if vendor_scope_label:
            query_summary = " / ".join(
                [x for x in [f"거래처코드구분 {vendor_scope_label}", query_summary] if x]
            )

        summary_line = _vendor_summary_line(query_summary)
        display_message = f"{message}\n\n{summary_line}"

        result = {
            "title": "거래처 목록",
            "action": "거래처 목록",
            "type": "text",
            "final": True,
            "params": params_out,

            "data": display_message,
            "message": display_message,
            
            "meta": {
                "nlq": True,
                "master_nlq": True,
                "domain": "vendors",
                "nlq_query": txt,
                "_force_push": True,
                "_nlq_nonce": str(uuid.uuid4()),
                "row_count": 0,
                "row_count_total": 0,
                "source": "거래처마스터(Rddbc030)",
                "query_summary": query_summary,
                "summary_md": summary_line,
            },            
        }
        push_sims_result_to_chat(result, "거래처 목록")
        session_state["__scroll_to_msg"] = (
            session_state.get("__sims_last_msg_id") or session_state.get("__scroll_to_msg")
        )
        return True

    _t0 = (txt or "").strip()
    if _t0 and ("거래처" in _t0) and any(k in _t0 for k in ("SIMS", "sims")) and any(k in _t0 for k in ("데이터", "등록", "있어", "있나", "존재", "여부", "확인")):
        m = re.search(r"[\"'“”‘’]([^\"'“”‘’]+)[\"'“”‘’]", _t0)
        cand = (m.group(1).strip() if m else "")
        if not cand:
            m2 = re.search(r"거래처\s+([^\s,?.!]+)(?:\s+([^\s,?.!]+))?", _t0)
            if m2:
                c1 = (m2.group(1) or "").strip()
                c2 = (m2.group(2) or "").strip()
                cand = f"{c1} {c2}".strip() if c2 else c1
        cand = _norm_kw(_strip_tail_josa(cand or ""))
        forced_ven_nm_kw = cand if cand else None
    else:
        forced_ven_nm_kw = None

    if not _has_vendor_master_anchor(txt):
        return False

    logger.info("[nlq.vendors] handler-enter unfiltered_list=%s", _is_unfiltered_vendor_list_request(txt))

    owner_kw = _extract_owner_keyword(txt)
    ven_nm_kw = forced_ven_nm_kw or _extract_vendorname_keyword(txt)

    if not ven_nm_kw:
        ven_nm_kw = _extract_loose_vendorname_keyword(txt)

    phone_kw = _extract_phone_keyword(txt)
    biz_kw = _extract_bizno_keyword(txt)
    sm_kw = _extract_salesman_keyword(txt)

    addr_kw = _extract_address_keyword(txt)

    sido_nm = _extract_sido_keyword(txt)
    gugun_nm = _extract_gugun_keyword(txt)
    dong_nm = _extract_dong_keyword(txt)
    road_nm = _extract_road_name_keyword(txt)
    road_addr_kw = _extract_road_address_keyword(txt)

    loose_region_key, loose_region_kw = None, None

    # 명시 지역 표현일 때만 보조 지역 해석을 사용한다.
    # 예: "서울 지역 거래처 조회", "강남구 지역 거래처 조회"
    if (
        not ven_nm_kw
        and not any([owner_kw, phone_kw, biz_kw, sm_kw, addr_kw, sido_nm, gugun_nm, dong_nm, road_nm, road_addr_kw])
        and any(k in txt for k in ("지역", "시도", "시구군", "시군구", "법정동", "법정읍면동", "도로명"))
    ):
        loose_region_key, loose_region_kw = _extract_loose_region_vendor_keyword(txt)

    if loose_region_key and loose_region_kw:


        if loose_region_key == "sido_nm" and not sido_nm:
            sido_nm = loose_region_kw
        elif loose_region_key == "gugun_nm" and not gugun_nm:
            gugun_nm = loose_region_kw
        elif loose_region_key == "dong_nm" and not dong_nm:
            dong_nm = loose_region_kw
        elif loose_region_key == "road_nm" and not road_nm:
            road_nm = loose_region_kw

    rel_axis, rel_kw = _extract_relation_axis_keyword(txt)

    vendor_scope, vendor_scope_label = _extract_vendor_scope_filter(txt)
    if vendor_scope:
        # "매출처 전체" / "매입처 전체"는 거래처명/keyword 검색이 아니라
        # 거래처코드 대역 scope 조회다.
        rel_axis = None
        rel_kw = None
        ven_nm_kw = None

    cost_apply_nm_kw = _extract_cost_apply_name_keyword(txt)    
    stock_apply_nm_kw = _extract_stock_apply_name_keyword(txt)
    add_user_kw = _extract_add_user_keyword(txt)
    add_date_from, add_date_to = _extract_add_date_range(txt)
    mod_user_kw = _extract_mod_user_keyword(txt)
    mod_date_from, mod_date_to = _extract_mod_date_range(txt)

    owner_kw = _norm_kw(_strip_tail_josa(owner_kw or "")) or None
    ven_nm_kw = _norm_kw(_strip_tail_josa(ven_nm_kw or "")) or None
    phone_kw = _norm_kw(_strip_tail_josa(phone_kw or "")) or None
    biz_kw = _norm_kw(_strip_tail_josa(biz_kw or "")) or None
    sm_kw = _norm_kw(_strip_tail_josa(sm_kw or "")) or None

    addr_kw = _norm_kw(_strip_tail_josa(addr_kw or "")) or None

    sido_nm = _norm_kw(_strip_tail_josa(sido_nm or "")) or None
    gugun_nm = _norm_kw(_strip_tail_josa(gugun_nm or "")) or None
    dong_nm = _norm_kw(_strip_tail_josa(dong_nm or "")) or None
    road_nm = _norm_kw(_strip_tail_josa(road_nm or "")) or None
    road_addr_kw = _norm_kw(_strip_tail_josa(road_addr_kw or "")) or None

    rel_axis = (rel_axis or "").strip() or None

    rel_kw = _norm_kw(rel_kw or "") or None
    cost_apply_nm_kw = _norm_kw(cost_apply_nm_kw or "") or None
    stock_apply_nm_kw = _norm_kw(stock_apply_nm_kw or "") or None
    add_user_kw = _norm_kw(add_user_kw or "") or None
    mod_user_kw = _norm_kw(mod_user_kw or "") or None
    add_date_from = (add_date_from or "").strip() or None
    add_date_to = (add_date_to or "").strip() or None
    mod_date_from = (mod_date_from or "").strip() or None
    mod_date_to = (mod_date_to or "").strip() or None

    if "거래처명" not in txt and any([
        owner_kw, phone_kw, biz_kw, sm_kw, addr_kw,
        sido_nm, gugun_nm, dong_nm, road_nm, road_addr_kw,
        rel_kw, vendor_scope,
        cost_apply_nm_kw, stock_apply_nm_kw,
        add_user_kw, add_date_from, add_date_to,
        mod_user_kw, mod_date_from, mod_date_to,
    ]):
        ven_nm_kw = None

    has_filter = any([
        owner_kw, ven_nm_kw, phone_kw, biz_kw, sm_kw, addr_kw,
        sido_nm, gugun_nm, dong_nm, road_nm, road_addr_kw,
        rel_kw, vendor_scope,
        cost_apply_nm_kw, stock_apply_nm_kw,
        add_user_kw, add_date_from, add_date_to,
        mod_user_kw, mod_date_from, mod_date_to,
    ])
    if not has_filter and not _is_unfiltered_vendor_list_request(txt):
        return False

    from app.services import rddbc030_service as R03
    from app.sims.nlq.master_source_limit import resolve_chat_source_limit
    from app.sims.views.vendors import _prepare_vendor_display

    try:
        sig = inspect.signature(R03.search_vendors_full)
        accepted = set(sig.parameters.keys())
        accepts_var_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in sig.parameters.values()
        )


        action_cap = None
        kw_args = {
            "top": 2000,
            "only_active": True,
            "exclude_test": True,
        }

        # 채팅 NLQ 속도 보호:
        # 넓은 조건은 우선 500건까지만 조회한다.
        if cost_apply_nm_kw or stock_apply_nm_kw:
            kw_args["top"] = 500
            action_cap = 500

        if owner_kw and not any([ven_nm_kw, phone_kw, biz_kw, sm_kw, addr_kw]):
            kw_args["top"] = min(int(kw_args.get("top") or 1000), 500)
            action_cap = 500

        if owner_kw:
            kw_args["owner_nm"] = owner_kw

        if ven_nm_kw:
            # 거래처명 명시/loose 거래처명은 wide keyword 검색이 아니라
            # Rddbc030.Rd03_Ven_Nm 단일 조건으로 보내야 빠르다.
            if "ven_nm" in accepted:
                kw_args["ven_nm"] = ven_nm_kw
            elif "ven_nm_kw" in accepted:
                kw_args["ven_nm_kw"] = ven_nm_kw
            elif "keyword" in accepted:
                kw_args["keyword"] = ven_nm_kw


        if sm_kw:
            kw_args["sales_man_nm"] = sm_kw
        if phone_kw:
            kw_args["phone_kw"] = phone_kw

        if biz_kw:
            kw_args["biz_no"] = biz_kw

        if addr_kw:
            kw_args["addr_kw"] = addr_kw
        if sido_nm:
            kw_args["sido_nm"] = sido_nm
        if gugun_nm:
            kw_args["gugun_nm"] = gugun_nm
        if dong_nm:
            kw_args["dong_nm"] = dong_nm
        if road_nm:
            kw_args["road_nm"] = road_nm
        if road_addr_kw:
            kw_args["road_addr_kw"] = road_addr_kw


        if cost_apply_nm_kw:
            kw_args["cost_apply_nm"] = cost_apply_nm_kw
        if stock_apply_nm_kw:
            kw_args["stock_apply_nm"] = stock_apply_nm_kw

        # 단가적용처명/재고적용처명은 결과 범위가 넓고 조인이 무거우므로
        # 채팅 NLQ에서는 우선 500건까지만 조회한다.
        if cost_apply_nm_kw or stock_apply_nm_kw:
            kw_args["top"] = 500
            action_cap = 500

        if add_user_kw:
            kw_args["add_user_nm"] = add_user_kw
        if add_date_from:
            kw_args["add_date_from"] = add_date_from
        if add_date_to:
            kw_args["add_date_to"] = add_date_to

        if mod_user_kw:
            kw_args["mod_user_nm"] = mod_user_kw
        if mod_date_from:
            kw_args["mod_date_from"] = mod_date_from
        if mod_date_to:
            kw_args["mod_date_to"] = mod_date_to

        relation_scope_map = {
            "제조사": "maker",
            "매입처": "purchase",
            "매출처": "sales",
        }

        if vendor_scope and "scope" in accepted:
            kw_args["scope"] = vendor_scope
        elif rel_axis in relation_scope_map and "scope" in accepted:
            kw_args["scope"] = relation_scope_map[rel_axis]

        if rel_kw and not ven_nm_kw:            
            if "keyword" in accepted:
                kw_args["keyword"] = rel_kw
            elif "ven_nm" in accepted:
                kw_args["ven_nm"] = rel_kw

        kw_args["top"] = resolve_chat_source_limit(
            kw_args["top"],
            action_cap=action_cap,
        )

        call_args = (
            dict(kw_args)
            if accepts_var_kwargs
            else {k: v for k, v in kw_args.items() if k in accepted}
        )

        logger.info(
            "[nlq.vendors] source-limit requested_top=%s effective_top=%s action_cap=%s",
            2000,
            call_args.get("top"),
            action_cap,
        )

        logger.info(
            "[nlq.vendors] extracted owner=%r ven_nm=%r sm=%r phone=%r biz=%r "
            "addr=%r sido=%r gugun=%r dong=%r road=%r road_addr=%r "
            "add_user=%r mod_user=%r",
            owner_kw, ven_nm_kw, sm_kw, phone_kw, biz_kw,
            addr_kw, sido_nm, gugun_nm, dong_nm, road_nm, road_addr_kw,
            add_user_kw, mod_user_kw
        )

        logger.info("[nlq.vendors] call_args=%s (from=%r)", call_args, txt)

        df_raw = R03.search_vendors_full(**call_args)
        logger.info("[nlq.vendors] rows=%s", 0 if df_raw is None else len(df_raw))
        try:
            source_limit = int(call_args.get("top") or 0)
        except (TypeError, ValueError):
            source_limit = 0
        source_limit_hit = source_limit > 0 and (0 if df_raw is None else len(df_raw)) >= source_limit

        if df_raw is None or df_raw.empty:
            return _push_vendor_text_local(
                message="해당 조회조건의 자료가 없습니다.",
                owner_kw=owner_kw,
                ven_nm_kw=ven_nm_kw,
                addr_kw=addr_kw,
                sm_kw=sm_kw,
                phone_kw=phone_kw,
                biz_kw=biz_kw,
                rel_axis=rel_axis,
                rel_kw=rel_kw,
                cost_apply_nm_kw=cost_apply_nm_kw,
                stock_apply_nm_kw=stock_apply_nm_kw,
                add_user_kw=add_user_kw,
                add_date_from=add_date_from,
                add_date_to=add_date_to,
                mod_user_kw=mod_user_kw,
                mod_date_from=mod_date_from,
                mod_date_to=mod_date_to,
            )

        # 목록 화면과 동일한 alias / 컬럼 순서 사용
        df_view = _prepare_vendor_display(df_raw)

        # 주소
        if addr_kw:
            cols = [c for c in ("상세주소", "상세주소2", "주소", "주소2") if c in df_view.columns]
            if cols:
                m = None
                for c in cols:
                    cur = _contains_no_space(df_view[c], addr_kw)
                    m = cur if m is None else (m | cur)
                if m is not None:
                    df_view = df_view[m]
            logger.info("[nlq.vendors] address-filter addr_kw=%r rows=%s", addr_kw, len(df_view))

        # 도로명주소 / 지역 후필터
        if sido_nm and "시도명" in df_view.columns:
            df_view = df_view[_contains_no_space(df_view["시도명"], sido_nm)]

        if gugun_nm and "시구군명" in df_view.columns:
            df_view = df_view[_contains_no_space(df_view["시구군명"], gugun_nm)]

        if dong_nm and "법정읍면동명" in df_view.columns:
            df_view = df_view[_contains_no_space(df_view["법정읍면동명"], dong_nm)]

        if road_nm and "도로명" in df_view.columns:
            df_view = df_view[_contains_no_space(df_view["도로명"], road_nm)]

        if road_addr_kw:
            cols = [c for c in ("도로명주소", "도로명", "시도명", "시구군명", "법정읍면동명") if c in df_view.columns]
            if cols:
                m = None
                for c in cols:
                    cur = _contains_no_space(df_view[c], road_addr_kw)
                    m = cur if m is None else (m | cur)
                if m is not None:
                    df_view = df_view[m]

        logger.info(
            "[nlq.vendors] road-filter sido=%r gugun=%r dong=%r road=%r road_addr=%r rows=%s",
            sido_nm, gugun_nm, dong_nm, road_nm, road_addr_kw, len(df_view)
        )

        # 전화
        if phone_kw:
            cols = [c for c in ("전화번호", "핸드폰", "팩스번호") if c in df_view.columns]
            if cols:
                p = _digits_only_local(phone_kw)
                if p:
                    def _match_phone(row):
                        for c in cols:
                            if p in _digits_only_local(str(row.get(c) or "")):
                                return True
                        return False
                    df_view = df_view[df_view.apply(_match_phone, axis=1)]
                else:
                    m = None
                    for c in cols:
                        cur = _contains_no_space(df_view[c], phone_kw)
                        m = cur if m is None else (m | cur)
                    if m is not None:
                        df_view = df_view[m]

        # 사업자번호
        if biz_kw and "사업자등록번호" in df_view.columns:
            b = _digits_only_local(biz_kw)
            if b:
                df_view = df_view[
                    df_view["사업자등록번호"].astype(str).apply(lambda x: b in _digits_only_local(x))
                ]
            else:
                df_view = df_view[_contains_no_space(df_view["사업자등록번호"], biz_kw)]

        # 대표자명
        if owner_kw and "대표자명" in df_view.columns:
            s = df_view["대표자명"].astype(str).str.replace(" ", "", regex=False)
            if len(owner_kw) == 1:
                df_view = df_view[s.str.startswith(owner_kw, na=False)]
            else:
                df_view = df_view[s.str.contains(owner_kw, na=False)]

        # 거래처명
        if ven_nm_kw and "거래처명" in df_view.columns:
            df_view = df_view[_contains_no_space(df_view["거래처명"], ven_nm_kw)]

        # 영업사원명
        if sm_kw:
            if "영업사원명" in df_view.columns:
                df_view = df_view[_contains_no_space(df_view["영업사원명"], sm_kw)]
            elif "영업사원" in df_view.columns:
                df_view = df_view[_contains_no_space(df_view["영업사원"], sm_kw)]

        # 단가/재고 적용처명
        if cost_apply_nm_kw and "단가적용처명" in df_view.columns:
            df_view = df_view[_contains_no_space(df_view["단가적용처명"], cost_apply_nm_kw)]

        if stock_apply_nm_kw and "재고적용처명" in df_view.columns:
            df_view = df_view[_contains_no_space(df_view["재고적용처명"], stock_apply_nm_kw)]

        # 등록자 / 등록일자
        if add_user_kw:
            for c in ("등록자", "등록자코드"):
                if c in df_view.columns:
                    df_view = df_view[_contains_no_space(df_view[c], add_user_kw)]
                    break

        if add_date_from or add_date_to:
            df_view = _apply_date_range(df_view, "등록일자", add_date_from, add_date_to)

        # 수정자 / 수정일자
        if mod_user_kw:
            for c in ("수정자", "수정자코드"):
                if c in df_view.columns:
                    df_view = df_view[_contains_no_space(df_view[c], mod_user_kw)]
                    break

        if mod_date_from or mod_date_to:
            df_view = _apply_date_range(df_view, "수정일자", mod_date_from, mod_date_to)

        if df_view is None or len(df_view) == 0:
            return _push_vendor_text_local(
                message="해당 조회조건의 자료가 없습니다.",
                owner_kw=owner_kw,
                ven_nm_kw=ven_nm_kw,
                addr_kw=addr_kw,
                sm_kw=sm_kw,
                phone_kw=phone_kw,
                biz_kw=biz_kw,
                rel_axis=rel_axis,
                rel_kw=rel_kw,
                cost_apply_nm_kw=cost_apply_nm_kw,
                stock_apply_nm_kw=stock_apply_nm_kw,
                add_user_kw=add_user_kw,
                add_date_from=add_date_from,
                add_date_to=add_date_to,
                mod_user_kw=mod_user_kw,
                mod_date_from=mod_date_from,
                mod_date_to=mod_date_to,
            )

        total = int(len(df_view))

        # 화면 표시는 제한
        show_n = min(total, 500)
        df_show = df_view.head(show_n).copy()

        # LLM 컨텍스트는 전체 결과 기준으로 만들되,
        # records는 과도하게 크지 않도록 1000건 샘플만 제공
        sample_records = df_view.head(min(len(df_view), 300)).to_dict(orient="records")
        
        aggs = getattr(df_raw, "attrs", {}).get("aggregations") or {}

        session_state["__sims_ctx"] = {
            "action": "거래처 목록",
            "meta": {
                "row_count_total": total,
                "sample_rows": min(300, len(df_view)),
                "cols": list(df_view.columns),
            },
            "records": sample_records,
            "aggregations": aggs,
        }

        params_out = _vendor_params_local(
            owner_kw=owner_kw,
            ven_nm_kw=ven_nm_kw,
            addr_kw=addr_kw,
            sm_kw=sm_kw,
            phone_kw=phone_kw,
            biz_kw=biz_kw,
            rel_axis=rel_axis,
            rel_kw=rel_kw,
            cost_apply_nm_kw=cost_apply_nm_kw,
            stock_apply_nm_kw=stock_apply_nm_kw,
            add_user_kw=add_user_kw,
            add_date_from=add_date_from,
            add_date_to=add_date_to,
            mod_user_kw=mod_user_kw,
            mod_date_from=mod_date_from,
            mod_date_to=mod_date_to,
        )

        params_out.update({
            "시도명": sido_nm or "",
            "시구군명": gugun_nm or "",
            "법정읍면동명": dong_nm or "",
            "도로명": road_nm or "",
            "도로명주소": road_addr_kw or "",
        })

        if vendor_scope:
            params_out.update({
                "거래처코드구분": vendor_scope_label,
            })

        query_summary = _vendor_query_summary_local(
            owner_kw=owner_kw,
            ven_nm_kw=ven_nm_kw,
            addr_kw=addr_kw,
            sm_kw=sm_kw,
            phone_kw=phone_kw,
            biz_kw=biz_kw,
            rel_axis=rel_axis,
            rel_kw=rel_kw,
            cost_apply_nm_kw=cost_apply_nm_kw,
            stock_apply_nm_kw=stock_apply_nm_kw,
            add_user_kw=add_user_kw,
            add_date_from=add_date_from,
            add_date_to=add_date_to,
            mod_user_kw=mod_user_kw,
            mod_date_from=mod_date_from,
            mod_date_to=mod_date_to,
        )

        road_summary_parts = []
        if sido_nm:
            road_summary_parts.append(f"시도명 {sido_nm}")
        if gugun_nm:
            road_summary_parts.append(f"시구군명 {gugun_nm}")
        if dong_nm:
            road_summary_parts.append(f"법정읍면동명 {dong_nm}")
        if road_nm:
            road_summary_parts.append(f"도로명 {road_nm}")
        if road_addr_kw:
            road_summary_parts.append(f"도로명주소 {road_addr_kw}")

        if road_summary_parts:
            query_summary = " / ".join(
                [x for x in [query_summary, " / ".join(road_summary_parts)] if x]
            )

        if vendor_scope_label:
            query_summary = " / ".join(
                [x for x in [f"거래처코드구분 {vendor_scope_label}", query_summary] if x]
            )

        summary_line = _vendor_summary_line(query_summary)

        summary_basis = "전체 조회결과 기준"
        if source_limit_hit:
            note = f"조회결과: **{total}건** (조회 상한 {source_limit}건 도달; 전체 건수 미확인)"
            summary_basis = "조회 상한 내 결과 기준"
        elif show_n >= total:
            note = f"조회결과: **{total}건** (전부 표시)"
        else:
            note = f"조회결과: **{total}건** (표시는 상위 {show_n}건) — 더 보려면 조건을 좁히거나 패널 필터를 사용하세요."

        session_state["__sims_context_note"] = f"{summary_line}\n\n{note}"

        vendor_master_summary = {}
        llm_summary_md = ""

        if callable(_build_vendor_master_llm_summary):
            try:
                vendor_master_summary = _build_vendor_master_llm_summary(
                    df_view,
                    query_condition=f"{query_summary or '전체'}\n{note}",
                    total=total,
                    display_count=show_n,
                )
                llm_summary_md = str(vendor_master_summary.get("llm_summary_md") or "")
            except Exception:
                vendor_master_summary = {}
                llm_summary_md = ""


        result = {
            "final": True,
            "type": "table",
            "title": "거래처 목록",
            "action": "거래처 목록",
            "params": params_out,
            "columns": list(df_show.columns),
            # df는 LLM/다운로드/전체 기준
            "df": df_view,

            # df_display는 화면 표시 기준
            "df_display": df_show,

            # records는 화면 표시용 대표 레코드
            "records": df_show.to_dict(orient="records"),


            "message": (
                f"거래처 목록 {total:,}건 (조회 상한 도달: 전체 건수 미확인)"
                if source_limit_hit
                else f"거래처 목록 {total:,}건"
            ),
            "meta": {
                "nlq": True,
                "master_nlq": True,
                "domain": "vendors",
                "nlq_query": txt,
                "condition": query_summary or "전체",
                "note": note,
                "_force_push": True,
                "_nlq_nonce": str(uuid.uuid4()),

                "row_count": int(total),
                "row_count_total": int(total),
                "display_row_count": int(show_n),
                "show_n": int(show_n),
                "source_limit": source_limit,
                "source_limit_hit": source_limit_hit,
                "scope": vendor_scope,
                "scope_label": vendor_scope_label,
                "analysis_type": "vendor_master",
                "llm_summary_kind": "vendor_master_summary",
                "llm_summary_md": llm_summary_md,
                "vendor_master_summary": vendor_master_summary,
                "analysis_row_count": int(total),
                "row_count_total_for_analysis": int(total),
                "summary_basis": summary_basis,
                "summary_md": note,
                "source": "거래처마스터(Rddbc030)",
                "ts": dt.datetime.now().isoformat(timespec="seconds"),
                "query_summary": query_summary,

            },            
        }

        push_sims_result_to_chat(result, "거래처 목록")
        logger.info(
            "[nlq.vendors] handler-complete handled=True rows=%s source_limit=%s",
            total,
            source_limit,
        )
        session_state["__scroll_to_msg"] = (
            session_state.get("__sims_last_msg_id") or session_state.get("__scroll_to_msg")
        )
        return True

    except Exception:
        logger.exception("[nlq.vendors] failed")
        return _push_vendor_text_local(
            message="거래처 조회 중 오류가 발생했습니다. 조회조건을 다시 확인해 주세요.",
            owner_kw=owner_kw,
            ven_nm_kw=ven_nm_kw,
            addr_kw=addr_kw,
            sm_kw=sm_kw,
            phone_kw=phone_kw,
            biz_kw=biz_kw,
            rel_axis=rel_axis,
            rel_kw=rel_kw,
            cost_apply_nm_kw=cost_apply_nm_kw,
            stock_apply_nm_kw=stock_apply_nm_kw,
            add_user_kw=add_user_kw,
            add_date_from=add_date_from,
            add_date_to=add_date_to,
            mod_user_kw=mod_user_kw,
            mod_date_from=mod_date_from,
            mod_date_to=mod_date_to,
        )
