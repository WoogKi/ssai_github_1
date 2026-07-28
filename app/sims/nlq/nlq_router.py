# app/sims/nlq/nlq_router.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Callable, Any, Dict
import re
import uuid
import datetime as dt
import os

import importlib
import pandas as pd
from app.services.ssai_analysis_profile_service import (
    normalize_business_code,
    normalize_business_code_pair,
)
 
# =============================================================================
# 키보드 보정(2벌식): 영문으로 잘못 입력된 한글을 한글로 변환
# - 예) "zjsxprtmxm anjdi?" -> "컨텍스트 뭐야?"
# =============================================================================
_CHO = ['ㄱ','ㄲ','ㄴ','ㄷ','ㄸ','ㄹ','ㅁ','ㅂ','ㅃ','ㅅ','ㅆ','ㅇ','ㅈ','ㅉ','ㅊ','ㅋ','ㅌ','ㅍ','ㅎ']
_JUNG = ['ㅏ','ㅐ','ㅑ','ㅒ','ㅓ','ㅔ','ㅕ','ㅖ','ㅗ','ㅘ','ㅙ','ㅚ','ㅛ','ㅜ','ㅝ','ㅞ','ㅟ','ㅠ','ㅡ','ㅢ','ㅣ']
_JONG = ['','ㄱ','ㄲ','ㄳ','ㄴ','ㄵ','ㄶ','ㄷ','ㄹ','ㄺ','ㄻ','ㄼ','ㄽ','ㄾ','ㄿ','ㅀ','ㅁ','ㅂ','ㅄ','ㅅ','ㅆ','ㅇ','ㅈ','ㅊ','ㅋ','ㅌ','ㅍ','ㅎ']

_CONSONANT_KEYS = {
    'r':'ㄱ','R':'ㄲ','s':'ㄴ','e':'ㄷ','E':'ㄸ','f':'ㄹ','a':'ㅁ','q':'ㅂ','Q':'ㅃ',
    't':'ㅅ','T':'ㅆ','d':'ㅇ','w':'ㅈ','W':'ㅉ','c':'ㅊ','z':'ㅋ','x':'ㅌ','v':'ㅍ','g':'ㅎ',
}
_VOWEL_KEYS = {
    'k':'ㅏ','o':'ㅐ','i':'ㅑ','O':'ㅒ','j':'ㅓ','p':'ㅔ','u':'ㅕ','P':'ㅖ','h':'ㅗ','y':'ㅛ',
    'n':'ㅜ','b':'ㅠ','m':'ㅡ','l':'ㅣ',
}
_COMPOUND_VOWEL_KEYS = {'hk':'ㅘ','ho':'ㅙ','hl':'ㅚ','nj':'ㅝ','np':'ㅞ','nl':'ㅟ','ml':'ㅢ'}
_DOUBLE_FINAL_FROM_PAIR = {
    ('ㄱ','ㅅ'):'ㄳ', ('ㄴ','ㅈ'):'ㄵ', ('ㄴ','ㅎ'):'ㄶ', ('ㄹ','ㄱ'):'ㄺ', ('ㄹ','ㅁ'):'ㄻ',
    ('ㄹ','ㅂ'):'ㄼ', ('ㄹ','ㅅ'):'ㄽ', ('ㄹ','ㅌ'):'ㄾ', ('ㄹ','ㅍ'):'ㄿ', ('ㄹ','ㅎ'):'ㅀ',
    ('ㅂ','ㅅ'):'ㅄ',
}
_DOUBLE_FINAL_SPLIT = {v:k for k,v in _DOUBLE_FINAL_FROM_PAIR.items()}

_CHO_MAP = {j:i for i,j in enumerate(_CHO)}
_JUNG_MAP = {j:i for i,j in enumerate(_JUNG)}
_JONG_MAP = {j:i for i,j in enumerate(_JONG) if j}
_CONS_SET = set(_CHO_MAP.keys()) | set(_JONG_MAP.keys()) | set(_DOUBLE_FINAL_SPLIT.keys())
_VOW_SET = set(_JUNG_MAP.keys())

def keyboard_fix(text: str) -> str:
    """영문(2벌식 키 입력)을 한글로 조합. 한글이 이미 있으면 그대로."""
    if not text:
        return text
    if re.search(r"[가-힣]", text):
        return text

    def _keys_to_jamos(s: str):
        out = []
        i = 0
        while i < len(s):
            pair = s[i:i+2]
            if pair in _COMPOUND_VOWEL_KEYS:
                out.append(_COMPOUND_VOWEL_KEYS[pair]); i += 2; continue
            ch = s[i]
            if ch in _CONSONANT_KEYS:
                out.append(_CONSONANT_KEYS[ch]); i += 1; continue
            if ch in _VOWEL_KEYS:
                out.append(_VOWEL_KEYS[ch]); i += 1; continue
            out.append(ch); i += 1
        return out

    def _compose(jamos):
        res = []
        L = V = T = None

        def flush():
            nonlocal L,V,T
            if L is None and V is None and T is None:
                return
            if V is None:
                if L is not None: res.append(L)
                if T is not None: res.append(T)
            else:
                if L is None: L = 'ㅇ'
                li = _CHO_MAP.get(L)
                vi = _JUNG_MAP.get(V)
                ti = _JONG_MAP.get(T, 0) if T else 0
                if li is None or vi is None:
                    if L: res.append(L)
                    if V: res.append(V)
                    if T: res.append(T)
                else:
                    res.append(chr(0xAC00 + (li*21 + vi)*28 + ti))
            L = V = T = None

        i = 0
        while i < len(jamos):
            cur = jamos[i]
            nxt = jamos[i+1] if i+1 < len(jamos) else None

            if cur in _VOW_SET:
                if V is None:
                    if L is None: L = 'ㅇ'
                    V = cur
                else:
                    flush()
                    L = 'ㅇ'; V = cur
                i += 1
                continue

            if cur in _CONS_SET:
                if L is None:
                    L = cur
                elif V is None:
                    flush()
                    L = cur
                elif T is None:
                    if nxt in _VOW_SET:
                        flush()
                        L = cur
                    else:
                        T = cur
                else:
                    if (T, cur) in _DOUBLE_FINAL_FROM_PAIR and not (nxt in _VOW_SET):
                        T = _DOUBLE_FINAL_FROM_PAIR[(T, cur)]
                    else:
                        flush()
                        L = cur
                i += 1
                continue

            flush()
            res.append(cur)
            i += 1

        flush()
        return ''.join(res)

    return _compose(_keys_to_jamos(text))

_VENDOR_MASTER_ATTR_WORDS = (
    "매입처", "매출처", "제조사", "발주처",
    "단가적용처", "단가적용처명",
    "재고적용처", "재고적용처명",
    "영업사원", "영업사원명",
    "수정자", "수정자명", "수정일자",
    "대표자", "대표자명",
    "사업자번호", "사업자등록번호",
    "주소", "소재지",

    "도로명주소", "도로명",
    "시도명", "시구군명",
    "법정읍면동명", "법정동명",
    "지역",

)

_VENDOR_TXN_SIGNAL_WORDS = (
    "내역", "거래내역", "현황", "명세", "이력",
    "전표", "집계", "검증", "수불", "재고",
)

_PRODUCT_SIGNAL_WORDS = (
    "제품", "상품", "보험", "바코드", "제품코드", "보험코드", "품목",
)

_PRODUCT_MASTER_ATTR_WORDS = (
    "제품그룹명",
    "구분명",
    "제품분류명",
    "수정자",
    "수정자명",
    "수정일자",
    "제약사",
    "제약사명",
    "제조사",
    "제조사명",
    "보험코드",
    "바코드",
    "제품코드",
    "제품명",
    "상품명",
)

def _normalize_io_action_spacing(txt: str) -> str:
    """
    IO/NLQ action 명칭의 띄어쓰기 변형을 표준 명칭으로 보정한다.

    예:
    - 제품 수불 현황 직듀오서방정
      → 제품수불현황 직듀오서방정
    """
    t = str(txt or "").strip()
    if not t:
        return ""

    replacements = (
        (r"제품\s*수불\s*현황", "제품수불현황"),
        (r"제품\s*수불\s*부", "제품수불부"),
        (r"제품\s*수불", "제품수불"),
        (r"제품\s*재고\s*현황", "제품재고현황"),
        (r"제품\s*재고\s*장", "제품재고장"),
        (r"실\s*재고\s*월\s*집계", "실재고월집계"),
        (r"장부\s*재고\s*월\s*집계", "장부재고월집계"),
        (r"입고\s*명세", "입고명세"),
        (r"출고\s*명세", "출고명세"),
        (r"거래\s*명세서\s*공통", "거래명세서 공통"),
        (r"세금\s*계산서\s*공통", "세금계산서 공통"),
    )

    for pat, repl in replacements:
        t = re.sub(pat, repl, t)

    return re.sub(r"\s+", " ", t).strip()

# 직전 SIMS 결과를 대상으로 한 후속 분석 질문인지 판단한다.
def _has_recent_sims_context() -> bool:
    try:
        from app.ui.chat_bridge import get_sims_context_data

        try:
            pack = get_sims_context_data(max_age_sec=86400)
        except TypeError:
            pack = get_sims_context_data()

        return isinstance(pack, dict) and bool(pack)
    except Exception:
        return False


def _looks_like_current_sims_followup_analysis(txt: str) -> bool:
    """
    직전 SIMS 결과를 대상으로 한 후속 분석 질문인지 판단한다.

    예:
    - 제품별 매출 금액 및 수량을 조회하고, 상위 20개 제품을 보여줘
    - 거래처별 매출 상위 20개 보여줘
    - 재고위치별 출고수량 합계 알려줘
    - 담당자별 매출금액 분석해줘
    """
    t = str(txt or "").strip()
    if not t:
        return False

    explicit_current_words = (
        "현재표",
        "현재 표",
        "현재 조회",
        "방금 조회",
        "위 표",
        "이 표",
        "조회 결과",
        "현재 결과",
    )

    group_words = (
        "제품별",
        "품목별",
        "거래처별",
        "재고위치별",
        "담당자별",
        "영업사원별",
        "일자별",
        "월별",
    )

    analysis_words = (
        "상위",
        "TOP",
        "top",
        "금액",
        "수량",
        "합계",
        "비중",
        "분포",
        "집계",
        "분석",
        "요약",
        "보여줘",
        "알려줘",
    )

    if any(w in t for w in explicit_current_words):
        return _has_recent_sims_context()

    if any(w in t for w in group_words) and any(w in t for w in analysis_words):
        return _has_recent_sims_context()

    return False


def _append_lookup_verb_for_io(txt: str) -> str:
    """
    IO action은 있는데 '조회'가 빠진 문장도 조회 의도로 처리한다.
    """
    t = str(txt or "").strip()
    if not t:
        return t

    if re.search(r"(조회|검색|찾아|보여줘|알려줘|확인)\s*$", t):
        return t

    if _is_io_inventory_phrase(t) or _is_explicit_io_nlq_phrase(t):
        return f"{t} 조회"

    return t

def _is_io_inventory_phrase(txt: str) -> bool:
    t = _normalize_io_action_spacing(txt)
    if not t:
        return False

    return any(
        k in t
        for k in (
            "제품수불현황",
            "제품수불부",
            "제품수불",
            "제품재고현황",
            "제품재고장",
            "재고현황",
            "재고장",
        )
    )

def _is_explicit_io_nlq_phrase(txt: str) -> bool:
    """
    입출고/명세서/재고 action이 문장에 명시된 경우.
    이런 문장은 거래처/제품 마스터 NLQ가 먼저 가로채면 안 된다.
    """
    t = _normalize_io_action_spacing(txt)
    if not t:
        return False

    explicit_actions = (
        "입고명세",
        "출고명세",
        "거래명세서 공통",
        "세금계산서 공통",
        "실재고월집계",
        "장부재고월집계",
        "제품수불현황",
        "제품수불부",
        "제품수불",
        "제품재고현황",
        "제품재고장",
        "재고현황",
        "재고장",
    )

    if any(k in t for k in explicit_actions):
        return True

    # 검증 4종
    has_check = any(k in t for k in ("불일치", "검증", "누락"))
    has_side = any(k in t for k in ("입고", "매입", "출고", "매출"))
    has_doc = any(k in t for k in ("거래명세서", "세금계산서"))

    if has_check and has_side and has_doc:
        return True

    return False


_IO_PICK_ORDINALS = {
    "첫번째": 1,
    "첫 번째": 1,
    "첫번": 1,
    "첫 번": 1,
    "두번째": 2,
    "두 번째": 2,
    "두번": 2,
    "두 번": 2,
    "세번째": 3,
    "세 번째": 3,
    "세번": 3,
    "세 번": 3,
    "네번째": 4,
    "네 번째": 4,
    "네번": 4,
    "네 번": 4,
    "다섯번째": 5,
    "다섯 번째": 5,
    "여섯번째": 6,
    "일곱번째": 7,
    "여덟번째": 8,
    "아홉번째": 9,
    "열번째": 10,
}

def _extract_pending_product_choice_index(txt: str) -> int | None:
    t = (txt or "").strip()
    if not t:
        return None

    m = re.search(r"([1-9]\d*)\s*번(?:으로)?(?:\s*조회|\s*선택)?", t)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None

    for key, idx in _IO_PICK_ORDINALS.items():
        if key in t:
            return idx

    return None

def _clear_pending_product_pick(session_state: Dict[str, Any]) -> None:
    """제품 후보 선택 상태를 정리한다."""
    try:
        session_state.pop("__io_pending_product_pick", None)
        session_state.pop("__io_keep_pending_product_pick_after_result", None)
    except Exception:
        pass


def _is_pending_pick_cancel_text(txt: str) -> bool:
    t = str(txt or "").strip().replace(" ", "")
    return t in {
        "취소",
        "후보취소",
        "선택취소",
        "제품선택취소",
        "제품후보취소",
        "그만",
    }


def _has_pending_product_pick(session_state: Dict[str, Any]) -> bool:
    pending = session_state.get("__io_pending_product_pick")
    if not isinstance(pending, dict):
        return False
    candidates = pending.get("candidates")
    return bool(pending.get("action")) and isinstance(candidates, list) and bool(candidates)

def _should_try_goods_before_users(txt: str) -> bool:
    """
    제품 마스터 의도면 goods를 users보다 먼저 태운다.

    예:
    - "수정자 관리자 제품조회" -> True
    - "수정일자 20230329 제품 조회" -> True
    - "제품그룹명 금융 제품 조회" -> True
    - "구분명 의약 제품 조회" -> True
    - "제품분류명 일반 제품 조회" -> True
    - "수정자 홍길동 사용자 조회" -> False
    """
    t = (txt or "").strip()
    if not t:
        return False

    has_product_signal = any(k in t for k in _PRODUCT_SIGNAL_WORDS)
    has_product_attr = any(k in t for k in _PRODUCT_MASTER_ATTR_WORDS)
    has_product_verb = any(k in t for k in (
        "조회", "검색", "목록", "찾아", "찾아줘", "찾아봐", "보여줘", "알려줘",
        "등록", "있어", "있나", "존재", "여부", "확인",
    ))

    # 제품/상품 시그널이 있으면 goods 우선
    if has_product_signal and (has_product_attr or has_product_verb):
        return True

    return False

def _has_vendor_txn_signal(txt: str) -> bool:
    t = (txt or "").strip()
    return any(k in t for k in _VENDOR_TXN_SIGNAL_WORDS)

def _should_try_vendors_before_goods(txt: str) -> bool:
    """
    거래처 마스터 의도면 vendors를 goods보다 먼저 태운다.

    예:
    - "동아 제조사 거래처 조회"            -> True
    - "한미 매입처에서 거래처 조회 해줘"   -> True
    - "단가적용처명 다라메 거래처 조회"    -> True
    - "매입처 한미 거래처 내역 조회"       -> False
    - "한미 제조사 제품 조회"              -> False
    """
    t = (txt or "").strip()
    if not t:
        return False

    # 거래/집계 신호가 있으면 거래처 마스터 우선 라우팅 금지
    if _has_vendor_txn_signal(t):
        return False

    has_vendor_anchor = any(k in t for k in (
        "거래처", "거래처명", "거래처코드",
        "거래처목록", "거래처 목록",
        "거래처마스터", "거래처 마스터",
        "SIMS", "sims",
    ))
    has_vendor_attr = any(k in t for k in _VENDOR_MASTER_ATTR_WORDS)
    has_master_verb = any(k in t for k in (
        "조회", "검색", "목록", "마스터",
        "찾아", "찾아줘", "찾아봐", "보여줘", "알려줘",
        "등록", "있어", "있나", "존재", "여부", "확인",
    ))
    has_product_signal = any(k in t for k in _PRODUCT_SIGNAL_WORDS)

    # 제품 의도가 강하고 거래처 앵커가 없으면 goods로 둔다.
    if has_product_signal and ("거래처" not in t):
        return False

    # 거래처 조회/목록/마스터/검색 + 속성어 조합은 vendors 우선
    if ("거래처" in t) and (has_vendor_attr or has_master_verb):
        return True

    # SIMS 등록/존재 여부 확인 류도 vendors 우선
    if has_vendor_anchor and has_master_verb and any(k in t for k in ("등록", "있어", "있나", "존재", "여부", "확인")):
        return True

    return False

def _should_allow_vendors_fallback(txt: str) -> bool:
    """
    goods가 처리하지 못한 뒤 vendors를 마지막으로 태워도 되는지 판단.
    명시적 거래/집계 의도는 vendors로 보내지 않는다.
    """
    t = (txt or "").strip()
    if not t:
        return False

    if _has_vendor_txn_signal(t):
        return False

    has_product_signal = any(k in t for k in _PRODUCT_SIGNAL_WORDS)
    has_vendor_anchor = any(k in t for k in (
        "거래처", "거래처명", "거래처코드",
        "거래처목록", "거래처 목록",
        "거래처마스터", "거래처 마스터",
        "SIMS", "sims",
    ))

    if has_product_signal and not has_vendor_anchor:
        return False

    return True

def _try_answer_ctx_meta_question(
    txt: str,
    room: Dict[str, Any],
    session_state: Dict[str, Any],
    make_ts: Callable[[], str],
    next_seq: Callable[[], int],
    logger,
) -> bool:
    """컨텍스트(meta) 관련 질문은 LLM 없이 SSOT(chat_bridge)로 즉시 응답."""
    t = (txt or "").strip()
    if not t:
        return False

    # 트리거: 컨텍스트/행수/컬럼/records/meta/집계 등
    meta_pat = r"(컨텍스트|context|행\s*수|몇\s*건|총\s*몇|컬럼|열\s*목록|필드|스키마|meta|records|aggregations|요약)"
    if not re.search(meta_pat, t, flags=re.IGNORECASE):
        return False

    try:
        from app.ui.chat_bridge import get_sims_context_data
    except Exception:
        logger.exception("[nlq.router] failed to import chat_bridge")
        return False

    try:
        pack = get_sims_context_data(max_age_sec=86400)
    except TypeError:
        pack = get_sims_context_data()
    except Exception:
        logger.exception("[nlq.router] get_sims_context_data failed")
        pack = None

    if not isinstance(pack, dict) or not pack:
        msg = "현재 SIMS 컨텍스트가 없습니다. 먼저 SIMS 조회(예: 거래처 목록)를 실행해 주세요."
        room.setdefault("messages", []).append({
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": msg,
            "time": make_ts(),
            "seq": next_seq(),
        })
        return True

    # chat_bridge 패킹(호환):
    # - pack={action, ts, params, data:{columns,records,meta}} 또는
    # - data_container(dict) 직접
    data = pack.get("data") if isinstance(pack.get("data"), dict) else pack
    meta = (data.get("meta") or {}) if isinstance(data, dict) else {}
    cols = (data.get("columns") or meta.get("columns") or []) if isinstance(data, dict) else []

    action = meta.get("action") or pack.get("action") or "(unknown)"
    params = meta.get("params") or pack.get("params") or {}
    ts = meta.get("ts") or pack.get("ts")

    row_count = meta.get("row_count")
    row_total = meta.get("row_count_total") or row_count
    col_count = meta.get("column_count") or (len(cols) if isinstance(cols, list) else None)

    wants_cols = bool(re.search(r"(컬럼|열\s*목록|필드|columns?)", t, flags=re.IGNORECASE))
    wants_aggs = bool(re.search(r"(집계|aggregations|요약)", t, flags=re.IGNORECASE))

    lines = [
        "현재 SIMS 컨텍스트 요약",
        "",
        f"action: {action}",
        f"rows: {row_total}",
        f"cols: {col_count}",
    ]
    if ts is not None:
        lines.append(f"ts: {ts}")
    if params:
        lines.append(f"params: {params}")

    if wants_cols and isinstance(cols, list) and cols:
        lines.append("")
        lines.append("columns:")
        lines.append(", ".join([str(c) for c in cols[:80]]))
        if len(cols) > 80:
            lines.append(f"... (+{len(cols)-80}개)")

    if wants_aggs:
        aggs = data.get("aggregations") if isinstance(data, dict) else None
        if isinstance(aggs, dict) and aggs:
            keys = ", ".join(list(aggs.keys())[:30])
            tail = "" if len(aggs) <= 30 else f" ... (+{len(aggs)-30}개)"
            lines.append("")
            lines.append(f"aggregations keys: {keys}{tail}")
        else:
            lines.append("")
            lines.append("aggregations: (없음)")

    msg = "\n".join(lines)
    room.setdefault("messages", []).append({
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": msg,
        "time": make_ts(),
        "seq": next_seq(),
    })
    return True

# =============================================================================
# 분석/KPI NLQ
# - 품목별 매출 추세 분석
# - 품목별 매출 추세 요약표
# - 품목별 매출 예상
# - 품목별 재고부족현황
# - 제약사별 매출 추세 분석
# - 제약사별 매출 추세 분석 요약표
# =============================================================================

_ANALYTICS_ACTION_SPECS = (
    {
        "action": "매입처별 재고부족 현황",
        "service": "get_supplier_stock_shortage_result",
        "phrases": (
            "매입처별 재고부족 현황",
            "매입처별 재고 부족 현황",
            "매입처별 재고부족",
            "매입처별 재고 부족",
            "매입처별 부족예상",
            "매입처별 부족금액",
            "발주처별 재고부족",
            "발주처별 재고 부족",
            "공급처별 재고부족",
            "공급처별 재고 부족",
        ),
    },
    {
        "action": "품목별 재고부족현황",
        "service": "get_stock_shortage_result",
        "phrases": (
            "품목별 재고부족현황",
            "품목별 재고 부족 현황",
            "재고부족현황",
            "재고 부족 현황",
            "품목별 재고부족",
            "품목별 재고 부족",
            "부족현황",
            "부족 현황",
            "재고부족",
            "재고 부족",
        ),
    },
    {
        "action": "제약사별 매출 추세 분석 요약표",
        "service": "get_manufacturer_sales_trend_summary_result",
        "phrases": (
            "제약사별 매출 추세 분석 요약표",
            "제약사별 매출추세 분석 요약표",
            "제약사별 매출 추세 요약표",
            "제약사별 매출추세 요약표",
            "제약사별 매출 추세요약표",
            "제약사별 매출추세요약표",
            "제약사 매출 추세 요약표",
            "제조사별 매출 추세 분석 요약표",
            "제조사별 매출 추세 요약표",
            "제조사별 매출추세요약표",
        ),
    },
    {
        "action": "제약사별 매출 추세 분석",
        "service": "get_manufacturer_sales_trend_result",
        "phrases": (
            "제약사별 매출 추세 분석",
            "제약사별 매출추세 분석",
            "제약사별 매출 추세",
            "제약사별 매출추세",
            "제약사 매출 추세 분석",
            "제약사 매출추세 분석",
            "제조사별 매출 추세 분석",
            "제조사별 매출추세 분석",
            "제조사별 매출 추세",
            "제조사별 매출추세",
        ),
    },
    {
        "action": "영업사원별 매출 예상",
        "service": "get_salesperson_sales_forecast_result",
        "phrases": (
            "영업사원별 매출 예상",
            "영업사원별 매출예상",
            "담당자별 매출 예상",
            "담당자별 매출예상",
            "사원별 다음달 매출 예상",
            "사원별 매출 예상",
        ),
    },
    {
        "action": "지역별 매출 예상",
        "service": "get_region_sales_forecast_result",
        "phrases": (
            "지역별 매출 예상",
            "지역별 매출예상",
            "시도별 매출 예상",
            "시도별 매출예상",
            "시군구별 매출 예상",
            "시구군별 매출 예상",
        ),
    },
    {
        "action": "매출처별 매출 예상",
        "service": "get_customer_sales_forecast_result",
        "phrases": (
            "매출처별 매출 예상",
            "매출처별 매출예상",
            "거래처별 매출 예상",
            "거래처별 매출예상",
            "고객별 매출 예상",
            "고객별 매출예상",
        ),
    },
    {
        "action": "품목별 매출 예상",
        "service": "get_sales_forecast_result",
        "phrases": (
            "품목별 매출 예상",
            "품목별 매출예상",
            "매출 예상",
            "매출예상",
            "예상매출",
        ),
    },
    {
        "action": "품목별 매출 추세 요약표",
        "service": "get_sales_trend_summary_result",
        "phrases": (
            "품목별 매출 추세 요약표",
            "품목별 매출추세 요약표",
            "품목별 매출 추세요약표",
            "품목별 매출추세요약표",
            "품목별 추세 요약표",
            "품목별 추세요약표",
            "품목별추세요약표",
            "매출 추세 요약표",
            "매출추세 요약표",
            "매출 추세요약표",
            "매출추세요약표",
            "추세 요약표",
            "추세요약표",
            "매출 추세 요약",
            "매출추세 요약",
        ),
    },    
    {
        "action": "품목별 매출 추세 분석",
        "service": "get_sales_trend_result",
        "phrases": (
            "품목별 매출 추세 분석",
            "품목별 매출추세 분석",
            "품목별 매출 추세",
            "품목별 매출추세",
            "매출 추세 분석",
            "매출추세 분석",
            "매출 추세",
            "매출추세",
            "품목별 매출",
        ),
    },
)


_ANALYTICS_TAIL_PATTERNS = (
    r"\s*품목별\s*매출\s*추세\s*요약표.*$",
    r"\s*품목별\s*매출추세\s*요약표.*$",
    r"\s*품목별\s*매출\s*추세\s*분석.*$",
    r"\s*품목별\s*매출추세\s*분석.*$",
    r"\s*품목별\s*매출\s*추세.*$",
    r"\s*품목별\s*매출추세.*$",
    r"\s*품목별\s*매출\s*예상.*$",
    r"\s*품목별\s*매출예상.*$",
    r"\s*제약사별\s*매출\s*추세\s*분석\s*요약표.*$",
    r"\s*제약사별\s*매출추세\s*분석\s*요약표.*$",
    r"\s*제약사별\s*매출\s*추세\s*요약표.*$",
    r"\s*제약사별\s*매출추세\s*요약표.*$",
    r"\s*제약사별\s*매출\s*추세.*$",
    r"\s*제약사별\s*매출추세.*$",
    r"\s*제조사별\s*매출\s*추세\s*분석\s*요약표.*$",
    r"\s*제조사별\s*매출추세\s*분석\s*요약표.*$",
    r"\s*제조사별\s*매출\s*추세\s*요약표.*$",
    r"\s*제조사별\s*매출추세\s*요약표.*$",
    r"\s*제조사별\s*매출\s*추세.*$",
    r"\s*제조사별\s*매출추세.*$",
    r"\s*품목별\s*재고\s*부족\s*현황.*$",
    r"\s*품목별\s*재고부족현황.*$",
    r"\s*품목별\s*재고\s*부족.*$",
    r"\s*품목별\s*재고부족.*$",
    r"\s*품목별\s*매출.*$",
    r"\s*품목별\s*매출\s*추세\s*요약표.*$",
    r"\s*품목별\s*매출\s*추세요약표.*$",
    r"\s*품목별\s*매출추세\s*요약표.*$",
    r"\s*품목별\s*매출추세요약표.*$",
    r"\s*품목별\s*추세\s*요약표.*$",
    r"\s*품목별\s*추세요약표.*$",    
    r"\s*매출\s*추세\s*요약표.*$",
    r"\s*매출추세\s*요약표.*$",
    r"\s*매출\s*추세\s*분석.*$",
    r"\s*매출추세\s*분석.*$",
    r"\s*매출\s*추세.*$",
    r"\s*매출추세.*$",
    r"\s*매출\s*예상.*$",
    r"\s*매출예상.*$",
    r"\s*예상매출.*$",
    r"\s*재고\s*부족\s*현황.*$",
    r"\s*재고부족현황.*$",
    r"\s*재고\s*부족.*$",
    r"\s*재고부족.*$",
    r"\s*부족\s*현황.*$",
    r"\s*부족현황.*$",
    
)


def _resolve_analytics_action(txt: str) -> str | None:
    """
    분석/KPI 문장에서 실행할 action을 결정한다.

    순서가 중요하다.
    - 재고부족현황
    - 매출 예상
    - 매출 추세 요약표
    - 매출 추세 분석

    보강:
    - 사용자가 '추세요약표', '매출추세요약표'처럼 붙여 쓰는 경우가 많으므로
      원문 비교와 공백 제거 비교를 같이 수행한다.
    """
    t = (txt or "").strip()
    if not t:
        return None

    compact_t = re.sub(r"\s+", "", t)

    for spec in _ANALYTICS_ACTION_SPECS:
        for phrase in spec["phrases"]:
            p = str(phrase or "").strip()
            if not p:
                continue

            compact_p = re.sub(r"\s+", "", p)

            if p in t:
                return str(spec["action"])

            if compact_p and compact_p in compact_t:
                return str(spec["action"])

    return None

def _looks_like_analytics_nlq(txt: str) -> bool:
    """
    분석/KPI NLQ 여부.
    별도 signal word를 중복 관리하지 않고 action 판정으로 일원화한다.
    """
    return _resolve_analytics_action(txt) is not None


def resolve_new_sims_nlq_candidate(txt: str) -> Dict[str, str] | None:
    """Resolve a new SIMS action without executing a service or DB query.

    The chat entrypoint uses this only to distinguish a fresh SIMS/NLQ request
    from a current-table follow-up.  Execution remains owned by
    :func:`try_handle_nlq`.
    """
    normalized = keyboard_fix(str(txt or "").strip())
    if not normalized:
        return None

    analytics_action = _resolve_analytics_action(normalized)
    if analytics_action:
        return {"route": "analytics", "action": str(analytics_action)}

    try:
        from app.services.io_nlq import resolve_io_nlq

        io_input = _append_lookup_verb_for_io(_normalize_io_action_spacing(normalized))
        parsed = resolve_io_nlq(io_input)
    except Exception:
        return None

    action = str((parsed or {}).get("action") or "").strip()
    if not action:
        return None
    return {"route": "io", "action": action}


def _last_day_yyyymm(yyyymm: str) -> str:
    s = re.sub(r"[^0-9]", "", str(yyyymm or ""))
    if len(s) < 6:
        return ""

    try:
        y = int(s[:4])
        m = int(s[4:6])
    except Exception:
        return ""

    if not (1 <= m <= 12):
        return ""

    if m == 12:
        nxt = dt.date(y + 1, 1, 1)
    else:
        nxt = dt.date(y, m + 1, 1)

    return (nxt - dt.timedelta(days=1)).strftime("%Y%m%d")


def _extract_analytics_year_range(txt: str) -> Dict[str, str]:
    """
    분석/KPI 기간 해석.

    지원:
    - 2025년
    - 2025
    - 2025~2026
    - 2025년부터 2026년까지

    YYYYMM / YYYYMMDD와 충돌하지 않도록 4자리 연도만 본다.
    """
    t = str(txt or "")
    out: Dict[str, str] = {}

    m = re.search(
        r"(?<!\d)(20\d{2})\s*년?\s*(?:~|-|부터|에서)\s*(20\d{2})\s*년?\s*(?:까지)?(?!\d)",
        t,
    )
    if m:
        y1, y2 = m.group(1), m.group(2)
        out["month_from"] = f"{y1}01"
        out["month_to"] = f"{y2}12"
        out["date_from"] = f"{y1}0101"
        out["date_to"] = f"{y2}1231"
        out["_year_month_range_applied"] = "Y"
        return out

    singles = re.findall(r"(?<!\d)(20\d{2})\s*년?(?!\d)", t)
    singles = list(dict.fromkeys(singles))

    if len(singles) == 1:
        y = singles[0]
        out["month_from"] = f"{y}01"
        out["month_to"] = f"{y}12"
        out["date_from"] = f"{y}0101"
        out["date_to"] = f"{y}1231"
        out["_year_month_range_applied"] = "Y"

    return out


def _apply_analytics_period_defaults(params: Dict[str, Any], txt: str) -> Dict[str, Any]:
    """
    분석/KPI 기간 기본값 보정.

    우선순위:
    1. io_nlq.extract_params()가 잡은 date_from/date_to
    2. io_nlq.extract_params()가 잡은 month_from/month_to
    3. 2025년 같은 연도 표현
    4. 없으면 올해 1월 1일 ~ 오늘
    """
    out = dict(params or {})

    date_from = str(out.get("date_from") or "").strip()
    date_to = str(out.get("date_to") or "").strip()
    month_from = str(out.get("month_from") or "").strip()
    month_to = str(out.get("month_to") or "").strip()

    # YYYYMMDD가 있으면 month도 같이 보정
    if date_from and not month_from:
        out["month_from"] = date_from[:6]
    if date_to and not month_to:
        out["month_to"] = date_to[:6]

    # YYYYMM만 있으면 date도 같이 보정
    month_from = str(out.get("month_from") or "").strip()
    month_to = str(out.get("month_to") or "").strip()

    if month_from and not out.get("date_from"):
        out["date_from"] = f"{month_from[:6]}01"
    if month_to and not out.get("date_to"):
        out["date_to"] = _last_day_yyyymm(month_to[:6])

    has_period = any(
        str(out.get(k) or "").strip()
        for k in ("date_from", "date_to", "month_from", "month_to")
    )

    # 2025년 단독 또는 2025~2026 연도 범위
    if not has_period:
        year_params = _extract_analytics_year_range(txt)
        if year_params:
            out.update(year_params)

    has_period = any(
        str(out.get(k) or "").strip()
        for k in ("date_from", "date_to", "month_from", "month_to")
    )

    # 아무 기간도 없으면 올해 1월 1일 ~ 오늘
    if not has_period:
        today = dt.date.today()
        first_day = dt.date(today.year, 1, 1)
        out["date_from"] = first_day.strftime("%Y%m%d")
        out["date_to"] = today.strftime("%Y%m%d")
        out["month_from"] = first_day.strftime("%Y%m")
        out["month_to"] = today.strftime("%Y%m")
        out["_default_date_applied"] = "Y"

    return out


def _analytics_nlq_max_rows(default: int = 30000) -> int:
    try:
        raw = (
            os.getenv("SIMS_PANEL_DISPLAY_MAX_ROWS")
            or os.getenv("SIMS_CHAT_DISPLAY_MAX_ROWS")
            or str(default)
        )
        v = int(str(raw or default).strip())
    except Exception:
        v = int(default)
    if v < 1:
        v = int(default)
    return v

def _extract_analytics_top(txt: str, default: int = 0) -> int:
    t = str(txt or "")
    env_max = _analytics_nlq_max_rows()
    if not default or default < 1:
        default = env_max

    patterns = [
        r"(?:top|TOP)\s*[:=]?\s*(\d{1,5})",
        r"상위\s*(\d{1,5})",
        r"(\d{1,5})\s*건",
    ]

    for pat in patterns:
        m = re.search(pat, t)
        if not m:
            continue

        try:
            v = int(m.group(1))
            return max(1, min(v, env_max))
        except Exception:
            pass

    return int(default)


def _apply_analytics_source_params(params: Dict[str, Any], txt: str, action: str) -> Dict[str, Any]:
    """
    분석자료원 / 재고기준 해석.

    source_mode:
    - 출고상세 / 상세자료 / 상세 기준 -> detail
    - 월집계 + 실재고 -> monthly_real
    - 월집계 + 장부재고 -> monthly_book
    - 월집계 단독 -> monthly_book
    - 그 외 -> auto

    stock_mode:
    - 품목별 재고부족현황에서만 사용
    - 실재고 기준 -> real
    - 장부재고 기준 -> book
    - 기본 -> book
    """
    out = dict(params or {})
    t = str(txt or "").strip()

    has_monthly = ("월집계" in t) or ("월 집계" in t)
    has_real = "실재고" in t
    has_book = "장부재고" in t

    if "출고상세" in t or "상세자료" in t or "상세 기준" in t:
        out["source_mode"] = "detail"
    elif has_monthly and has_real:
        out["source_mode"] = "monthly_real"
    elif has_monthly and has_book:
        out["source_mode"] = "monthly_book"
    elif has_monthly:
        out["source_mode"] = "monthly_book"
    else:
        out.setdefault("source_mode", "auto")

    if action in {"품목별 재고부족현황", "매입처별 재고부족 현황"}:
        if has_real:
            out["stock_mode"] = "real"
        elif has_book:
            out["stock_mode"] = "book"
        else:
            out.setdefault("stock_mode", "book")

    return out


def _strip_analytics_tail(value: Any) -> str:
    """
    분석/KPI NLQ에서 조건값 뒤에 붙은 액션 문구 제거.

    예:
    - "한미 품목별 매출 예상" -> "한미"
    - "일반 품목별 재고부족현황" -> "일반"
    - "우루사 품목별 매출 추세" -> "우루사"
    """
    s = str(value or "").strip()
    if not s:
        return ""

    for sep in [",", "/", " / ", " , "]:
        if sep in s:
            s = s.split(sep, 1)[0].strip()

    for pat in _ANALYTICS_TAIL_PATTERNS:
        s = re.sub(pat, "", s).strip()

    s = re.sub(r"\s*(조회|검색|보여줘|알려줘|찾아줘|해줘)$", "", s).strip()
    s = re.sub(r"\s+", " ", s).strip()

    return s


_ANALYTICS_GROUP_ACTIONS = {
    "제약사별 매출 추세 분석",
    "제약사별 매출 추세 분석 요약표",
    "품목별 매출 추세 분석",
    "품목별 매출 추세 요약표",
    "품목별 매출 예상",
    "매입처별 재고부족 현황",
    "매출처별 매출 예상",
    "영업사원별 매출 예상",
    "지역별 매출 예상",
}


def _explicit_manufacturer_before_group_action(text: str, action: str) -> str:
    """Read only a token immediately before a grouped analytics action."""
    if action not in _ANALYTICS_GROUP_ACTIONS:
        return ""
    action_pattern = re.escape(action).replace(r"\ ", r"\s*")
    match = re.search(action_pattern, str(text or ""))
    if not match:
        return ""
    prefix = str(text or "")[:match.start()].strip(" ,")
    candidate = re.search(r"([가-힣A-Za-z0-9_-]+)\s*$", prefix)
    if not candidate:
        return ""
    value = candidate.group(1).strip()
    if value in {"조회", "분석", "현황", "요약", "요약표", "예상", "추세", "매출", "실재고", "장부재고"}:
        return ""
    if re.fullmatch(r"\d{1,8}(?:년|월|일)?", value):
        return ""
    return value if any(token in value for token in ("제약", "약품")) else ""


def _cleanup_analytics_named_params(
    params: Dict[str, Any],
    *,
    text: str = "",
    action: str = "",
) -> Dict[str, Any]:
    """
    분석/KPI NLQ 조건값 후처리.
    io_nlq.extract_params()가 액션 문구까지 같이 잡는 경우를 보정한다.
    """
    out = dict(params or {})

    named_keys = [
        "physic_nm",
        "product_ven_nm",
        "maker_nm",
        "product_group_nm",
        "product_di_nm",
        "product_class_nm",
        "ven_nm",
        "buy_nm",
        "real_ven_nm",
        "sales_man_nm",
        "stock_nm",
        "stock_apply_nm",
        "sido_nm",
        "gugun_nm",
        "dong_nm",
        "road_nm",
        "road_addr_kw",
    ]

    for key in named_keys:
        if key in out:
            out[key] = _strip_analytics_tail(out.get(key))

    # Group action words contain labels such as "제약사별".  They are not
    # manufacturer conditions, but a valid explicit manufacturer must survive.
    if action in _ANALYTICS_GROUP_ACTIONS:
        maker = str(out.get("maker_nm") or "").strip()
        product_maker = str(out.get("product_ven_nm") or "").strip()
        explicit_maker = _explicit_manufacturer_before_group_action(text, action)
        if action.startswith("제약사별"):
            maker = maker if maker and maker != "별" else explicit_maker
            product_maker = product_maker if product_maker and product_maker != "별" else maker
        elif not maker and not product_maker and explicit_maker:
            maker = explicit_maker
            product_maker = explicit_maker
        if maker == "별":
            maker = ""
        if product_maker == "별":
            product_maker = ""
        out["maker_nm"] = maker
        out["product_ven_nm"] = product_maker

    # 제조사명은 서비스 쪽에서 product_ven_nm을 주로 사용하므로 동기화
    if out.get("maker_nm") and not out.get("product_ven_nm"):
        out["product_ven_nm"] = out["maker_nm"]

    if out.get("product_ven_nm") and not out.get("maker_nm"):
        out["maker_nm"] = out["product_ven_nm"]

    return out

def _extract_analytics_trend_judge(txt: str) -> str:
    t = str(txt or "").replace(" ", "")

    # 긴 표현 먼저
    if "반품주의" in t:
        return "반품주의"
    if "자료부족" in t or "데이터부족" in t:
        return "자료부족"
    if "신규/증가" in t or "신규증가" in t:
        return "신규/증가"
    if "감소" in t:
        return "감소"
    if "증가" in t:
        return "증가"
    if "안정" in t:
        return "안정"

    return ""


def _extract_analytics_shortage_grade(txt: str) -> str:
    t = str(txt or "").replace(" ", "")

    grade_patterns = [
        ("재고없음/수요없음", ("재고없음/수요없음", "재고없고수요없음", "수요없고재고없음")),
        ("1개월내 부족", ("1개월내부족", "1개월부족")),
        ("2개월내 부족주의", ("2개월내부족주의", "2개월부족주의")),
        ("2개월내 부족", ("2개월내부족", "2개월부족")),
        ("3개월내 부족주의", ("3개월내부족주의", "3개월부족주의")),
        ("3개월내 부족", ("3개월내부족", "3개월부족")),
        ("수요관찰", ("수요관찰",)),
        ("재고없음", ("재고없음", "재고없어", "재고없는")),
        ("정상", ("정상",)),
    ]

    for grade, patterns in grade_patterns:
        if any(p in t for p in patterns):
            return grade
    return ""


def _build_analytics_params(txt: str, action: str) -> Dict[str, Any]:
    try:
        from app.services.io_nlq import extract_params
        params = extract_params(txt)
    except Exception:
        params = {}

    params = _cleanup_analytics_named_params(params, text=txt, action=action)
    params = _apply_analytics_condition_aliases(params, txt)
    params = _apply_analytics_period_defaults(params, txt)

    params = _apply_analytics_source_params(params, txt, action)

    trend_judge = _extract_analytics_trend_judge(txt)
    if trend_judge and action != "품목별 재고부족현황":
        params["trend_judge"] = trend_judge

    if action in {"품목별 재고부족현황", "매입처별 재고부족 현황"}:
        shortage_grade = _extract_analytics_shortage_grade(txt)
        if shortage_grade:
            params["shortage_grade"] = shortage_grade

    params["top"] = _extract_analytics_top(txt)

    return params


def _apply_analytics_condition_aliases(params: Dict[str, Any], txt: str) -> Dict[str, Any]:
    """Fill analytics-only name/code aliases without coercing names to codes."""
    out = dict(params or {})
    text = str(txt or "")

    if not _analytics_nlq_code_values(out, "stock_cd_list"):
        stock_match = re.search(r"(?<!\d)(\d{1,6})\s*창고", text)
        if stock_match:
            code = stock_match.group(1)
            out["stock_cds"] = [code]
            out["stock_cd"] = code
    if not _analytics_nlq_name_values(out, "stock_cd_list") and not _analytics_nlq_code_values(out, "stock_cd_list"):
        stock_name_match = re.search(r"([가-힣A-Za-z][가-힣A-Za-z0-9_-]*)\s*창고", text)
        if stock_name_match and stock_name_match.group(1) != "전체":
            out["stock_nm"] = stock_name_match.group(1)

    for code_key, name_key, label in (
        ("product_di_list", "product_di_nm", "제품구분"),
        ("product_class_list", "product_class_nm", "제품분류"),
    ):
        if _analytics_nlq_code_values(out, code_key) or _analytics_nlq_name_values(out, code_key):
            continue
        match = re.search(rf"([가-힣A-Za-z][가-힣A-Za-z0-9_-]*)\s*{label}", text)
        if match and match.group(1) != "전체":
            out[name_key] = match.group(1)
    return out


_ANALYTICS_NLQ_DEFAULT_KEYS = {
    "품목별 매출 추세 분석": {"stock_cd_list", "product_di_list", "product_class_list", "io_gu_list"},
    "품목별 매출 추세 요약표": {"stock_cd_list", "product_di_list", "product_class_list", "io_gu_list"},
    "품목별 매출 예상": {"stock_cd_list", "product_di_list", "product_class_list", "io_gu_list"},
    "제약사별 매출 추세 분석": {"stock_cd_list", "product_di_list", "product_class_list", "io_gu_list"},
    "제약사별 매출 추세 분석 요약표": {"stock_cd_list", "product_di_list", "product_class_list", "io_gu_list"},
    "매출처별 매출 예상": {"io_gu_list"},
    "영업사원별 매출 예상": {"io_gu_list"},
    "지역별 매출 예상": {"io_gu_list"},
    "품목별 재고부족현황": {"stock_mode", "stock_cd_list", "product_di_list", "product_class_list", "io_gu_list"},
    "매입처별 재고부족 현황": {"stock_mode", "stock_cd_list", "product_di_list", "product_class_list", "io_gu_list"},
}


def _profile_tcodes(values: Any) -> list[str]:
    normalized: list[str] = []
    for value in (values or []):
        raw = normalize_business_code(value)
        if not raw:
            continue
        pair = normalize_business_code_pair(raw) if ":" in raw else ""
        normalized.append(pair.rsplit(":", 1)[-1] if pair else raw)
    return list(dict.fromkeys(normalized))


def _profile_code_pairs(values: Any) -> list[str]:
    """Normalize profile code pairs without collapsing their Gcode portion."""
    return list(dict.fromkeys(
        normalize_business_code_pair(value)
        for value in (values or [])
        if normalize_business_code_pair(value)
    ))


def _analytics_nlq_values(params: Dict[str, Any], *keys: str) -> list[str]:
    """Read one alias family without converting names into codes."""
    for key in keys:
        value = params.get(key)
        if isinstance(value, (list, tuple, set)):
            values = [str(item).strip() for item in value if str(item).strip()]
        else:
            text = str(value or "").strip()
            values = [text] if text else []
        if values:
            return list(dict.fromkeys(values))
    return []


def _analytics_nlq_code_values(params: Dict[str, Any], key: str) -> list[str]:
    aliases = {
        "stock_cd_list": ("stock_cd_list", "stock_cds", "stock_cd"),
        "product_di_list": ("dashboard_product_di_list", "product_di_list", "product_di"),
        "product_class_list": ("dashboard_product_class_list", "product_class_list", "product_class"),
        "io_gu_list": ("io_gu_list", "dashboard_io_gu_list", "sales_io_gu_list", "io_gu_pairs", "io_gu"),
    }
    values = _analytics_nlq_values(params, *aliases.get(key, ()))
    normalized: list[str] = []
    for value in values:
        code = normalize_business_code_pair(value) if ":" in value else normalize_business_code(value)
        if code:
            normalized.append(code)
    return list(dict.fromkeys(normalized))


def _analytics_nlq_name_values(params: Dict[str, Any], key: str) -> list[str]:
    aliases = {
        "stock_cd_list": ("stock_nm", "stock_nm_list"),
        "product_di_list": ("product_di_nm", "product_di_nm_list"),
        "product_class_list": ("product_class_nm", "product_class_nm_list"),
    }
    return _analytics_nlq_values(params, *aliases.get(key, ()))


def _analytics_nlq_option_codes(field: str) -> list[str]:
    """Reuse the KPI option universe for Default full-selection handling."""
    gcode = {
        "stock_cd_list": "0018",
        "product_di_list": "0004",
        "product_class_list": "0031",
        "io_gu_list": "0012",
    }.get(str(field or ""))
    if not gcode:
        return []
    try:
        from app.sims.views.analytics_views import get_analytics_code_option_codes

        return list(get_analytics_code_option_codes(gcode) or [])
    except Exception:
        # Do not infer a company-specific universe from a Default profile.
        return []


def _context_business_code_pairs(text: str, labels: tuple[str, ...]) -> list[tuple[str, str]]:
    """Read only pairs directly adjacent to a condition label."""
    label_pattern = "|".join(re.escape(label) for label in labels)
    code_pattern = r"([A-Za-z0-9_-]+)\s*:\s*([A-Za-z0-9_-]+)"
    found: list[tuple[str, str]] = []
    for pattern in (
        rf"(?<![A-Za-z0-9_-]){code_pattern}\s*(?:{label_pattern})",
        rf"(?:{label_pattern})\s*{code_pattern}(?![A-Za-z0-9_-])",
    ):
        for match in re.finditer(pattern, str(text or "")):
            gcode = normalize_business_code(match.group(1))
            tcode = normalize_business_code(match.group(2))
            if gcode and tcode:
                found.append((gcode, tcode))
    return list(dict.fromkeys(found))


def _resolve_explicit_product_di_contract(text: str) -> list[str]:
    return [f"{gcode}:{tcode}" for gcode, tcode in _context_business_code_pairs(text, ("제품구분",)) if gcode == "0004"]


def _resolve_explicit_product_class_contract(params: Dict[str, Any], text: str) -> dict[str, Any]:
    """Route only product-class-context pairs to 0031 Tax or 0028 legacy."""
    raw_text = str(text or "")
    tax_pairs: list[str] = []
    legacy_codes: list[str] = []
    for gcode, tcode in _context_business_code_pairs(text, ("제품분류", "특수관리제품")):
        if gcode == "0031":
            tax_pairs.append(f"{gcode}:{tcode}")
        elif gcode == "0028":
            legacy_codes.append(tcode)
    if tax_pairs or legacy_codes:
        return {
            "tax_pairs": list(dict.fromkeys(tax_pairs)),
            "legacy_codes": list(dict.fromkeys(legacy_codes)),
        }

    code_match = re.search(r"(?<!\d)(\d{1,6})\s*제품분류", raw_text)
    if code_match:
        tcode = normalize_business_code(code_match.group(1))
        if tcode:
            return {"tax_pairs": [f"0031:{tcode}"], "legacy_codes": []}

    name_values = _analytics_nlq_name_values(params, "product_class_list")
    if not name_values:
        return {}
    try:
        from app.sims.views.analytics_views import _load_code_options

        options = _load_code_options("0031")
    except Exception:
        return {}
    name = name_values[0]
    for option in options:
        if str(option.get("name") or "").strip() != name:
            continue
        tcode = normalize_business_code(option.get("code"))
        if tcode:
            return {"tax_pairs": [f"0031:{tcode}"], "legacy_codes": []}
    return {}


def _clear_legacy_product_class_aliases(params: Dict[str, Any]) -> None:
    params["product_class_list"] = []
    params["product_class"] = ""
    params["product_class_nm"] = ""
    params["product_class_nm_list"] = []


def _analytics_nlq_condition_sources(params: Dict[str, Any], sources: Dict[str, Any]) -> str:
    """Build a user-facing summary from applied conditions only."""
    labels = {
        "stock_mode": "재고기준",
        "stock_cd_list": "재고위치",
        "product_di_list": "제품구분",
        "product_class_list": "제품분류",
        "io_gu_list": "입출고구분",
    }
    values = {
        "stock_mode": {"real": "실재고", "book": "장부재고"}.get(str(params.get("stock_mode") or ""), str(params.get("stock_mode") or "")),
        "stock_cd_list": _analytics_nlq_code_values(params, "stock_cd_list")
        or _analytics_nlq_name_values(params, "stock_cd_list"),
        "product_di_list": _analytics_nlq_code_values(params, "product_di_list")
        or _analytics_nlq_name_values(params, "product_di_list"),
        "product_class_list": _analytics_nlq_code_values(params, "product_class_list")
        or _analytics_nlq_name_values(params, "product_class_list"),
        "io_gu_list": _analytics_nlq_code_values(params, "io_gu_list"),
    }
    parts = []
    for key in labels:
        source = dict(sources or {}).get(key)
        if not source:
            continue
        value = values.get(key)
        if source == "explicit_clear":
            text = "전체"
        elif isinstance(value, list):
            display_values = [str(item) for item in value if str(item).strip()]
            if not display_values:
                # An explicit parser condition with no display-safe alias must
                # not be presented as an explicit "전체" condition.
                if source == "explicit":
                    continue
                text = "전체"
            else:
                text = ", ".join(display_values)
        else:
            text = str(value or "")
            if not text:
                if source == "explicit":
                    continue
                text = "전체"
        source_label = "질문에서 지정" if source == "explicit" else (
            "전체 조건" if source == "explicit_clear" else "회사 Default"
        )
        parts.append(f"{labels[key]}: {text} ({source_label})")
    return " / ".join(parts)


def _merge_analytics_nlq_condition_summary(
    existing_summary: str,
    source_summary: str,
) -> str:
    """Append only missing condition values; add provenance for duplicates."""
    existing = str(existing_summary or "").strip()
    if not existing:
        return str(source_summary or "").strip()
    missing: list[str] = []
    provenance: list[str] = []
    for part in (item.strip() for item in str(source_summary or "").split(" / ")):
        if not part:
            continue
        label, separator, rest = part.partition(":")
        if not separator:
            missing.append(part)
            continue
        source_label = rest.rsplit("(", 1)[-1].rstrip(")").strip() if "(" in rest else ""
        if label.strip() and label.strip() in existing:
            provenance.append(f"{label.strip()} {source_label}".strip())
        else:
            missing.append(part)
    suffixes = list(missing)
    if provenance:
        suffixes.append(f"조건 출처: {' / '.join(provenance)}")
    return " / ".join([existing, *suffixes])


def _analytics_nlq_param_log_summary(params: Dict[str, Any], sources: Dict[str, Any]) -> dict[str, Any]:
    """Return only non-identifying parameter shape/counts for NLQ logs."""
    source_values = list(dict(sources or {}).values())
    return {
        "stock_mode_present": bool(str(params.get("stock_mode") or "").strip()),
        "stock_cd_count": len(_analytics_nlq_code_values(params, "stock_cd_list")),
        "product_di_count": len(_analytics_nlq_code_values(params, "product_di_list")),
        "product_class_count": len(_analytics_nlq_code_values(params, "product_class_list")),
        "io_gu_count": len(_analytics_nlq_code_values(params, "io_gu_list")),
        "default_condition_count": source_values.count("default"),
        "explicit_condition_count": source_values.count("explicit"),
        "explicit_clear_count": source_values.count("explicit_clear"),
    }


def _apply_company_default_to_analytics_nlq(
    params: Dict[str, Any],
    *,
    text: str,
    action: str,
    session_state: Dict[str, Any],
    logger,
) -> Dict[str, Any]:
    """Apply only code-safe company Defaults to an analytics NLQ request."""
    supported = set(_ANALYTICS_NLQ_DEFAULT_KEYS.get(action, set()))
    if not supported:
        return params

    try:
        from app.services.ssai_analysis_profile_service import (
            build_company_default_adapter,
            load_dashboard_profile,
            normalize_analytics_multi_code_filter,
        )
        from app.ui.ssai_login import get_selected_company

        company_id = str((get_selected_company() or {}).get("company_id") or "").strip()
    except Exception:
        company_id = ""
    if not company_id:
        return params

    out = dict(params or {})
    text_compact = re.sub(r"\s+", "", str(text or ""))
    explicit_keys: set[str] = set()
    clear_keys: set[str] = set()
    if action in {"품목별 재고부족현황", "매입처별 재고부족 현황"} and any(
        token in text_compact for token in ("실재고", "장부재고")
    ):
        explicit_keys.add("stock_mode")
    stock_code_values = _analytics_nlq_code_values(out, "stock_cd_list")
    stock_name_values = _analytics_nlq_name_values(out, "stock_cd_list")
    if stock_code_values or stock_name_values:
        explicit_keys.add("stock_cd_list")
    if stock_code_values:
        out["stock_cd_list"] = list(stock_code_values)
        out["stock_cds"] = list(stock_code_values)
        out["stock_cd"] = stock_code_values[0] if len(stock_code_values) == 1 else ""
    if any(token in text_compact for token in ("전체창고", "전창고", "모든창고", "창고전체")):
        clear_keys.add("stock_cd_list")
    if any(token in text_compact for token in ("전체제품구분", "전제품구분", "모든제품구분")):
        clear_keys.add("product_di_list")
    if any(token in text_compact for token in ("전체제품분류", "전제품분류", "모든제품분류")):
        clear_keys.add("product_class_list")
    # KPI NLQ never accepts a per-question IO override.  Preserve the shared
    # parser behavior for non-KPI IO actions, but remove those aliases only
    # at this KPI action boundary before the company Default is injected.
    for key in ("io_gu", "io_gu_list", "io_gu_pairs", "dashboard_io_gu_list", "sales_io_gu_list", "io_gu_prefix"):
        out.pop(key, None)
    product_di_code_values = _analytics_nlq_code_values(out, "product_di_list")
    product_di_name_values = _analytics_nlq_name_values(out, "product_di_list")
    explicit_product_di_pairs = _resolve_explicit_product_di_contract(text)
    if explicit_product_di_pairs:
        explicit_keys.add("product_di_list")
        out["product_di_list"] = [pair.rsplit(":", 1)[-1] for pair in explicit_product_di_pairs]
        out["dashboard_product_di_list"] = list(explicit_product_di_pairs)
        out["product_di"] = ""
        out["product_di_nm"] = ""
        out["product_di_nm_list"] = []
    elif product_di_code_values or product_di_name_values:
        explicit_keys.add("product_di_list")
    if product_di_code_values and not explicit_product_di_pairs:
        out["product_di_list"] = list(product_di_code_values)
        out["dashboard_product_di_list"] = list(product_di_code_values)
    explicit_product_class = _resolve_explicit_product_class_contract(out, text)
    product_class_code_values = _analytics_nlq_code_values(out, "product_class_list")
    product_class_name_values = _analytics_nlq_name_values(out, "product_class_list")
    if explicit_product_class:
        explicit_keys.add("product_class_list")
        tax_pairs = list(explicit_product_class.get("tax_pairs") or [])
        legacy_codes = list(explicit_product_class.get("legacy_codes") or [])
        if tax_pairs and not legacy_codes:
            _clear_legacy_product_class_aliases(out)
        else:
            out["product_class_list"] = legacy_codes
            out["product_class"] = ""
            out["product_class_nm"] = ""
            out["product_class_nm_list"] = []
        out["dashboard_product_class_list"] = tax_pairs
    elif product_class_code_values or product_class_name_values:
        explicit_keys.add("product_class_list")
    if product_class_code_values and not explicit_product_class:
        out["product_class_list"] = list(product_class_code_values)
        out["dashboard_product_class_list"] = list(product_class_code_values)
    if str(out.get("product_group_nm") or "").strip():
        # The target has a legacy single-value product-group field.  Never
        # squeeze a multi-code Default into it.
        supported.discard("product_group_list")

    try:
        from app.services.ssai_analysis_profile_service import invalidate_analysis_profile_cache

        last_company_key = "__analysis_profile_last_company_id"
        previous_company = str(session_state.get(last_company_key) or "")
        if previous_company and previous_company != company_id:
            invalidate_analysis_profile_cache(session_state, company_id=previous_company)
            invalidate_analysis_profile_cache(session_state, company_id=company_id)
        session_state[last_company_key] = company_id
    except Exception:
        pass

    cache = session_state.setdefault("__analysis_profile_company_cache", {})
    profile = cache.get(company_id) if isinstance(cache, dict) else None
    cache_used = isinstance(profile, dict)
    if not cache_used:
        profile = load_dashboard_profile(company_id=int(company_id))
        if isinstance(cache, dict):
            cache[company_id] = dict(profile or {})

    adapter = build_company_default_adapter(
        profile,
        supported_keys=supported,
        explicit=out,
        explicit_keys=explicit_keys,
        clear_keys=clear_keys,
    )
    for key, value in dict(adapter.get("effective") or {}).items():
        source = (adapter.get("sources") or {}).get(key)
        if source == "explicit":
            continue
        if key == "stock_cd_list":
            values = [] if source == "explicit_clear" else _profile_tcodes(value)
            normalized = normalize_analytics_multi_code_filter(
                values, _analytics_nlq_option_codes(key), expected_gcode="0018"
            )
            values = list(normalized["effective_codes"])
            out["stock_cd_list"] = values
            out["stock_cds"] = values
            out["stock_cd"] = values[0] if len(values) == 1 else ""
            if normalized["is_full_selection"]:
                adapter_sources = adapter.get("sources")
                if isinstance(adapter_sources, dict):
                    adapter_sources.pop(key, None)
            logger.info(
                "[analysis_profile.filter_normalize] action=%s field=%s selected_count=%s available_count=%s "
                "full_selection=%s effective_count=%s source=%s",
                action, key, normalized["selected_count"], normalized["available_count"],
                normalized["is_full_selection"], len(values), source,
            )
        elif key == "io_gu_list":
            values = [] if source == "explicit_clear" else _profile_tcodes(value)
            # Company IO is a required, persisted scope for KPI NLQ.  Unlike
            # optional multi-select filters, choosing every configured code
            # still means a concrete company policy and must not collapse to
            # an empty legacy/all scope.
            out["io_gu_list"] = list(dict.fromkeys(values))
            logger.info(
                "[analysis_profile.filter_normalize] action=%s field=%s selected_count=%s available_count=%s "
                "full_selection=%s effective_count=%s source=%s",
                action, key, len(values), len(_analytics_nlq_option_codes(key)),
                False, len(out["io_gu_list"]), source,
            )
        elif key in {"product_di_list", "product_class_list"}:
            pairs = [] if source == "explicit_clear" else _profile_code_pairs(value)
            normalized = normalize_analytics_multi_code_filter(
                _profile_tcodes(pairs),
                _analytics_nlq_option_codes(key),
                pairs,
                "0004" if key == "product_di_list" else "0031",
            )
            if key == "product_class_list" and normalized.get("pair_gcode_matches"):
                # Dashboard Company Default product classification is 0031
                # (Rd04_Physic_Tax).  The legacy product_class aliases retain
                # their 0028/Rd04_Physic_Gu meaning and must stay empty.
                out[key] = []
                out["dashboard_product_class_list"] = list(normalized["effective_pairs"])
                out["product_class"] = ""
                out["product_class_nm"] = ""
                out["product_class_nm_list"] = []
            else:
                out[key] = list(normalized["effective_codes"])
                out[f"dashboard_{key}"] = list(normalized["effective_pairs"])
            if normalized["is_full_selection"]:
                legacy_key = key.replace("_list", "")
                out[legacy_key] = ""
                out[f"{legacy_key}_nm"] = ""
                out[f"{legacy_key}_nm_list"] = []
                adapter_sources = adapter.get("sources")
                if isinstance(adapter_sources, dict):
                    adapter_sources.pop(key, None)
            logger.info(
                "[analysis_profile.filter_normalize] action=%s field=%s selected_count=%s available_count=%s "
                "full_selection=%s effective_count=%s source=%s",
                action, key, normalized["selected_count"], normalized["available_count"],
                normalized["is_full_selection"], len(out[key]), source,
            )
        else:
            out[key] = value

    # Clear every legacy alias too.  Some services still inspect the older
    # name/code fields, so clearing only the adapter key is not sufficient.
    if "stock_cd_list" in clear_keys:
        for key in ("stock_cd_list", "stock_cds", "stock_cd", "stock_nm", "stock_nm_list"):
            out[key] = [] if key in {"stock_cd_list", "stock_cds", "stock_nm_list"} else ""
    if "product_di_list" in clear_keys:
        for key in ("product_di_list", "dashboard_product_di_list", "product_di", "product_di_nm", "product_di_nm_list"):
            out[key] = [] if key.endswith("_list") else ""
    if "product_class_list" in clear_keys:
        for key in ("product_class_list", "dashboard_product_class_list", "product_class", "product_class_nm", "product_class_nm_list"):
            out[key] = [] if key.endswith("_list") else ""

    out["__analysis_default_sources"] = dict(adapter.get("sources") or {})
    io_source = str((out["__analysis_default_sources"] or {}).get("io_gu_list") or "")
    if io_source == "default":
        io_source = "company_default"
    elif io_source == "explicit_clear":
        io_source = "explicit_all"
    out["io_gu_source"] = io_source or "company_default"
    out["_require_company_io"] = True
    if not _analytics_nlq_code_values(out, "io_gu_list"):
        out["__company_io_missing"] = True

    logger.info(
        "[analysis_profile.adapter] company_id_present=True profile_found=%s target_context=nlq "
        "supported_key_count=%s applied_default_count=%s explicit_override_count=%s explicit_clear_count=%s "
        "unsupported_key_count=%s cache_used=%s reason=nlq_request",
        bool(adapter.get("profile_found")), len(supported), adapter.get("applied_default_count", 0),
        adapter.get("explicit_override_count", 0), adapter.get("explicit_clear_count", 0),
        len(adapter.get("unsupported_default_keys") or []), cache_used,
    )
    logger.info(
        "[NLQ profile 적용] target_action=%s explicit_condition_count=%s default_condition_count=%s "
        "explicit_override_count=%s explicit_clear_count=%s",
        action, len(explicit_keys), adapter.get("applied_default_count", 0),
        adapter.get("explicit_override_count", 0), adapter.get("explicit_clear_count", 0),
    )
    return out


def _get_analytics_handler(action: str):
    try:
        from app.services.analytics_sales_trend_service import (
            get_sales_trend_result,
            get_sales_trend_summary_result,
            get_sales_forecast_result,
            get_stock_shortage_result,
        )
        from app.services.analytics_supplier_stock_shortage_service import (
            get_supplier_stock_shortage_result,
        )
        from app.services.analytics_manufacturer_sales_trend_service import (
            get_manufacturer_sales_trend_result,
            get_manufacturer_sales_trend_summary_result,
        )
        from app.services.analytics_customer_sales_forecast_service import (
            get_customer_sales_forecast_result,
            get_region_sales_forecast_result,
            get_salesperson_sales_forecast_result,
        )
    except Exception:
        raise

    fn_map = {
        "품목별 매출 추세 분석": get_sales_trend_result,
        "품목별 매출 추세 요약표": get_sales_trend_summary_result,
        "품목별 매출 예상": get_sales_forecast_result,
        "매출처별 매출 예상": get_customer_sales_forecast_result,
        "영업사원별 매출 예상": get_salesperson_sales_forecast_result,
        "지역별 매출 예상": get_region_sales_forecast_result,
        "제약사별 매출 추세 분석": get_manufacturer_sales_trend_result,
        "제약사별 매출 추세 분석 요약표": get_manufacturer_sales_trend_summary_result,
        "품목별 재고부족현황": get_stock_shortage_result,
        "매입처별 재고부족 현황": get_supplier_stock_shortage_result,
    }

    return fn_map.get(str(action or "").strip())

#=============================================================================
# 분석/KPI NLQ 라우팅
# - _looks_like_analytics_nlq()로 판정된 문장은 _try_handle_analytics_nlq()로 처리한다.
# - action 해석 -> params 빌드 -> 서비스 함수 호출 -> 결과를 채팅으로 push
# - 서비스 함수는 dict 또는 str을 반환한다. dict인 경우 meta 필드를 포함할 수 있다.
# - meta 필드로 nlq_query, _force_push, _nlq_nonce 등을 전달해서 채팅에서 NLQ 결과임을 인식하게 한다.
def _try_handle_analytics_nlq(
    txt: str,
    *,
    room: Dict[str, Any],
    session_state: Dict[str, Any],
    make_ts: Callable[[], str],
    next_seq: Callable[[], int],
    logger,
) -> bool:
    """
    분석/KPI NLQ 라우팅.

    예:
    - 품목별 매출 추세 2025년 조회
    - 품목별 매출 추세 요약표 2025년 조회
    - 품목별 매출 예상 2025년 월집계 장부재고 조회
    - 품목별 재고부족현황 2025년 실재고 기준 조회
    """
    t = (txt or "").strip()
    if not t:
        return False

    action = _resolve_analytics_action(t)
    if not action:
        return False

    try:
        from app.ui.chat_middleware import push_sims_result_to_chat
    except Exception:
        logger.exception("[nlq.router] failed to import chat_middleware")
        return False

    try:
        fn = _get_analytics_handler(action)
    except Exception:
        logger.exception("[nlq.router] failed to import analytics handler")
        return False

    if not callable(fn):
        return False

    params = _build_analytics_params(t, action)
    params = _apply_company_default_to_analytics_nlq(
        params,
        text=t,
        action=action,
        session_state=session_state,
        logger=logger,
    )
    adapter_sources = dict(params.pop("__analysis_default_sources", {}) or {})
    service_params = dict(params)
    log_summary = _analytics_nlq_param_log_summary(service_params, adapter_sources)

    if service_params.pop("__company_io_missing", False):
        payload = {
            "final": True,
            "type": "text",
            "title": action,
            "action": action,
            "params": service_params,
            "data": "회사 공통 분석용 입출고구분이 설정되지 않았습니다. Dashboard 공통조건에서 설정 후 저장해 주세요.",
            "message": "회사 공통 분석용 입출고구분이 설정되지 않았습니다. Dashboard 공통조건에서 설정 후 저장해 주세요.",
            "meta": {
                "nlq": True,
                "analysis_nlq": True,
                "analytics": True,
                "action": action,
                "io_gu_source": "company_default",
                "company_io_required": True,
                "row_count": 0,
                "row_count_total": 0,
            },
        }
        push_sims_result_to_chat(payload, action)
        return True

    try:
        payload = fn(service_params)
    except Exception as e:
        logger.exception(
            "[nlq.router] analytics service failed action=%r stock_mode_present=%s stock_cd_count=%s "
            "product_di_count=%s product_class_count=%s default_condition_count=%s "
            "explicit_condition_count=%s explicit_clear_count=%s",
            action,
            log_summary["stock_mode_present"], log_summary["stock_cd_count"],
            log_summary["product_di_count"], log_summary["product_class_count"],
            log_summary["default_condition_count"], log_summary["explicit_condition_count"],
            log_summary["explicit_clear_count"],
        )

        payload = {
            "final": True,
            "type": "text",
            "title": f"{action} 오류",
            "action": action,
            "params": service_params,
            "data": f"{action} 처리 중 오류가 발생했습니다: {e}",
            "message": f"{action} 처리 중 오류가 발생했습니다: {e}",
            "meta": {
                "nlq": True,
                "analysis_nlq": True,
                "analytics": True,
                "action": action,
                "row_count": 0,
                "row_count_total": 0,
                "condition": "",
                "query_summary": "",
            },
        }

        try:
            push_sims_result_to_chat(payload, action)
        except Exception:
            logger.exception("[nlq.router] push analytics error payload failed action=%r", action)

        return True

    if not isinstance(payload, dict):
        payload = {
            "final": True,
            "type": "text",
            "title": action,
            "action": action,
            "params": params,
            "data": str(payload),
            "message": str(payload),
        }

    payload.setdefault("title", action)
    payload.setdefault("action", action)
    payload_params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    effective_params = {**service_params, **payload_params}
    payload["params"] = effective_params

    meta = dict(payload.get("meta") or {})
    source_summary = _analytics_nlq_condition_sources(effective_params, adapter_sources)
    if source_summary:
        existing_summary = str(meta.get("query_summary") or meta.get("condition") or "").strip()
        merged_summary = _merge_analytics_nlq_condition_summary(existing_summary, source_summary)
        meta["query_summary"] = merged_summary
        meta["condition"] = merged_summary
    meta.update({
        "nlq": True,
        "nlq_query": txt,
        "_force_push": True,
        "_nlq_nonce": str(uuid.uuid4()),
        "analysis_nlq": True,
    })
    payload["meta"] = meta

    try:
        push_sims_result_to_chat(payload, action)
    except Exception:
        logger.exception("[nlq.router] push analytics result failed action=%r", action)
        return False

    session_state["__sims_last_nlq_action"] = action
    session_state["__sims_last_nlq_params"] = effective_params
    session_state["__scroll_to_msg"] = (
        session_state.get("__sims_last_msg_id") or session_state.get("__scroll_to_msg")
    )

    log_summary = _analytics_nlq_param_log_summary(effective_params, adapter_sources)
    logger.info(
        "[nlq.router] analytics handled action=%r stock_mode_present=%s stock_cd_count=%s "
        "product_di_count=%s product_class_count=%s default_condition_count=%s "
        "explicit_condition_count=%s explicit_clear_count=%s",
        action,
        log_summary["stock_mode_present"], log_summary["stock_cd_count"],
        log_summary["product_di_count"], log_summary["product_class_count"],
        log_summary["default_condition_count"], log_summary["explicit_condition_count"],
        log_summary["explicit_clear_count"],
    )
    return True

def _io_has_period_params(params: Dict[str, Any]) -> bool:
    """IO/NLQ params에 기간/월 조건이 이미 있는지 확인."""
    p = params or {}
    return any(
        str(p.get(k) or "").strip()
        for k in ("date_from", "date_to", "month_from", "month_to")
    )


def _apply_io_recent_1month_default(
    params: Dict[str, Any],
    action: str,
) -> Dict[str, Any]:
    """
    IO/NLQ 기본기간 보정.

    원칙:
    - 기간 조건이 없으면 최근 1개월을 자동 적용한다.
    - 화면 표시는 기존처럼 200건만 표시한다.
    - LLM/다운로드는 이 조회조건 전체 건을 기준으로 한다.
    """
    out = dict(params or {})

    if _io_has_period_params(out):
        return out

    default_actions = {
        "입고명세 조회",
        "출고명세 조회",
        "거래명세서 공통 조회",
        "세금계산서 공통 조회",
        "입고↔거래명세서 검증",
        "입고↔세금계산서 검증",
        "출고↔거래명세서 검증",
        "출고↔세금계산서 검증",
        "실재고월집계 조회",
        "장부재고월집계 조회",
    }

    if str(action or "").strip() not in default_actions:
        return out

    today = dt.date.today()
    date_from = today - dt.timedelta(days=30)
    date_to = today

    out["date_from"] = date_from.strftime("%Y%m%d")
    out["date_to"] = date_to.strftime("%Y%m%d")
    out["month_from"] = date_from.strftime("%Y%m")
    out["month_to"] = date_to.strftime("%Y%m")

    # 조회조건 표시용 플래그
    out["_default_recent_1month_applied"] = "Y"

    return out


def _fmt_io_yyyymmdd(value: Any) -> str:
    s = re.sub(r"[^0-9]", "", str(value or ""))
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return str(value or "").strip()


def _fmt_io_yyyymm(value: Any) -> str:
    s = re.sub(r"[^0-9]", "", str(value or ""))
    if len(s) == 6:
        return f"{s[:4]}-{s[4:6]}"
    return str(value or "").strip()


def _first_clean_value(*values: Any) -> str:
    for v in values:
        s = str(v or "").strip()
        if s:
            return s
    return ""


def _append_code_name(parts: list[str], label: str, code: Any = "", name: Any = "") -> None:
    code_s = str(code or "").strip()
    name_s = str(name or "").strip()

    if code_s and name_s:
        parts.append(f"{label} {code_s} {name_s}")
    elif code_s:
        parts.append(f"{label} {code_s}")
    elif name_s:
        parts.append(f"{label} {name_s}")


def _build_io_query_summary(action: str, params: Dict[str, Any]) -> str:
    """
    IO / 명세서 / 재고 NLQ 공통 조회조건 표시용 summary.

    목적:
    - 입고명세/출고명세/공통조회/월집계/검증 payload에도
      meta.query_summary, meta.summary_md를 공통으로 제공한다.
    - 제품수불/제품재고처럼 이미 query_summary가 있는 payload는 건드리지 않는다.
    """
    p = dict(params or {})
    parts: list[str] = []

    # 기간
    date_from = str(p.get("date_from") or "").strip()
    date_to = str(p.get("date_to") or "").strip()
    if date_from or date_to:
        if date_from and date_to and date_from != date_to:
            parts.append(f"기간 {_fmt_io_yyyymmdd(date_from)} ~ {_fmt_io_yyyymmdd(date_to)}")
        else:
            parts.append(f"기간 {_fmt_io_yyyymmdd(date_from or date_to)}")

    # 기준월
    month_from = str(p.get("month_from") or "").strip()
    month_to = str(p.get("month_to") or "").strip()
    if month_from or month_to:
        if month_from and month_to and month_from != month_to:
            parts.append(f"기준월 {_fmt_io_yyyymm(month_from)} ~ {_fmt_io_yyyymm(month_to)}")
        else:
            parts.append(f"기준월 {_fmt_io_yyyymm(month_from or month_to)}")

    # 순번류
    if p.get("in_seq"):
        parts.append(f"입고순번 {p.get('in_seq')}")
    if p.get("out_seq"):
        parts.append(f"출고순번 {p.get('out_seq')}")
    if p.get("trans_seq"):
        parts.append(f"거래명세서순번 {p.get('trans_seq')}")
    if p.get("tax_seq"):
        parts.append(f"세금계산서순번 {p.get('tax_seq')}")

    # 구분류
    trans_di = str(p.get("trans_di") or "").strip()
    if trans_di:
        trans_label = {"1": "매입분", "3": "매출분"}.get(trans_di, trans_di)
        parts.append(f"거래명세서구분 {trans_label}")

    tax_di = str(p.get("tax_di") or "").strip()
    if tax_di:
        tax_label = {
            "1": "매입",
            "2": "회계매입",
            "3": "매출",
            "4": "회계매출",
        }.get(tax_di, tax_di)
        parts.append(f"세금계산서구분 {tax_label}")

    io_gu_prefix = str(p.get("io_gu_prefix") or "").strip()
    if io_gu_prefix:
        parts.append(f"입출고구분앞자리 {io_gu_prefix}")

    stock_side = str(p.get("stock_side") or "").strip()
    if stock_side:
        stock_side_label = {"in": "입고/매입", "out": "출고/매출"}.get(stock_side, stock_side)
        parts.append(f"집계방향 {stock_side_label}")

    # 제품/거래처
    _append_code_name(parts, "제품", p.get("physic_cd"), p.get("physic_nm"))
    _append_code_name(parts, "거래처", p.get("ven_cd"), p.get("ven_nm"))

    # 거래처/제품 관련 참조 조건
    maker_cd = _first_clean_value(p.get("maker_cd"), p.get("product_ven_cd"))
    maker_nm = _first_clean_value(p.get("maker_nm"), p.get("product_ven_nm"))
    _append_code_name(parts, "제조사", maker_cd, maker_nm)

    _append_code_name(parts, "발주처", p.get("order_cd"), p.get("order_nm"))
    _append_code_name(parts, "매입처", p.get("buy_cd"), p.get("buy_nm"))
    _append_code_name(parts, "실납처", p.get("real_ven_cd"), p.get("real_ven_nm"))
    _append_code_name(parts, "영업사원", p.get("sales_man"), p.get("sales_man_nm"))

    # 제품/거래처 분류
    if p.get("product_group_nm"):
        parts.append(f"제품그룹명 {p.get('product_group_nm')}")
    if p.get("product_di_nm"):
        parts.append(f"제품구분명 {p.get('product_di_nm')}")
    if p.get("product_class_nm"):
        parts.append(f"제품분류명 {p.get('product_class_nm')}")
    if p.get("ven_group_nm"):
        parts.append(f"거래처그룹명 {p.get('ven_group_nm')}")
    if p.get("ven_kind_nm"):
        parts.append(f"거래처종류명 {p.get('ven_kind_nm')}")

    # 등록/수정자
    if p.get("add_nm"):
        parts.append(f"등록자 {p.get('add_nm')}")
    if p.get("mod_nm"):
        parts.append(f"수정자 {p.get('mod_nm')}")

    # 재고위치
    stock_cds = p.get("stock_cds")
    if isinstance(stock_cds, (list, tuple)) and stock_cds:
        parts.append("재고위치 " + ",".join(str(x) for x in stock_cds if str(x).strip()))
    elif p.get("stock_cd"):
        parts.append(f"재고위치 {p.get('stock_cd')}")
    if p.get("stock_nm"):
        parts.append(f"재고위치명 {p.get('stock_nm')}")

    _append_code_name(parts, "재고적용처", p.get("stock_apply_cd"), p.get("stock_apply_nm"))

    # 제품수불/제품재고 기준류
    if p.get("stock_mode"):
        parts.append(f"기준 {p.get('stock_mode')}")
    if p.get("flow_scope"):
        parts.append(f"범위 {p.get('flow_scope')}")
    if p.get("date_basis"):
        parts.append(f"기준일자 {p.get('date_basis')}")
    if p.get("group_basis"):
        parts.append(f"집계기준 {p.get('group_basis')}")
    if p.get("price_mode"):
        parts.append(f"단가기준 {p.get('price_mode')}")

    # 검증/불일치
    if str(p.get("only_mismatch") or "").upper() == "Y":
        parts.append("불일치만")
    if str(p.get("only_mismatch_trans") or "").upper() == "Y":
        parts.append("거래명세서 불일치만")
    if str(p.get("only_mismatch_tax") or "").upper() == "Y":
        parts.append("세금계산서 불일치만")

    # 기본기간/기본월 자동 적용 표시
    if str(p.get("_default_recent_1month_applied") or "").upper() == "Y":
        parts.append("최근 1개월 자동적용")
    elif str(p.get("_default_date_applied") or "").upper() == "Y":
        parts.append("기본기간 자동적용")

    if str(p.get("_default_month_applied") or "").upper() == "Y":
        parts.append("기본월 자동적용")
    if str(p.get("_year_month_range_applied") or "").upper() == "Y":
        parts.append("연도기간 자동적용")


    return " / ".join(str(x).strip() for x in parts if str(x).strip())

def _io_payload_to_df(payload: Dict[str, Any]) -> pd.DataFrame | None:
    """IO payload에서 LLM 요약용 DataFrame을 최대한 안전하게 꺼낸다."""
    if not isinstance(payload, dict):
        return None

    for key in ("df_display", "df", "data"):
        obj = payload.get(key)
        if isinstance(obj, pd.DataFrame):
            return obj

    records = payload.get("records")
    columns = payload.get("columns")
    if isinstance(records, list) and records:
        try:
            if isinstance(columns, list) and columns:
                return pd.DataFrame.from_records(records, columns=columns)
            return pd.DataFrame.from_records(records)
        except Exception:
            return None

    meta = payload.get("meta") or {}
    records = meta.get("df")
    columns = meta.get("columns")
    if isinstance(records, list) and records:
        try:
            if isinstance(columns, list) and columns:
                return pd.DataFrame.from_records(records, columns=columns)
            return pd.DataFrame.from_records(records)
        except Exception:
            return None

    return None


def _io_to_num(value: Any) -> float:
    """문자/콤마/단위가 섞인 값을 숫자로 변환한다."""
    if value is None:
        return 0.0

    s = str(value).strip()
    if not s or s in {"None", "nan", "NaN", "<NA>", "NaT"}:
        return 0.0

    s = (
        s.replace(",", "")
        .replace("원", "")
        .replace("개", "")
        .replace("건", "")
        .replace("%", "")
        .strip()
    )

    try:
        return float(s)
    except Exception:
        return 0.0


def _io_sum_by_columns(df: pd.DataFrame | None, candidates: list[str]) -> float:
    """후보 컬럼명 중 존재하는 첫 컬럼의 합계를 구한다."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return 0.0

    for col in candidates:
        if col in df.columns:
            try:
                return float(df[col].map(_io_to_num).sum())
            except Exception:
                return 0.0

    return 0.0


def _io_last_by_columns(df: pd.DataFrame | None, candidates: list[str]) -> float:
    """후보 컬럼명 중 존재하는 첫 컬럼의 마지막 값을 구한다."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return 0.0

    for col in candidates:
        if col in df.columns:
            try:
                s = df[col].map(_io_to_num)
                if len(s) > 0:
                    return float(s.iloc[-1])
            except Exception:
                return 0.0

    return 0.0


def _io_fmt_num(value: Any) -> str:
    n = _io_to_num(value)
    if abs(n - int(n)) < 1e-9:
        return f"{int(n):,}"
    return f"{n:,.2f}".rstrip("0").rstrip(".")


def _product_info_label(product_info: Any, params: Dict[str, Any]) -> str:
    """제품수불현황 LLM 요약용 제품 표시 문자열."""
    if isinstance(product_info, dict):
        code = str(
            product_info.get("제품코드")
            or product_info.get("physic_cd")
            or product_info.get("product_code")
            or product_info.get("code")
            or ""
        ).strip()

        name = str(
            product_info.get("제품명")
            or product_info.get("physic_nm")
            or product_info.get("product_name")
            or product_info.get("name")
            or ""
        ).strip()

        standard = str(product_info.get("규격") or product_info.get("standard") or "").strip()
        maker = str(product_info.get("제조사명") or product_info.get("maker_nm") or "").strip()

        parts = []
        if code or name:
            parts.append(f"{code} {name}".strip())
        if standard:
            parts.append(f"규격 {standard}")
        if maker:
            parts.append(f"제조사 {maker}")

        if parts:
            return " / ".join(parts)

    physic_cd = str(params.get("physic_cd") or "").strip()
    physic_nm = str(params.get("physic_nm") or "").strip()

    if physic_cd and physic_nm:
        return f"{physic_cd} {physic_nm}"
    if physic_cd:
        return f"제품코드 {physic_cd}"
    if physic_nm:
        return f"제품명 {physic_nm}"

    return "제품 미지정"

#=============================================================================
# 제품수불현황 LLM 요약 보강
# - 제품수불현황 NLQ에 대해 LLM이 핵심 수치를 바로 이해할 수 있도록 meta에 요약 정보를 보강한다.
# - 후보표(candidate_table)는 실제 수불표가 아니므로 별도 안내 요약만 제공한다.
# - 제품수불현황이 아닌 다른 NLQ 결과는 건드리지 않는다.
def _ensure_product_flow_llm_summary(
    payload: Dict[str, Any],
    action: str,
    params: Dict[str, Any],
    query_summary: str,
) -> Dict[str, Any]:
    """
    제품수불현황 payload에 LLM 분석용 요약 meta를 보강한다.

    목표:
    - LLM이 표 전체를 보지 않아도 이월/입고/출고/재고 핵심 수치를 이해하게 한다.
    - 후보표(candidate_table)는 실제 수불표가 아니므로 별도 안내 요약만 제공한다.
    """
    if action != "제품수불현황 조회" or not isinstance(payload, dict):
        return payload

    meta = dict(payload.get("meta") or {})
    df = _io_payload_to_df(payload)

    row_count = int(meta.get("row_count_total") or meta.get("row_count") or (len(df) if isinstance(df, pd.DataFrame) else 0) or 0)

    # 후보표는 실제 수불표가 아니라 제품 선택 안내표다.
    if bool(meta.get("candidate_table")):
        candidate_count = row_count
        summary_md = (
            f"조회조건: {query_summary}\n\n"
            "### 제품 후보 선택 안내\n"
            f"- 후보 제품수: **{candidate_count:,}건**\n"
            "- 원하는 제품 번호를 입력하면 해당 제품으로 제품수불현황을 조회합니다.\n"
            "- 취소하려면 `취소`를 입력합니다."
        )

        meta["summary_md"] = summary_md
        meta["llm_summary_kind"] = "product_flow_candidate"
        meta["analysis_type"] = "product_flow_candidate"
        meta["product_flow_candidate_count"] = candidate_count
        meta.setdefault("message", "제품 후보를 선택하세요.")

        payload["meta"] = meta
        payload.setdefault("message", "제품 후보를 선택하세요.")
        return payload

    # 실제 제품수불표 요약
    product_info = meta.get("product_info") or {}
    product_label = _product_info_label(product_info, params)

    carry_qty = meta.get("carry_qty")
    in_qty = meta.get("in_qty")
    out_qty = meta.get("out_qty")
    stock_qty = meta.get("stock_qty")

    if carry_qty is None:
        carry_qty = _io_last_by_columns(df, ["이월재고", "이월재고수량", "전월재고", "기초재고"])
    if in_qty is None:
        in_qty = _io_sum_by_columns(df, ["입고수량", "매입수량", "입고", "매입"])
    if out_qty is None:
        out_qty = _io_sum_by_columns(df, ["출고수량", "매출수량", "출고", "매출"])
    if stock_qty is None:
        stock_qty = _io_last_by_columns(df, ["재고수량", "현재고", "현재재고", "잔고수량", "잔량"])

    stock_mode = str(meta.get("stock_mode") or params.get("stock_mode") or "").strip()
    date_basis = str(meta.get("date_basis") or params.get("date_basis") or "").strip()
    flow_scope = str(meta.get("flow_scope") or params.get("flow_scope") or "").strip()

    basis_parts = []
    if stock_mode:
        basis_parts.append(f"수불기준 {stock_mode}")
    if date_basis:
        basis_parts.append(f"일자기준 {date_basis}")
    if flow_scope:
        basis_parts.append(f"조회범위 {flow_scope}")

    basis_text = " / ".join(basis_parts) if basis_parts else "기준 미지정"

    flow_summary = {
        "product": product_label,
        "row_count": row_count,
        "query_summary": query_summary,
        "stock_mode": stock_mode,
        "date_basis": date_basis,
        "flow_scope": flow_scope,
        "carry_qty": _io_to_num(carry_qty),
        "in_qty": _io_to_num(in_qty),
        "out_qty": _io_to_num(out_qty),
        "stock_qty": _io_to_num(stock_qty),
    }

    summary_md = (
        f"조회조건: {query_summary}\n\n"
        "### 제품수불현황 요약\n"
        f"- 제품: **{product_label}**\n"
        f"- 기준: {basis_text}\n"
        f"- 조회건수: **{row_count:,}건**\n"
        f"- 이월재고: **{_io_fmt_num(carry_qty)}**\n"
        f"- 입고수량: **{_io_fmt_num(in_qty)}**\n"
        f"- 출고수량: **{_io_fmt_num(out_qty)}**\n"
        f"- 재고수량: **{_io_fmt_num(stock_qty)}**"
    )

    meta["summary_md"] = summary_md
    meta["llm_summary_kind"] = "product_flow_summary"
    meta["analysis_type"] = "product_flow"
    meta["flow_summary"] = flow_summary

    # LLM context builder가 meta 중심으로 보게 하기 위한 직접 키도 같이 제공
    meta["carry_qty"] = flow_summary["carry_qty"]
    meta["in_qty"] = flow_summary["in_qty"]
    meta["out_qty"] = flow_summary["out_qty"]
    meta["stock_qty"] = flow_summary["stock_qty"]
    meta["product_label"] = product_label

    payload["meta"] = meta
    payload.setdefault("message", "제품수불현황 조회 결과입니다.")
    return payload


def _io_text_value(value: Any) -> str:
    """LLM 요약용 안전 문자열."""
    try:
        if value is None:
            return ""
        s = str(value).strip()
        if s in {"", "None", "nan", "NaN", "<NA>", "NaT"}:
            return ""
        return s
    except Exception:
        return ""


def _io_first_by_columns(df: pd.DataFrame | None, candidates: list[str]) -> str:
    """후보 컬럼명 중 존재하는 첫 컬럼의 첫 번째 유효 문자열을 구한다."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return ""

    for col in candidates:
        if col not in df.columns:
            continue

        try:
            for v in df[col].tolist():
                s = _io_text_value(v)
                if s:
                    return s
        except Exception:
            return ""

    return ""


def _io_distinct_count_by_columns(df: pd.DataFrame | None, candidates: list[str]) -> int:
    """후보 컬럼명 중 존재하는 첫 컬럼의 distinct count."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return 0

    for col in candidates:
        if col not in df.columns:
            continue

        try:
            s = df[col].map(_io_text_value)
            s = s[s != ""]
            return int(s.nunique())
        except Exception:
            return 0

    return 0

# 제품재고현황 LLM 요약 보강
# - 제품재고현황 NLQ에 대해 LLM이 핵심 수치를 바로 이해할 수 있도록 meta에 요약 정보를 보강한다.    
def _io_top_records_by_stock_qty(df: pd.DataFrame | None, limit: int = 10) -> list[dict]:
    """
    제품재고현황 LLM용 대표 목록.
    재고수량이 적은 순서로 제품/재고위치/수량/금액만 작게 제공한다.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []

    qty_col = next(
        (
            c for c in [
                "재고수량",
                "현재재고수량",
                "장부재고수량",
                "실재고수량",
                "수량",
            ]
            if c in df.columns
        ),
        None,
    )

    use_cols = [
        c for c in [
            "제품코드",
            "제품명",
            "규격",
            "제조사명",
            "발주처명",
            "재고위치",
            "재고위치명",
            "재고수량",
            "현재재고수량",
            "장부재고수량",
            "실재고수량",
            "재고금액",
            "보험금액",
        ]
        if c in df.columns
    ]

    if not use_cols:
        use_cols = list(df.columns[:12])

    work = df.copy()

    if qty_col:
        try:
            work["_llm_stock_qty"] = work[qty_col].map(_io_to_num)
            work = work.sort_values("_llm_stock_qty", ascending=True)
        except Exception:
            pass

    out: list[dict] = []
    try:
        for _, row in work.head(limit).iterrows():
            rec: dict[str, Any] = {}
            for col in use_cols:
                v = row.get(col)
                if col in {
                    "재고수량",
                    "현재재고수량",
                    "장부재고수량",
                    "실재고수량",
                    "수량",
                    "재고금액",
                    "보험금액",
                }:
                    rec[col] = _io_to_num(v)
                else:
                    rec[col] = _io_text_value(v)
            out.append(rec)
    except Exception:
        return []

    return out

# 제품재고현황 LLM 요약 보강
# - 제품재고현황 NLQ에 대해 LLM이 핵심 수치를 바로 이해할 수 있도록 meta에 요약 정보를 보강한다.
def _ensure_product_inventory_llm_summary(
    payload: Dict[str, Any],
    action: str,
    params: Dict[str, Any],
    query_summary: str,
) -> Dict[str, Any]:
    """
    제품재고현황/제품재고장 payload에 LLM 분석용 요약 meta를 보강한다.

    목표:
    - LLM이 전체 표를 읽지 않아도 품목수/재고위치수/재고수량/재고금액/보험금액을 우선 사용하게 한다.
    - 0건 text 결과는 기존 0건 메시지를 그대로 유지한다.
    """
    if action != "제품재고현황 조회" or not isinstance(payload, dict):
        return payload

    meta = dict(payload.get("meta") or {})
    df = _io_payload_to_df(payload)

    row_count = int(
        meta.get("row_count_total")
        or meta.get("row_count")
        or (len(df) if isinstance(df, pd.DataFrame) else 0)
        or 0
    )

    # 0건 text 결과는 기존 메시지를 유지하되, LLM meta만 최소 표시한다.
    if row_count <= 0 or not isinstance(df, pd.DataFrame) or df.empty:
        meta["llm_summary_kind"] = "product_inventory_empty"
        meta["analysis_type"] = "product_inventory"
        meta.setdefault("inventory_summary", {
            "row_count": 0,
            "query_summary": query_summary,
        })
        payload["meta"] = meta
        return payload

    product_count = _io_distinct_count_by_columns(
        df,
        ["제품코드", "physic_cd", "상품코드"],
    )
    stock_location_count = _io_distinct_count_by_columns(
        df,
        ["재고위치", "재고위치코드", "재고위치명", "stock_cd", "stock_nm"],
    )

    stock_qty = _io_sum_by_columns(
        df,
        [
            "재고수량",
            "현재재고수량",
            "장부재고수량",
            "실재고수량",
            "수량",
        ],
    )
    stock_amt = _io_sum_by_columns(
        df,
        [
            "재고금액",
            "현재재고금액",
            "장부재고금액",
            "실재고금액",
        ],
    )
    insurance_amt = _io_sum_by_columns(
        df,
        [
            "보험금액",
            "보험가금액",
            "현재보험금액",
        ],
    )

    # 제품재고현황 대상 표시
    # - 제품코드/제품명이 명시된 경우: 특정 제품 조회
    # - 제조사/발주처/매입처/제품그룹 등 조건 조회: 첫 번째 제품을 대표처럼 말하면 안 됨
    has_single_product_condition = bool(
        _io_text_value(params.get("physic_cd"))
        or _io_text_value(params.get("physic_nm"))
    )

    is_single_product_result = int(product_count or 0) == 1
    target_scope = "single_product" if (has_single_product_condition or is_single_product_result) else "multi_product_condition"

    product_label = _product_info_label(meta.get("product_info") or {}, params)

    if target_scope != "single_product":
        maker_nm = _first_clean_value(params.get("maker_nm"), params.get("product_ven_nm"))
        order_nm = _first_clean_value(params.get("order_nm"))
        buy_nm = _first_clean_value(params.get("buy_nm"))
        group_nm = _first_clean_value(params.get("product_group_nm"))
        di_nm = _first_clean_value(params.get("product_di_nm"))
        class_nm = _first_clean_value(params.get("product_class_nm"))

        if maker_nm:
            product_label = f"제조사 {maker_nm} 조건 전체 제품"
        elif order_nm:
            product_label = f"발주처 {order_nm} 조건 전체 제품"
        elif buy_nm:
            product_label = f"매입처 {buy_nm} 조건 전체 제품"
        elif group_nm:
            product_label = f"제품그룹명 {group_nm} 조건 전체 제품"
        elif di_nm:
            product_label = f"제품구분명 {di_nm} 조건 전체 제품"
        elif class_nm:
            product_label = f"제품분류명 {class_nm} 조건 전체 제품"
        else:
            product_label = "조회조건 전체 제품"

    elif product_label == "제품 미지정":
        code = _io_first_by_columns(df, ["제품코드", "physic_cd", "상품코드"])
        name = _io_first_by_columns(df, ["제품명", "physic_nm", "상품명"])
        maker = _io_first_by_columns(df, ["제조사명", "maker_nm", "제약사명"])
        parts = []
        if code or name:
            parts.append(f"{code} {name}".strip())
        if maker:
            parts.append(f"제조사 {maker}")
        product_label = " / ".join(parts) if parts else "전체 또는 조건 조회"

    stock_mode = str(meta.get("stock_mode") or params.get("stock_mode") or "").strip()
    group_basis = str(meta.get("group_basis") or params.get("group_basis") or "").strip()
    price_mode = str(meta.get("price_mode") or params.get("price_mode") or "").strip()

    basis_parts = []
    if stock_mode:
        basis_parts.append(f"재고기준 {stock_mode}")
    if group_basis:
        basis_parts.append(f"집계기준 {group_basis}")
    if price_mode:
        basis_parts.append(f"단가기준 {price_mode}")

    basis_text = " / ".join(basis_parts) if basis_parts else "기준 미지정"

    zero_stock_records = _io_records_by_stock_qty_condition(df, mode="zero", limit=50)
    zero_or_negative_stock_records = _io_records_by_stock_qty_condition(df, mode="zero_or_negative", limit=50)
    negative_stock_records = _io_records_by_stock_qty_condition(df, mode="negative", limit=50)

    inventory_summary = {
        "product": product_label,
        "target_scope": target_scope,
        "target_rule": "single_product이면 특정 제품 분석, multi_product_condition이면 조건 전체 제품 분석",
        "row_count": row_count,
        "product_count": int(product_count),
        "stock_location_count": int(stock_location_count),
        "query_summary": query_summary,
        "stock_mode": stock_mode,
        "group_basis": group_basis,
        "price_mode": price_mode,
        "stock_qty": float(stock_qty),
        "stock_amt": float(stock_amt),
        "insurance_amt": float(insurance_amt),
        "low_stock_records": _io_top_records_by_stock_qty(df, limit=10),
        "zero_stock_count": len(zero_stock_records),
        "zero_stock_records": zero_stock_records,
        "zero_or_negative_stock_count": len(zero_or_negative_stock_records),
        "zero_or_negative_stock_records": zero_or_negative_stock_records,
        "negative_stock_count": len(negative_stock_records),
        "negative_stock_records": negative_stock_records,


    }

    summary_md = (
        f"조회조건: {query_summary}\n\n"
        "### 제품재고현황 요약\n"
        f"- 대상: **{product_label}**\n"
        f"- 기준: {basis_text}\n"
        f"- 조회건수: **{row_count:,}건**\n"
        f"- 제품수: **{product_count:,}개**\n"
        f"- 재고위치수: **{stock_location_count:,}개**\n"
        f"- 재고수량: **{_io_fmt_num(stock_qty)}**\n"
        f"- 재고금액: **{_io_fmt_num(stock_amt)}**\n"
        f"- 보험금액: **{_io_fmt_num(insurance_amt)}**\n\n"
        "### 답변 규칙\n"
        "- 제품수가 2개 이상이면 특정 제품 1개의 재고현황처럼 말하지 않는다.\n"
        "- 제조사/발주처/매입처/제품그룹 조건 조회는 조건에 해당하는 전체 제품 집계로 설명한다.\n"
        "- 첫 번째 행의 제품명이나 low_stock_records의 제품을 전체 대표 제품처럼 표현하지 않는다.\n"
        "- 재고수량이 낮은 참고 목록은 부족 확정 목록이 아니므로 단정하지 않는다.\n"
        "- 내부 key 이름은 답변에 쓰지 않는다.\n"
        "- 주요 수치는 조회건수, 제품수, 재고수량, 재고금액, 보험금액, 입고수량, 출고수량을 중심으로 설명한다."
        "- low_stock_records, sample_records, risk_products_top 같은 내부 key 이름을 답변에 쓰지 않는다.\n"
        "- '샘플 데이터'라고 표현하지 말고, 필요한 경우 '참고 목록' 또는 '전체 조회 결과 기준'이라고 표현한다.\n"
        "- 제품재고현황은 재고부족현황 분석표가 아니므로, 부족등급/재고부족 확정 목록처럼 단정하지 않는다.\n"        
    )

    meta["summary_md"] = summary_md
    meta["llm_summary_kind"] = "product_inventory_summary"
    meta["analysis_type"] = "product_inventory"
    meta["inventory_summary"] = inventory_summary

    meta["zero_stock_count"] = len(zero_stock_records)
    meta["zero_or_negative_stock_count"] = len(zero_or_negative_stock_records)
    meta["negative_stock_count"] = len(negative_stock_records)

    meta["inventory_target_scope"] = target_scope

    # LLM context builder가 meta 중심으로 보게 하기 위한 직접 키도 같이 제공
    meta["product_label"] = product_label
    meta["product_count"] = int(product_count)
    meta["stock_location_count"] = int(stock_location_count)
    meta["stock_qty"] = float(stock_qty)
    meta["stock_amt"] = float(stock_amt)
    meta["insurance_amt"] = float(insurance_amt)

    payload["meta"] = meta
    payload.setdefault("message", "제품재고현황 조회 결과입니다.")
    return payload

def _io_records_by_stock_qty_condition(
    df: pd.DataFrame,
    *,
    mode: str = "zero",
    limit: int = 50,
) -> list[dict]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []

    stock_col = _io_find_first_column(
        df,
        ["재고수량", "현재재고수량", "장부재고수량", "실재고수량", "수량"],
    )
    if not stock_col:
        return []

    product_code_col = _io_find_first_column(df, ["제품코드", "physic_cd", "상품코드"])
    product_name_col = _io_find_first_column(df, ["제품명", "physic_nm", "상품명"])
    maker_col = _io_find_first_column(df, ["제조사명", "maker_nm", "제약사명"])
    stock_amt_col = _io_find_first_column(df, ["재고금액", "현재재고금액", "장부재고금액", "실재고금액"])
    insu_amt_col = _io_find_first_column(df, ["보험금액", "보험가금액", "현재보험금액"])

    work = df.copy()
    nums = work[stock_col].map(lambda x: _io_to_num(x))

    if mode == "negative":
        sub = work[nums < 0]
    elif mode == "zero_or_negative":
        sub = work[nums <= 0]
    else:
        sub = work[nums == 0]

    if sub.empty:
        return []

    cols = [
        c for c in [
            product_code_col,
            product_name_col,
            maker_col,
            stock_col,
            stock_amt_col,
            insu_amt_col,
        ]
        if c
    ]

    out: list[dict] = []
    for _, row in sub[cols].head(limit).iterrows():
        rec = {}
        for c in cols:
            rec[str(c)] = _io_text_value(row.get(c))
        out.append(rec)

    return out

# 입고명세/출고명세 LLM 요약 보강
# - 입고명세/출고명세 NLQ에 대해 LLM이 핵심 수치를 바로 이해할 수 있도록 meta에 요약 정보를 보강한다.
# - 제품/거래처별 상위 그룹 목록도 같이 제공한다.
def _io_find_first_column(df: pd.DataFrame | None, candidates: list[str]) -> str:
    """후보 컬럼명 중 실제 존재하는 첫 컬럼명."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return ""

    cols = {str(c): c for c in df.columns}
    for cand in candidates:
        if cand in cols:
            return cols[cand]
    return ""


def _io_top_group_sum_records(
    df: pd.DataFrame | None,
    group_candidates: list[str],
    qty_candidates: list[str],
    amount_candidates: list[str],
    *,
    limit: int = 10,
) -> list[dict]:
    """
    LLM 요약용 상위 그룹 목록.
    예: 상위 거래처, 상위 제품, 상위 영업사원.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []

    group_col = _io_find_first_column(df, group_candidates)
    if not group_col:
        return []

    qty_col = _io_find_first_column(df, qty_candidates)
    amount_col = _io_find_first_column(df, amount_candidates)

    work = pd.DataFrame()
    work["name"] = df[group_col].map(_io_text_value)

    if qty_col:
        work["qty"] = df[qty_col].map(_io_to_num)
    else:
        work["qty"] = 0.0

    if amount_col:
        work["amount"] = df[amount_col].map(_io_to_num)
    else:
        work["amount"] = 0.0

    work = work[work["name"] != ""]
    if work.empty:
        return []

    try:
        grouped = (
            work.groupby("name", as_index=False)
            .agg(qty=("qty", "sum"), amount=("amount", "sum"))
            .sort_values(["amount", "qty"], ascending=[False, False])
            .head(limit)
        )
    except Exception:
        return []

    out: list[dict] = []
    for _, row in grouped.iterrows():
        out.append(
            {
                "name": _io_text_value(row.get("name")),
                "qty": float(_io_to_num(row.get("qty"))),
                "amount": float(_io_to_num(row.get("amount"))),
            }
        )

    return out


def _io_find_first_column(df: pd.DataFrame | None, candidates: list[str]) -> str:
    """후보 컬럼명 중 실제 존재하는 첫 컬럼명."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return ""

    cols = {str(c): c for c in df.columns}
    for cand in candidates:
        if cand in cols:
            return cols[cand]
    return ""

# 입고명세/출고명세 LLM 요약 보강
# - 입고명세/출고명세 NLQ에 대해 LLM이 핵심 수치를 바로 이해할 수 있도록 meta에 요약 정보를 보강한다.
# - 제품/거래처별 상위 그룹 목록도 같이 제공한다.
def _io_group_stats_records(
    df: pd.DataFrame | None,
    group_candidates: list[str],
    qty_candidates: list[str],
    supply_candidates: list[str],
    tax_candidates: list[str],
    *,
    limit: int = 10,
) -> list[dict]:
    """
    LLM 분석용 그룹 집계.
    반환:
    - name
    - row_count
    - qty_sum
    - supply_sum
    - tax_sum
    - amount_sum
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []

    group_col = _io_find_first_column(df, group_candidates)
    if not group_col:
        return []

    qty_col = _io_find_first_column(df, qty_candidates)
    supply_col = _io_find_first_column(df, supply_candidates)
    tax_col = _io_find_first_column(df, tax_candidates)

    work = pd.DataFrame()
    work["name"] = df[group_col].map(_io_text_value)

    if qty_col:
        work["qty_sum"] = df[qty_col].map(_io_to_num)
    else:
        work["qty_sum"] = 0.0

    if supply_col:
        work["supply_sum"] = df[supply_col].map(_io_to_num)
    else:
        work["supply_sum"] = 0.0

    if tax_col:
        work["tax_sum"] = df[tax_col].map(_io_to_num)
    else:
        work["tax_sum"] = 0.0

    work["amount_sum"] = work["supply_sum"] + work["tax_sum"]
    work = work[work["name"] != ""]

    if work.empty:
        return []

    try:
        grouped = (
            work.groupby("name", as_index=False)
            .agg(
                row_count=("name", "size"),
                qty_sum=("qty_sum", "sum"),
                supply_sum=("supply_sum", "sum"),
                tax_sum=("tax_sum", "sum"),
                amount_sum=("amount_sum", "sum"),
            )
            .sort_values(["amount_sum", "qty_sum", "row_count"], ascending=[False, False, False])
            .head(limit)
        )
    except Exception:
        return []

    out: list[dict] = []
    for _, row in grouped.iterrows():
        out.append(
            {
                "name": _io_text_value(row.get("name")),
                "row_count": int(_io_to_num(row.get("row_count"))),
                "qty_sum": float(_io_to_num(row.get("qty_sum"))),
                "supply_sum": float(_io_to_num(row.get("supply_sum"))),
                "tax_sum": float(_io_to_num(row.get("tax_sum"))),
                "amount_sum": float(_io_to_num(row.get("amount_sum"))),
            }
        )

    return out


def _io_group_records_to_md(title: str, records: list[dict], *, limit: int = 5) -> str:
    """
    LLM이 바로 읽을 수 있는 그룹 집계 요약 문장.
    내부 key 이름(top_products 등)을 답변에 노출하지 않도록
    자연어 형태로 정리한다.
    """
    title = str(title or "").strip() or "그룹별"
    if not isinstance(records, list) or not records:
        return f"### {title}\n- 집계 대상 없음"

    lines = [f"### {title} 상위 {min(len(records), limit)}개"]

    for i, rec in enumerate(records[:limit], start=1):
        if not isinstance(rec, dict):
            continue

        name = _io_text_value(rec.get("name")) or "(명칭 없음)"
        row_count = int(_io_to_num(rec.get("row_count")))
        qty_sum = _io_to_num(rec.get("qty_sum"))
        supply_sum = _io_to_num(rec.get("supply_sum"))
        tax_sum = _io_to_num(rec.get("tax_sum"))
        amount_sum = _io_to_num(rec.get("amount_sum"))

        lines.append(
            f"- {i}. {name}: "
            f"건수 {_io_fmt_num(row_count)}건 / "
            f"수량 {_io_fmt_num(qty_sum)} / "
            f"공급가액 {_io_fmt_num(supply_sum)} / "
            f"세액 {_io_fmt_num(tax_sum)} / "
            f"합계금액 {_io_fmt_num(amount_sum)}"
        )

    return "\n".join(lines)

# 입고명세/출고명세 LLM 요약 보강
# - 입고명세/출고명세 NLQ에 대해 LLM이 핵심 수치를 바로 이해할 수 있도록 meta에 요약 정보를 보강한다.
# - 제품/거래처별 상위 그룹 목록도 같이 제공한다.
def _doc_group_records_to_md(title: str, records: list[dict], *, limit: int = 5) -> str:
    title = str(title or "").strip() or "그룹별"
    if not isinstance(records, list) or not records:
        return f"### {title}\n- 집계 대상 없음"

    lines = [f"### {title} 상위 {min(len(records), limit)}개"]

    for i, rec in enumerate(records[:limit], start=1):
        if not isinstance(rec, dict):
            continue

        name = _io_text_value(rec.get("name")) or "(명칭 없음)"
        row_count = int(_io_to_num(rec.get("row_count")))
        supply_sum = _io_to_num(rec.get("supply_sum"))
        tax_sum = _io_to_num(rec.get("tax_sum"))
        amount_sum = _io_to_num(rec.get("amount_sum"))
        dc_sum = _io_to_num(rec.get("dc_sum"))
        mismatch_count = int(_io_to_num(rec.get("mismatch_count")))

        lines.append(
            f"- {i}. {name}: "
            f"건수 {_io_fmt_num(row_count)}건 / "
            f"공급가액 {_io_fmt_num(supply_sum)} / "
            f"세액 {_io_fmt_num(tax_sum)} / "
            f"합계금액 {_io_fmt_num(amount_sum)} / "
            f"할인금액 {_io_fmt_num(dc_sum)} / "
            f"불일치 {_io_fmt_num(mismatch_count)}건"
        )

    return "\n".join(lines)


def _get_trans_doc_full_summary(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    거래명세서 공통 LLM 분석용 전체 집계 로딩.
    화면용 TOP 200 DataFrame이 아니라, 서비스의 전체 집계 SQL을 사용한다.
    """
    try:
        from app.services.rddbc130_service import get_rddbc130_analysis_summary

        summary = get_rddbc130_analysis_summary(params)
        return summary if isinstance(summary, dict) else {}
    except Exception:
        return {}


def _get_tax_doc_full_summary(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    세금계산서 공통 LLM 분석용 전체 집계 로딩.
    화면용 TOP 200 DataFrame이 아니라, 서비스의 전체 집계 SQL을 사용한다.
    """
    try:
        from app.services.rddbc140_service import get_rddbc140_analysis_summary

        summary = get_rddbc140_analysis_summary(params)
        return summary if isinstance(summary, dict) else {}
    except Exception:
        return {}

def _monthly_stock_group_records_to_md(title: str, records: list[dict], *, limit: int = 5) -> str:
    """
    월집계 LLM 분석용 그룹 집계 markdown.

    주의:
    - 월집계는 재고잔량표가 아니라 입고/출고 발생 집계다.
    - 입고+출고 단순합산을 대표 지표처럼 노출하지 않는다.
    - LLM 답변 품질을 고정하기 위해 표 형태로 제공한다.
    """
    title = str(title or "").strip() or "월집계"
    if not isinstance(records, list) or not records:
        return f"### {title}\n- 집계 대상 없음"

    use_records = records[:limit]

    lines = [
        f"### {title} 상위 {min(len(records), limit)}개",
        "",
        "| 구분 | 건수 | 입고수량 | 출고수량 | 입고공급가액 | 출고공급가액 |",
        "|---|---:|---:|---:|---:|---:|",
    ]

    for rec in use_records:
        if not isinstance(rec, dict):
            continue

        name = _io_text_value(rec.get("name")) or "(명칭 없음)"
        row_count = int(_io_to_num(rec.get("row_count")))
        in_qty = _io_to_num(rec.get("in_qty_sum"))
        out_qty = _io_to_num(rec.get("out_qty_sum"))
        in_supply = _io_to_num(rec.get("in_supply_sum"))
        out_supply = _io_to_num(rec.get("out_supply_sum"))

        lines.append(
            f"| {name} "
            f"| {_io_fmt_num(row_count)} "
            f"| {_io_fmt_num(in_qty)} "
            f"| {_io_fmt_num(out_qty)} "
            f"| {_io_fmt_num(in_supply)} "
            f"| {_io_fmt_num(out_supply)} |"
        )

    return "\n".join(lines)


def _get_monthly_stock_full_summary(action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    실재고월집계/장부재고월집계 LLM 분석용 전체 집계 로딩.
    화면용 TOP 200 DataFrame이 아니라, 서비스의 전체 집계 SQL을 사용한다.
    """
    try:
        if action == "실재고월집계 조회":
            from app.services.rddbc210_service import get_rddbc210_analysis_summary

            summary = get_rddbc210_analysis_summary(params)
            return summary if isinstance(summary, dict) else {}

        if action == "장부재고월집계 조회":
            from app.services.rddbc220_service import get_rddbc220_analysis_summary

            summary = get_rddbc220_analysis_summary(params)
            return summary if isinstance(summary, dict) else {}

    except Exception:
        return {}

    return {}


# 입고명세/출고명세 LLM 요약 보강
# - 입고명세/출고명세 NLQ에 대해 LLM이 핵심 수치를 바로 이해할 수 있도록 meta에 요약 정보를 보강한다.
# - 제품/거래처별 상위 그룹 목록도 같이 제공한다.
def _get_io_detail_full_summary(action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    입고명세/출고명세 LLM 분석용 전체 집계 로딩.
    화면용 TOP 200 DataFrame이 아니라, 서비스의 전체 집계 SQL을 사용한다.
    """
    try:
        if action == "입고명세 조회":
            from app.services.rddbc110_service import get_rddbc110_analysis_summary

            summary = get_rddbc110_analysis_summary(params)
            return summary if isinstance(summary, dict) else {}

        if action == "출고명세 조회":
            from app.services.rddbc120_service import get_rddbc120_analysis_summary

            summary = get_rddbc120_analysis_summary(params)
            return summary if isinstance(summary, dict) else {}

    except Exception:
        return {}

    return {}

# 입고명세/출고명세 LLM 요약 보강
# - 입고명세/출고명세 NLQ에 대해 LLM이 핵심 수치를 바로 이해할 수 있도록 meta에 요약 정보를 보강한다.
# - 제품/거래처별 상위 그룹 목록도 같이 제공한다.
def _ensure_io_detail_llm_summary(
    payload: Dict[str, Any],
    action: str,
    params: Dict[str, Any],
    query_summary: str,
) -> Dict[str, Any]:
    """
    입고명세/출고명세 payload에 LLM 분석용 요약 meta를 보강한다.

    화면:
    - summary_md에는 조회조건만 표시한다.

    LLM:
    - llm_summary_md
    - in_detail_summary / out_detail_summary
    - 거래처별/제품별/재고위치별/실납처별/영업사원별 집계
    """
    if action not in {"입고명세 조회", "출고명세 조회"} or not isinstance(payload, dict):
        return payload

    meta = dict(payload.get("meta") or {})
    df = _io_payload_to_df(payload)

    row_count = int(
        meta.get("row_count_total")
        or meta.get("row_count")
        or (len(df) if isinstance(df, pd.DataFrame) else 0)
        or 0
    )

    is_in = action == "입고명세 조회"
    summary_key = "in_detail_summary" if is_in else "out_detail_summary"
    analysis_type = "in_detail" if is_in else "out_detail"
    title_label = "입고명세" if is_in else "출고명세"

    visible_summary_md = f"조회조건: {query_summary}"

    if row_count <= 0 or not isinstance(df, pd.DataFrame) or df.empty:
        meta["summary_md"] = visible_summary_md
        meta["llm_summary_md"] = visible_summary_md
        meta["llm_summary_kind"] = f"{analysis_type}_empty"
        meta["analysis_type"] = analysis_type
        meta.setdefault(
            summary_key,
            {
                "row_count": 0,
                "query_summary": query_summary,
            },
        )
        payload["meta"] = meta
        return payload

    if is_in:
        qty_candidates = [
            "입고수량", "수량", "수량합계",
            "Rd11_Quantity", "Rd11_Oquantity",
        ]
        supply_candidates = [
            "최종공급가액", "공급가액", "공급가", "공급가합계",
            "Rd11_Fin_Supply_Price", "Rd11_Supply_Price",
        ]
        tax_candidates = [
            "최종세액", "세액", "부가세", "세액합계",
            "Rd11_Fin_Tax_Price", "Rd11_Tax_Price",
        ]
        vendor_candidates = [
            "거래처명", "매입처명", "입고처명", "Ven_Nm", "Rd03_Ven_Nm",
        ]
        product_candidates = [
            "제품명", "상품명", "Physic_Nm", "Rd04_Physic_Nm",
        ]
        stock_candidates = [
            "재고위치", "재고위치명", "stock_nm",
        ]
        maker_candidates = [
            "제조사명", "제약사명", "maker_nm", "product_ven_nm",
        ]
        staff_candidates = [
            "등록자", "등록자명",
        ]
    else:
        qty_candidates = [
            "출고수량", "수량", "수량합계",
            "Rd12_Quantity", "Rd12_Oquantity",
        ]
        supply_candidates = [
            "최종공급가액", "공급가액", "공급가", "공급가합계",
            "Rd12_Fin_Supply_Price", "Rd12_Supply_Price",
        ]
        tax_candidates = [
            "최종세액", "세액", "부가세", "세액합계",
            "Rd12_Fin_Tax_Price", "Rd12_Tax_Price",
        ]
        vendor_candidates = [
            "거래처명", "매출처명", "Ven_Nm", "Rd03_Ven_Nm",
        ]
        real_vendor_candidates = [
            "실납처명", "실납처", "Real_Ven_Nm",
        ]
        buy_vendor_candidates = [
            "매입처명", "매입처", "In_Ven_Nm",
        ]
        product_candidates = [
            "제품명", "상품명", "Physic_Nm", "Rd04_Physic_Nm",
        ]
        stock_candidates = [
            "재고위치", "재고위치명", "stock_nm",
        ]
        maker_candidates = [
            "제조사명", "제약사명", "maker_nm", "product_ven_nm",
        ]
        staff_candidates = [
            "영업사원명", "영업사원", "Sales_Man_Nm",
        ]

    qty_sum = _io_sum_by_columns(df, qty_candidates)
    supply_sum = _io_sum_by_columns(df, supply_candidates)
    tax_sum = _io_sum_by_columns(df, tax_candidates)
    amount_sum = supply_sum + tax_sum

    vendor_count = _io_distinct_count_by_columns(df, vendor_candidates)
    product_count = _io_distinct_count_by_columns(df, product_candidates)
    stock_count = _io_distinct_count_by_columns(df, stock_candidates)

    maker_col = _io_find_first_column(df, maker_candidates)
    maker_count = (
        _io_distinct_count_by_columns(df, maker_candidates)
        if maker_col
        else None
    )

    staff_col = _io_find_first_column(df, staff_candidates)
    staff_count = (
        _io_distinct_count_by_columns(df, staff_candidates)
        if staff_col
        else None
    )

    top_vendors = _io_group_stats_records(
        df,
        vendor_candidates,
        qty_candidates,
        supply_candidates,
        tax_candidates,
        limit=10,
    )

    top_products = _io_group_stats_records(
        df,
        product_candidates,
        qty_candidates,
        supply_candidates,
        tax_candidates,
        limit=10,
    )

    top_stock_locations = _io_group_stats_records(
        df,
        stock_candidates,
        qty_candidates,
        supply_candidates,
        tax_candidates,
        limit=10,
    )

    top_real_vendors: list[dict] = []
    top_buy_vendors: list[dict] = []
    top_sales_staff: list[dict] = []

    detail_summary: dict[str, Any] = {
        "row_count": row_count,
        "query_summary": query_summary,
        "qty_sum": float(qty_sum),
        "supply_sum": float(supply_sum),
        "tax_sum": float(tax_sum),
        "amount_sum": float(amount_sum),
        "vendor_count": int(vendor_count),
        "product_count": int(product_count),
        "stock_location_count": int(stock_count),
        "top_products": top_products,
        "top_stock_locations": top_stock_locations,
    }

    if maker_count is not None:
        detail_summary["maker_count"] = int(maker_count)

    if is_in:
        detail_summary["top_purchase_vendors"] = top_vendors
        detail_summary["top_vendors"] = top_vendors
    else:
        top_real_vendors = _io_group_stats_records(
            df,
            real_vendor_candidates,
            qty_candidates,
            supply_candidates,
            tax_candidates,
            limit=10,
        )
        top_buy_vendors = _io_group_stats_records(
            df,
            buy_vendor_candidates,
            qty_candidates,
            supply_candidates,
            tax_candidates,
            limit=10,
        )
        top_sales_staff = _io_group_stats_records(
            df,
            staff_candidates,
            qty_candidates,
            supply_candidates,
            tax_candidates,
            limit=10,
        )

        detail_summary["top_sales_vendors"] = top_vendors
        detail_summary["top_vendors"] = top_vendors
        detail_summary["top_real_vendors"] = top_real_vendors
        detail_summary["top_buy_vendors"] = top_buy_vendors
        detail_summary["top_sales_staff"] = top_sales_staff

        if staff_count is not None:
            detail_summary["staff_count"] = int(staff_count)

    # 화면용 표는 TOP 200일 수 있으므로, LLM 분석용 전체 집계는 서비스에서 별도 조회한다.
    display_row_count = int(row_count)
    full_summary = _get_io_detail_full_summary(action, params)

    if isinstance(full_summary, dict) and int(_io_to_num(full_summary.get("row_count_total") or full_summary.get("row_count"))) > 0:
        total_row_count = int(_io_to_num(full_summary.get("row_count_total") or full_summary.get("row_count")))

        row_count = total_row_count
        qty_sum = _io_to_num(full_summary.get("qty_sum"))
        supply_sum = _io_to_num(full_summary.get("supply_sum"))
        tax_sum = _io_to_num(full_summary.get("tax_sum"))
        amount_sum = _io_to_num(full_summary.get("amount_sum"))
        vendor_count = int(_io_to_num(full_summary.get("vendor_count")))
        product_count = int(_io_to_num(full_summary.get("product_count")))
        stock_count = int(_io_to_num(full_summary.get("stock_location_count")))

        if full_summary.get("staff_count") is not None:
            staff_count = int(_io_to_num(full_summary.get("staff_count")))

        # 전체 집계 결과로 detail_summary를 보강/대체한다.
        detail_summary.update(full_summary)
        detail_summary["display_row_count"] = display_row_count
        detail_summary["row_count"] = total_row_count
        detail_summary["row_count_total"] = total_row_count
        detail_summary["summary_basis"] = "전체 조회조건 기준"
    else:
        detail_summary["display_row_count"] = display_row_count
        detail_summary["row_count_total"] = row_count
        detail_summary["summary_basis"] = "표시된 결과 기준"

    maker_line = ""


    if maker_count is not None:
        maker_line = f"\n- 제조사수: **{maker_count:,}개**"

    staff_line = ""
    if (not is_in) and staff_count is not None:
        staff_line = f"\n- 영업사원수: **{staff_count:,}명**"

    if is_in:
        top_sections = "\n\n".join(
            [
                _io_group_records_to_md("매입처별 입고", detail_summary.get("top_purchase_vendors") or []),
                _io_group_records_to_md("제품별 입고", detail_summary.get("top_products") or []),
                _io_group_records_to_md("재고위치별 입고", detail_summary.get("top_stock_locations") or []),
            ]
        )
    else:
        top_sections = "\n\n".join(
            [
                _io_group_records_to_md("매출처별 출고", detail_summary.get("top_sales_vendors") or []),
                _io_group_records_to_md("실납처별 출고", detail_summary.get("top_real_vendors") or []),
                _io_group_records_to_md("제품별 출고", detail_summary.get("top_products") or []),
                _io_group_records_to_md("영업사원별 출고", detail_summary.get("top_sales_staff") or []),
                _io_group_records_to_md("재고위치별 출고", detail_summary.get("top_stock_locations") or []),
            ]
        )

    maker_line = ""
    if maker_count is not None:
        maker_line = f"\n- 제조사수: **{maker_count:,}개**"

    staff_line = ""
    if (not is_in) and staff_count is not None:
        staff_line = f"\n- 영업사원수: **{staff_count:,}명**"

    llm_summary_md = (
        f"조회조건: {query_summary}\n\n"
        f"### {title_label} 전체 요약\n"
        f"- 조회건수: **{row_count:,}건**\n"
        f"- 화면표시건수: **{display_row_count:,}건**\n"
        f"- 수량합계: **{_io_fmt_num(qty_sum)}**\n"
        f"- 공급가액합계: **{_io_fmt_num(supply_sum)}**\n"
        f"- 세액합계: **{_io_fmt_num(tax_sum)}**\n"
        f"- 합계금액: **{_io_fmt_num(amount_sum)}**\n"
        f"- 거래처수: **{vendor_count:,}개**\n"
        f"- 제품수: **{product_count:,}개**"
        f"{maker_line}\n"
        f"- 재고위치수: **{stock_count:,}개**"
        f"{staff_line}\n\n"
        f"{top_sections}\n\n"
        "### 답변 규칙\n"
        "- 위의 거래처별/제품별/재고위치별/영업사원별 집계를 실제 수치와 함께 요약한다.\n"
        "- 화면표시건수와 조회건수가 다르면, 화면에는 일부만 표시되지만 분석은 전체 조회조건 기준이라고 설명한다.\n"
        "- 내부 key 이름(top_products, top_sales_vendors 등)은 답변에 쓰지 않는다.\n"
        "- maker_count 또는 제조사수 항목이 없으면 제조사수, 제조사 정보 누락, 제조사별 분석 불가를 답변에서 언급하지 않는다.\n"
        "- 별도 요청이 없으면 제조사 정보 추가나 시스템 설정 변경을 다음 조회 제안에 넣지 않는다."
    )

    meta["summary_md"] = visible_summary_md
    meta["llm_summary_md"] = llm_summary_md
    meta["llm_summary_kind"] = f"{analysis_type}_summary"
    meta["analysis_type"] = analysis_type
    meta[summary_key] = detail_summary

    # LLM context builder가 meta 중심으로 보게 하기 위한 직접 키
    meta["qty_sum"] = detail_summary["qty_sum"]
    meta["supply_sum"] = detail_summary["supply_sum"]
    meta["tax_sum"] = detail_summary["tax_sum"]
    meta["amount_sum"] = detail_summary["amount_sum"]
    meta["vendor_count"] = detail_summary["vendor_count"]
    meta["product_count"] = detail_summary["product_count"]
    meta["stock_location_count"] = detail_summary["stock_location_count"]

    meta["display_row_count"] = int(display_row_count)
    meta["analysis_row_count"] = int(row_count)
    meta["row_count_total_for_analysis"] = int(row_count)

    if maker_count is not None:
        meta["maker_count"] = int(maker_count)
    else:
        meta.pop("maker_count", None)

    if (not is_in) and staff_count is not None:
        meta["staff_count"] = int(staff_count)
    else:
        meta.pop("staff_count", None)

    meta["field_notes"] = (
        "maker_count 또는 제조사수 항목이 없으면 제조사수, 제조사 정보 누락, "
        "제조사별 분석 불가를 답변에서 언급하지 말 것. "
        "별도 요청이 없으면 제조사 정보 추가나 시스템 설정 변경을 다음 조회 제안에 넣지 말 것."
    )

    payload["meta"] = meta
    payload.setdefault("message", f"{title_label} 조회 결과입니다.")
    return payload

def _ensure_trans_doc_llm_summary(
    payload: Dict[str, Any],
    action: str,
    params: Dict[str, Any],
    query_summary: str,
) -> Dict[str, Any]:
    """
    거래명세서 공통 payload에 LLM 분석용 전체 요약 meta를 보강한다.
    """
    if action != "거래명세서 공통 조회" or not isinstance(payload, dict):
        return payload

    meta = dict(payload.get("meta") or {})
    df = _io_payload_to_df(payload)

    display_row_count = int(
        meta.get("row_count")
        or (len(df) if isinstance(df, pd.DataFrame) else 0)
        or 0
    )

    visible_summary_md = f"조회조건: {query_summary}"

    full_summary = _get_trans_doc_full_summary(params)
    row_count = int(_io_to_num(full_summary.get("row_count_total") or full_summary.get("row_count")))

    if row_count <= 0:
        meta["summary_md"] = visible_summary_md
        meta["llm_summary_md"] = visible_summary_md
        meta["llm_summary_kind"] = "trans_doc_empty"
        meta["analysis_type"] = "trans_doc"
        meta.setdefault(
            "trans_doc_summary",
            {
                "row_count": 0,
                "query_summary": query_summary,
                "display_row_count": display_row_count,
            },
        )
        payload["meta"] = meta
        return payload

    supply_sum = _io_to_num(full_summary.get("supply_sum"))
    tax_sum = _io_to_num(full_summary.get("tax_sum"))
    amount_sum = _io_to_num(full_summary.get("amount_sum"))
    dc_sum = _io_to_num(full_summary.get("dc_sum"))
    mismatch_count = int(_io_to_num(full_summary.get("mismatch_count")))
    vendor_count = int(_io_to_num(full_summary.get("vendor_count")))

    detail_summary = dict(full_summary)
    detail_summary["display_row_count"] = display_row_count
    detail_summary["row_count"] = row_count
    detail_summary["row_count_total"] = row_count
    detail_summary["query_summary"] = query_summary
    detail_summary["summary_basis"] = "전체 조회조건 기준"

    top_sections = "\n\n".join(
        [
            _doc_group_records_to_md("거래명세서 구분별", detail_summary.get("by_trans_type") or []),
            _doc_group_records_to_md("거래처별 거래명세서", detail_summary.get("top_vendors") or []),
            _doc_group_records_to_md("상세합계 일치여부별", detail_summary.get("by_match_status") or []),
            _doc_group_records_to_md("배송구분별", detail_summary.get("by_delivery") or []),
        ]
    )

    llm_summary_md = (
        f"조회조건: {query_summary}\n\n"
        "### 거래명세서 공통 전체 요약\n"
        f"- 조회건수: **{row_count:,}건**\n"
        f"- 화면표시건수: **{display_row_count:,}건**\n"
        f"- 공급가액합계: **{_io_fmt_num(supply_sum)}**\n"
        f"- 세액합계: **{_io_fmt_num(tax_sum)}**\n"
        f"- 합계금액: **{_io_fmt_num(amount_sum)}**\n"
        f"- 할인금액합계: **{_io_fmt_num(dc_sum)}**\n"
        f"- 거래처수: **{vendor_count:,}개**\n"
        f"- 상세합계 불일치/상세없음 건수: **{mismatch_count:,}건**\n\n"
        f"{top_sections}\n\n"
        "### 답변 규칙\n"
        "- 화면표시건수와 조회건수가 다르면, 화면에는 일부만 표시되지만 분석은 전체 조회조건 기준이라고 설명한다.\n"
        "- 거래명세서 구분별, 거래처별, 상세합계 일치여부별 집계를 실제 수치와 함께 요약한다.\n"
        "- 내부 key 이름(by_trans_type, top_vendors 등)은 답변에 쓰지 않는다."
    )

    meta["summary_md"] = visible_summary_md
    meta["llm_summary_md"] = llm_summary_md
    meta["llm_summary_kind"] = "trans_doc_summary"
    meta["analysis_type"] = "trans_doc"
    meta["trans_doc_summary"] = detail_summary

    meta["display_row_count"] = int(display_row_count)
    meta["analysis_row_count"] = int(row_count)
    meta["row_count_total_for_analysis"] = int(row_count)
    meta["supply_sum"] = float(supply_sum)
    meta["tax_sum"] = float(tax_sum)
    meta["amount_sum"] = float(amount_sum)
    meta["dc_sum"] = float(dc_sum)
    meta["vendor_count"] = int(vendor_count)
    meta["mismatch_count"] = int(mismatch_count)

    payload["meta"] = meta
    payload.setdefault("message", "거래명세서 공통 조회 결과입니다.")
    return payload

def _ensure_tax_doc_llm_summary(
    payload: Dict[str, Any],
    action: str,
    params: Dict[str, Any],
    query_summary: str,
) -> Dict[str, Any]:
    """
    세금계산서 공통 payload에 LLM 분석용 전체 요약 meta를 보강한다.
    """
    if action != "세금계산서 공통 조회" or not isinstance(payload, dict):
        return payload

    meta = dict(payload.get("meta") or {})
    df = _io_payload_to_df(payload)

    display_row_count = int(
        meta.get("row_count")
        or (len(df) if isinstance(df, pd.DataFrame) else 0)
        or 0
    )

    visible_summary_md = f"조회조건: {query_summary}"

    full_summary = _get_tax_doc_full_summary(params)
    row_count = int(_io_to_num(full_summary.get("row_count_total") or full_summary.get("row_count")))

    if row_count <= 0:
        meta["summary_md"] = visible_summary_md
        meta["llm_summary_md"] = visible_summary_md
        meta["llm_summary_kind"] = "tax_doc_empty"
        meta["analysis_type"] = "tax_doc"
        meta.setdefault(
            "tax_doc_summary",
            {
                "row_count": 0,
                "query_summary": query_summary,
                "display_row_count": display_row_count,
            },
        )
        payload["meta"] = meta
        return payload

    supply_sum = _io_to_num(full_summary.get("supply_sum"))
    tax_sum = _io_to_num(full_summary.get("tax_sum"))
    amount_sum = _io_to_num(full_summary.get("amount_sum"))
    mismatch_count = int(_io_to_num(full_summary.get("mismatch_count")))
    detail_missing_count = int(_io_to_num(full_summary.get("detail_missing_count")))
    accounting_count = int(_io_to_num(full_summary.get("accounting_count")))
    vendor_count = int(_io_to_num(full_summary.get("vendor_count")))

    detail_summary = dict(full_summary)
    detail_summary["display_row_count"] = display_row_count
    detail_summary["row_count"] = row_count
    detail_summary["row_count_total"] = row_count
    detail_summary["query_summary"] = query_summary
    detail_summary["summary_basis"] = "전체 조회조건 기준"

    top_sections = "\n\n".join(
        [
            _doc_group_records_to_md("세금계산서 구분별", detail_summary.get("by_tax_type") or []),
            _doc_group_records_to_md("거래처별 세금계산서", detail_summary.get("top_vendors") or []),
            _doc_group_records_to_md("상세합계 상태별", detail_summary.get("by_match_status") or []),
        ]
    )

    llm_summary_md = (
        f"조회조건: {query_summary}\n\n"
        "### 세금계산서 공통 전체 요약\n"
        f"- 조회건수: **{row_count:,}건**\n"
        f"- 화면표시건수: **{display_row_count:,}건**\n"
        f"- 공급가액합계: **{_io_fmt_num(supply_sum)}**\n"
        f"- 세액합계: **{_io_fmt_num(tax_sum)}**\n"
        f"- 합계금액: **{_io_fmt_num(amount_sum)}**\n"
        f"- 거래처수: **{vendor_count:,}개**\n"
        f"- 상세합계 불일치 건수: **{mismatch_count:,}건**\n"
        f"- 상세 없음 건수: **{detail_missing_count:,}건**\n"
        f"- 회계분 건수: **{accounting_count:,}건**\n\n"
        f"{top_sections}\n\n"
        "### 답변 규칙\n"
        "- 화면표시건수와 조회건수가 다르면, 화면에는 일부만 표시되지만 분석은 전체 조회조건 기준이라고 설명한다.\n"
        "- 세금계산서 구분별, 거래처별, 상세합계 상태별 집계를 실제 수치와 함께 요약한다.\n"
        "- 회계매입/회계매출은 상세 입출고 연결이 없을 수 있으므로 이를 불일치로 단정하지 않는다.\n"
        "- 내부 key 이름(by_tax_type, top_vendors 등)은 답변에 쓰지 않는다."
    )

    meta["summary_md"] = visible_summary_md
    meta["llm_summary_md"] = llm_summary_md
    meta["llm_summary_kind"] = "tax_doc_summary"
    meta["analysis_type"] = "tax_doc"
    meta["tax_doc_summary"] = detail_summary

    meta["display_row_count"] = int(display_row_count)
    meta["analysis_row_count"] = int(row_count)
    meta["row_count_total_for_analysis"] = int(row_count)
    meta["supply_sum"] = float(supply_sum)
    meta["tax_sum"] = float(tax_sum)
    meta["amount_sum"] = float(amount_sum)
    meta["vendor_count"] = int(vendor_count)
    meta["mismatch_count"] = int(mismatch_count)
    meta["detail_missing_count"] = int(detail_missing_count)
    meta["accounting_count"] = int(accounting_count)

    payload["meta"] = meta
    payload.setdefault("message", "세금계산서 공통 조회 결과입니다.")
    return payload


def _ensure_monthly_stock_llm_summary(
    payload: Dict[str, Any],
    action: str,
    params: Dict[str, Any],
    query_summary: str,
) -> Dict[str, Any]:
    """
    실재고월집계/장부재고월집계 payload에 LLM 분석용 전체 요약 meta를 보강한다.
    """
    if action not in {"실재고월집계 조회", "장부재고월집계 조회"} or not isinstance(payload, dict):
        return payload

    meta = dict(payload.get("meta") or {})
    df = _io_payload_to_df(payload)

    display_row_count = int(
        meta.get("row_count")
        or (len(df) if isinstance(df, pd.DataFrame) else 0)
        or 0
    )

    visible_summary_md = f"조회조건: {query_summary}"

    full_summary = _get_monthly_stock_full_summary(action, params)
    row_count = int(_io_to_num(full_summary.get("row_count_total") or full_summary.get("row_count")))

    analysis_type = "monthly_real_stock" if action == "실재고월집계 조회" else "monthly_book_stock"
    stock_basis = "실재고" if action == "실재고월집계 조회" else "장부재고"
    title_label = "실재고월집계" if action == "실재고월집계 조회" else "장부재고월집계"

    if row_count <= 0:
        meta["summary_md"] = visible_summary_md
        meta["llm_summary_md"] = visible_summary_md
        meta["llm_summary_kind"] = f"{analysis_type}_empty"
        meta["analysis_type"] = analysis_type
        meta.setdefault(
            "monthly_stock_detail_summary",
            {
                "row_count": 0,
                "query_summary": query_summary,
                "display_row_count": display_row_count,
                "stock_basis": stock_basis,
            },
        )
        payload["meta"] = meta
        return payload

    in_qty_sum = _io_to_num(full_summary.get("in_qty_sum"))
    in_bonus_qty_sum = _io_to_num(full_summary.get("in_bonus_qty_sum"))
    in_supply_sum = _io_to_num(full_summary.get("in_supply_sum"))
    in_tax_sum = _io_to_num(full_summary.get("in_tax_sum"))

    out_qty_sum = _io_to_num(full_summary.get("out_qty_sum"))
    out_bonus_qty_sum = _io_to_num(full_summary.get("out_bonus_qty_sum"))
    out_supply_sum = _io_to_num(full_summary.get("out_supply_sum"))
    out_tax_sum = _io_to_num(full_summary.get("out_tax_sum"))

    total_qty_sum = _io_to_num(full_summary.get("total_qty_sum"))
    total_supply_sum = _io_to_num(full_summary.get("total_supply_sum"))
    total_tax_sum = _io_to_num(full_summary.get("total_tax_sum"))

    product_count = int(_io_to_num(full_summary.get("product_count")))
    vendor_count = int(_io_to_num(full_summary.get("vendor_count")))
    stock_location_count = int(_io_to_num(full_summary.get("stock_location_count")))
    stock_apply_count = int(_io_to_num(full_summary.get("stock_apply_count")))

    detail_summary = dict(full_summary)
    detail_summary["display_row_count"] = display_row_count
    detail_summary["row_count"] = row_count
    detail_summary["row_count_total"] = row_count
    detail_summary["query_summary"] = query_summary
    detail_summary["summary_basis"] = "전체 조회조건 기준"
    detail_summary["stock_basis"] = stock_basis

    top_sections = "\n\n".join(
        [
            _monthly_stock_group_records_to_md("월별 집계", detail_summary.get("by_month") or []),
            _monthly_stock_group_records_to_md("집계방향별", detail_summary.get("by_side") or []),
            _monthly_stock_group_records_to_md("입출고구분별", detail_summary.get("by_io_type") or []),
            _monthly_stock_group_records_to_md("제품별 월집계", detail_summary.get("top_products") or []),
            _monthly_stock_group_records_to_md("거래처별 월집계", detail_summary.get("top_vendors") or []),
            _monthly_stock_group_records_to_md("재고위치별 월집계", detail_summary.get("top_stock_locations") or []),
        ]
    )

    llm_summary_md = (
        f"조회조건: {query_summary}\n\n"
        f"### {title_label} 전체 요약\n"
        f"- 재고기준: **{stock_basis}**\n"
        f"- 조회건수: **{row_count:,}건**\n"
        f"- 화면표시건수: **{display_row_count:,}건**\n"
        f"- 제품수: **{product_count:,}개**\n"
        f"- 거래처수: **{vendor_count:,}개**\n"
        f"- 재고위치수: **{stock_location_count:,}개**\n"
        f"- 재고적용처수: **{stock_apply_count:,}개**\n"
        f"- 입고수량합계: **{_io_fmt_num(in_qty_sum)}**\n"
        f"- 입고할증수량합계: **{_io_fmt_num(in_bonus_qty_sum)}**\n"
        f"- 입고공급가액합계: **{_io_fmt_num(in_supply_sum)}**\n"
        f"- 입고세액합계: **{_io_fmt_num(in_tax_sum)}**\n"
        f"- 출고수량합계: **{_io_fmt_num(out_qty_sum)}**\n"
        f"- 출고할증수량합계: **{_io_fmt_num(out_bonus_qty_sum)}**\n"
        f"- 출고공급가액합계: **{_io_fmt_num(out_supply_sum)}**\n"
        f"- 출고세액합계: **{_io_fmt_num(out_tax_sum)}**\n"

        f"{top_sections}\n\n"
        "### 답변 규칙\n"
        "- 실재고월집계와 장부재고월집계는 반드시 같은 답변 형식으로 작성한다.\n"
        "- 답변은 반드시 ① 핵심 요약, ② 주요 수치, ③ 월별 상위 5개 표, ④ 집계방향별 요약, ⑤ 주의/확인할 점, ⑥ 다음 조회 제안 순서로 작성한다.\n"
        "- 주요 수치에는 반드시 조회건수, 화면표시건수, 제품수, 거래처수, 재고위치수, 입고수량, 출고수량, 입고공급가액, 출고공급가액을 포함한다.\n"
        "- 월별 상위 5개는 반드시 표 형태로 작성한다.\n"
        "- 화면표시건수와 조회건수가 다르면, 화면에는 일부만 표시되지만 분석은 전체 조회조건 기준이라고 설명한다.\n"
        "- 월집계는 재고잔량표가 아니라 입고/출고 발생 월집계이다.\n"
        "- 입고수량/입고공급가액과 출고수량/출고공급가액을 분리해서 설명한다.\n"
        "- 입고+출고 단순합산값을 재고금액, 매출액, 전체 공급가액처럼 표현하지 않는다.\n"
        "- 월별, 집계방향별, 입출고구분별, 제품별, 거래처별, 재고위치별 집계를 실제 수치와 함께 요약한다.\n"
        "- 내부 key 이름(by_month, top_products 등)은 답변에 쓰지 않는다."    
    )

    meta["summary_md"] = visible_summary_md
    meta["llm_summary_md"] = llm_summary_md
    meta["llm_summary_kind"] = f"{analysis_type}_summary"
    meta["analysis_type"] = analysis_type
    meta["monthly_stock_detail_summary"] = detail_summary

    meta["display_row_count"] = int(display_row_count)
    meta["analysis_row_count"] = int(row_count)
    meta["row_count_total_for_analysis"] = int(row_count)
    meta["stock_basis"] = stock_basis
    meta["product_count"] = int(product_count)
    meta["vendor_count"] = int(vendor_count)
    meta["stock_location_count"] = int(stock_location_count)
    meta["stock_apply_count"] = int(stock_apply_count)

    meta["sum_in_qty"] = float(in_qty_sum)
    meta["sum_in_bonus_qty"] = float(in_bonus_qty_sum)
    meta["sum_in_supply_amt"] = float(in_supply_sum)
    meta["sum_in_tax_amt"] = float(in_tax_sum)
    meta["sum_out_qty"] = float(out_qty_sum)
    meta["sum_out_bonus_qty"] = float(out_bonus_qty_sum)
    meta["sum_out_supply_amt"] = float(out_supply_sum)
    meta["sum_out_tax_amt"] = float(out_tax_sum)

    payload["meta"] = meta
    payload.setdefault("message", f"{title_label} 조회 결과입니다.")
    return payload

#=============================================================================
# 입출고/명세서/재고 NLQ 요약 보강
# - 제품수불현황뿐만 아니라 입출고/명세서/재고 NLQ 결과에도 meta에 query_summary, summary_md를 보강한다.
# - 이미 query_summary가 있는 payload는 건드리지 않는다.
def _ensure_io_summary_meta(payload: Dict[str, Any], action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    IO NLQ payload에 query_summary / condition / summary_md를 보강한다.
    제품수불현황은 LLM 분석용 flow_summary까지 추가로 보강한다.
    """
    if not isinstance(payload, dict):
        return payload

    meta = dict(payload.get("meta") or {})

    query_summary = str(
        meta.get("query_summary")
        or meta.get("condition")
        or ""
    ).strip()

    if not query_summary:
        query_summary = _build_io_query_summary(action, params)

    if not query_summary:
        query_summary = "전체"

    meta.setdefault("query_summary", query_summary)
    meta.setdefault("condition", query_summary)

    summary_md = str(meta.get("summary_md") or "").strip()

    if summary_md:
        if "조회조건:" not in summary_md:
            meta["summary_md"] = f"조회조건: {query_summary}\n\n{summary_md}"
    else:
        meta["summary_md"] = f"조회조건: {query_summary}"

    payload["meta"] = meta

    # 입고명세/출고명세는 LLM이 사용할 핵심 명세 요약을 추가한다.
    if action in {"입고명세 조회", "출고명세 조회"}:
        payload = _ensure_io_detail_llm_summary(
            payload,
            action,
            params,
            query_summary,
        )

    # 거래명세서 공통은 LLM이 사용할 핵심 거래명세서 요약을 추가한다.
    if action == "거래명세서 공통 조회":
        payload = _ensure_trans_doc_llm_summary(
            payload,
            action,
            params,
            query_summary,
        )

    # 세금계산서 공통은 LLM이 사용할 핵심 세금계산서 요약을 추가한다.
    if action == "세금계산서 공통 조회":
        payload = _ensure_tax_doc_llm_summary(
            payload,
            action,
            params,
            query_summary,
        )

    # 실재고월집계/장부재고월집계는 LLM이 사용할 핵심 월집계 요약을 추가한다.
    if action in {"실재고월집계 조회", "장부재고월집계 조회"}:
        payload = _ensure_monthly_stock_llm_summary(
            payload,
            action,
            params,
            query_summary,
        )

    if action == "세금계산서 공통 조회":
        payload = _ensure_tax_doc_llm_summary(
            payload,
            action,
            params,
            query_summary,
        )


    # 제품수불현황은 기존 보강 함수가 있으면 그대로 사용한다.
    if action == "제품수불현황 조회" and "_ensure_product_flow_llm_summary" in globals():
        payload = _ensure_product_flow_llm_summary(
            payload,
            action,
            params,
            query_summary,
        )

    # 제품재고현황/제품재고장은 기존 보강 함수가 있으면 그대로 사용한다.
    if action == "제품재고현황 조회" and "_ensure_product_inventory_llm_summary" in globals():
        payload = _ensure_product_inventory_llm_summary(
            payload,
            action,
            params,
            query_summary,
        )

    return payload

#=============================================================================
# 입출고/명세서/재고 NLQ 라우팅
# - _looks_like_io_nlq()로 판정된 문장은 _try_handle_io_nlq()로 처리한다.
# - io_nlq가 action/params 해석
# - 서비스 함수 우선 호출 
# - 서비스 실패 시에만 view fallback 
# - 결과를 채팅 pending 큐로 push
def _try_handle_io_nlq(
    txt: str,
    *,
    room: Dict[str, Any],
    session_state: Dict[str, Any],
    make_ts: Callable[[], str],
    next_seq: Callable[[], int],
    logger,
) -> bool:
    """
    입출고/명세서/재고 NLQ 라우팅
    - io_nlq가 action/params 해석
    - service 함수 우선 호출
    - service 실패 시에만 view fallback
    - 결과를 채팅 pending 큐로 push
    """
    try:
        from app.services.io_nlq import resolve_io_nlq
    except Exception:
        logger.exception("[nlq.router] failed to import io_nlq")
        return False

    try:
        from app.ui.chat_middleware import push_sims_result_to_chat
    except Exception:
        logger.exception("[nlq.router] failed to import chat_middleware")
        return False

    parsed = None

    pending_pick = session_state.get("__io_pending_product_pick") or {}
    pick_index = _extract_pending_product_choice_index(txt)

    if pick_index is not None and isinstance(pending_pick, dict):
        pending_action = str(pending_pick.get("action") or "").strip()
        pending_params = dict(pending_pick.get("params") or {})
        candidates = pending_pick.get("candidates") or []

        if pending_action and isinstance(candidates, list) and candidates:

            if 1 <= pick_index <= len(candidates):
                picked = candidates[pick_index - 1] or {}

                picked_code = str(picked.get("제품코드") or "").strip()
                picked_name = str(picked.get("제품명") or "").strip()

                parsed = {
                    "action": pending_action,
                    "params": {
                        **pending_params,
                        "physic_cd": picked_code,
                        "physic_nm": picked_name,
                    },
                }

                # 후보표가 화면에 남아 있는 동안에는
                # 사용자가 3번 선택 후 4번을 다시 입력할 수 있어야 한다.
                # 따라서 후보 pending은 지우지 않고 유지한다.
                session_state["__io_pending_product_pick"] = {
                    "action": pending_action,
                    "params": pending_params,
                    "candidates": candidates,
                    "last_pick_index": pick_index,
                    "last_pick_code": picked_code,
                    "last_pick_name": picked_name,
                }

                # 이번 선택 결과 조회 후에도 pending을 유지하라는 표시
                session_state["__io_keep_pending_product_pick_after_result"] = True

                logger.info(
                    "[nlq.router] io pending product pick resolved idx=%r action=%r code=%r",
                    pick_index,
                    pending_action,
                    picked_code,
                )                
            else:
                payload = {
                    "final": True,
                    "type": "text",
                    "title": pending_action or "제품수불현황 조회",
                    "action": pending_action or "제품수불현황 조회",
                    "params": pending_params,
                    "data": f"후보 번호 {pick_index}번은 없습니다. 1번부터 {len(candidates)}번 중에서 입력해 주세요.",
                    "message": f"후보 번호 {pick_index}번은 없습니다. 1번부터 {len(candidates)}번 중에서 입력해 주세요.",
                    "meta": {
                        "nlq": True,
                        "nlq_query": txt,
                        "_force_push": True,
                        "_nlq_nonce": str(uuid.uuid4()),
                    },
                }
                push_sims_result_to_chat(payload, payload["action"])
                session_state["__scroll_to_msg"] = (
                    session_state.get("__sims_last_msg_id") or session_state.get("__scroll_to_msg")
                )
                return True

    if not isinstance(parsed, dict):
        txt_for_io = _normalize_io_action_spacing(txt)
        txt_for_io = _append_lookup_verb_for_io(txt_for_io)

        parsed = resolve_io_nlq(txt_for_io)

        # 방어 fallback:
        # io_nlq.py가 "제품수불현황 제품명" 형태를 놓쳐도 무응답이 되지 않게 한다.
        if not isinstance(parsed, dict):
            m = re.match(
                r"^(제품수불현황|제품수불부|제품수불)\s+(.+?)(?:\s*조회)?$",
                txt_for_io,
            )
            if m:
                product_text = str(m.group(2) or "").strip()
                product_text = re.sub(
                    r"\s*(조회|검색|찾아줘|찾아봐|찾아|보여줘|알려줘|확인)\s*$",
                    "",
                    product_text,
                ).strip()

                if product_text:
                    parsed = {
                        "action": "제품수불현황 조회",
                        "params": {
                            "physic_nm": product_text,
                        },
                    }

    if not isinstance(parsed, dict):
        return False

    action = str(parsed.get("action") or "").strip()
    params = dict(parsed.get("params") or {})
    if not action:
        return False

    params = _apply_io_recent_1month_default(params, action)
    parsed["params"] = params

    logger.info("[nlq.router] io parsed action=%r params=%r", action, params)

    try:
        from app.ui.chat_middleware import push_sims_result_to_chat
    except Exception:
        logger.exception("[nlq.router] failed to import chat_middleware")
        return False

    def _call_any(fn, params):
        try:
            return fn(params=params)
        except TypeError:
            try:
                return fn(params)
            except TypeError:
                try:
                    return fn(**params)
                except TypeError:
                    return fn()

    def _wrap_df_payload(df: pd.DataFrame, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if df is None:
            df = pd.DataFrame()
        if not isinstance(df, pd.DataFrame):
            try:
                df = pd.DataFrame(df)
            except Exception:
                df = pd.DataFrame()

        return {
            "final": True,
            "type": "table",
            "title": action,
            "action": action,
            "params": params,
            "df": df,
            "df_display": df,
            "columns": list(df.columns),
            "records": df.to_dict(orient="records"),
            "meta": {
                "row_count": int(len(df)),
                "row_count_total": int(len(df)),
            },
        }
# io_nlq 서비스 함수는 dict 또는 str을 반환할 수 있다.
# dict인 경우 실제 표시할 DataFrame이 여러 형태로 들어올 수 있으므로 보정한다.  
# - df_display / df / table / records / data 필드 중에서 DataFrame으로 해석 가능한 것을 찾아서 df/df_display로 보정
# - DataFrame이 없으면 text payload로 보정
    def _normalize_payload(payload, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        def _to_df(obj) -> pd.DataFrame | None:
            if obj is None:
                return None

            if isinstance(obj, pd.DataFrame):
                return obj

            if isinstance(obj, list):
                try:
                    return pd.DataFrame(obj)
                except Exception:
                    return None

            if isinstance(obj, dict):
                # dict 안에 실제 표가 한 번 더 들어 있는 경우 방어
                for k in ("df_display", "df", "table", "records", "rows", "data"):
                    v = obj.get(k)
                    if isinstance(v, pd.DataFrame):
                        return v
                    if isinstance(v, list):
                        try:
                            return pd.DataFrame(v)
                        except Exception:
                            pass

                # 컬럼 dict 형태 방어
                try:
                    df = pd.DataFrame(obj)
                    if isinstance(df, pd.DataFrame):
                        return df
                except Exception:
                    return None

            return None

        if isinstance(payload, pd.DataFrame):
            return _wrap_df_payload(payload, action, params)

        if not isinstance(payload, dict):
            return {
                "final": True,
                "type": "text",
                "title": action,
                "action": action,
                "params": params,
                "data": str(payload),
                "message": str(payload),
            }

        payload.setdefault("title", action)
        payload.setdefault("action", action)
        payload.setdefault("params", params)

        df = None
        for key in ("df_display", "df", "table", "records", "data"):
            df = _to_df(payload.get(key))
            if isinstance(df, pd.DataFrame):
                break

        if isinstance(df, pd.DataFrame) and not df.empty:
            payload["type"] = "table"
            payload["df"] = df
            payload["df_display"] = df
            payload["records"] = df.to_dict(orient="records")
            payload["columns"] = list(df.columns)

            meta = dict(payload.get("meta") or {})
            meta.setdefault("row_count", int(len(df)))
            meta.setdefault("row_count_total", int(len(df)))
            payload["meta"] = meta

            return payload

        # 여기까지 왔다는 것은 실제 표시할 DataFrame이 없다는 뜻.
        # 0건 조회 또는 서비스가 메시지만 반환한 경우이므로 text payload로 보정한다.
        meta = dict(payload.get("meta") or {})

        query_summary = str(
            meta.get("query_summary")
            or meta.get("condition")
            or _build_io_query_summary(action, params)
            or "전체"
        ).strip()

        msg = str(
            payload.get("message")
            or payload.get("data")
            or meta.get("summary_md")
            or ""
        ).strip()

        if not msg:
            msg = f"해당 조회조건의 자료가 없습니다.\n\n조회조건: {query_summary}"

        payload["type"] = "text"
        payload["data"] = msg
        payload["message"] = msg
        payload.pop("df", None)
        payload.pop("df_display", None)

        meta.setdefault("row_count", 0)
        meta.setdefault("row_count_total", 0)
        meta.setdefault("query_summary", query_summary)
        meta.setdefault("condition", query_summary)
        meta.setdefault("summary_md", msg)
        payload["meta"] = meta

        return payload

    service_specs = {        
        "입고명세 조회": (
            "app.services.rddbc110_service",
            ["get_rddbc110_result", "get_result"],
        ),
        "출고명세 조회": (
            "app.services.rddbc120_service",
            ["get_rddbc120_result", "get_result"],
        ),
        "거래명세서 공통 조회": (
            "app.services.rddbc130_service",
            ["get_rddbc130_result", "get_result"],
        ),
        "세금계산서 공통 조회": (
            "app.services.rddbc140_service",
            ["get_rddbc140_result", "get_result"],
        ),
        "실재고월집계 조회": (
            "app.services.rddbc210_service",
            ["get_rddbc210_result", "get_result"],
        ),
        "장부재고월집계 조회": (
            "app.services.rddbc220_service",
            ["get_rddbc220_result", "get_result"],
        ),
        "입고↔거래명세서 검증": (
            "app.services.rddbc110_service",
            ["get_rddbc110_trans_check_result", "get_rddbc110_result"],
        ),
        "입고↔세금계산서 검증": (
            "app.services.rddbc110_service",
            ["get_rddbc110_tax_check_result", "get_rddbc110_result"],
        ),
        "출고↔거래명세서 검증": (
            "app.services.rddbc120_service",
            ["get_rddbc120_trans_check_result", "get_rddbc120_result"],
        ),
        "출고↔세금계산서 검증": (
            "app.services.rddbc120_service",
            ["get_rddbc120_tax_check_result", "get_rddbc120_result"],
        ),
        "제품수불현황 조회": (
            "app.services.product_flow_service",
            ["get_product_flow_result"],
        ),
        "제품재고현황 조회": (
            "app.services.product_inventory_service",
            ["get_product_inventory_result"],
        ),
    }

    payload = None

    spec = service_specs.get(action)
    if spec:
        module_name, preferred_names = spec
        try:
            mod = importlib.import_module(module_name)
            fn = None

            for name in preferred_names:
                cand = getattr(mod, name, None)
                if callable(cand):
                    fn = cand
                    break

            # 검증 action은 일반 get_result로 fallback하면 안 된다.
            # 예: 입고↔거래명세서 검증이 get_rddbc110_result로 떨어지면
            # 최종 action이 입고명세 조회로 바뀌는 문제가 생긴다.
            allow_generic_result_fallback = "검증" not in action

            if fn is None and allow_generic_result_fallback:
                for name in dir(mod):
                    if name.endswith("_result"):
                        cand = getattr(mod, name, None)
                        if callable(cand):
                            fn = cand
                            break

            if fn is not None:
                payload = _call_any(fn, params)
                payload = _normalize_payload(payload, action, params)

                # 최종 action/title은 NLQ parser가 결정한 action을 우선한다.
                payload["title"] = action
                payload["action"] = action

                # 서비스가 date_from/date_to/stock_mode/date_basis/flow_scope 등을
                # 보정해서 payload["params"]에 넣어준 경우가 있다.
                # 기존 params로 덮어쓰면 NLQ 결과 조회조건에서 기간이 빠진다.
                service_params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
                if service_params:
                    params = {**params, **service_params}

                payload["params"] = params

        except Exception:
            logger.exception("[nlq.router] io service failed action=%r module=%r", action, module_name)

    if payload is None:
        try:
            from app.sims.views import rddbc_io_views
        except Exception:
            logger.exception("[nlq.router] failed to import io views")
            return False

        action_map = {
            "입고명세 조회": getattr(rddbc_io_views, "view_rddbc110", None),
            "출고명세 조회": getattr(rddbc_io_views, "view_rddbc120", None),
            "거래명세서 공통 조회": getattr(rddbc_io_views, "view_rddbc130", None),
            "세금계산서 공통 조회": getattr(rddbc_io_views, "view_rddbc140", None),
            "실재고월집계 조회": getattr(rddbc_io_views, "view_rddbc210", None),
            "장부재고월집계 조회": getattr(rddbc_io_views, "view_rddbc220", None),
            "입고↔거래명세서 검증": getattr(rddbc_io_views, "view_rddbc110_trans_check", None),
            "입고↔세금계산서 검증": getattr(rddbc_io_views, "view_rddbc110_tax_check", None),
            "출고↔거래명세서 검증": getattr(rddbc_io_views, "view_rddbc120_trans_check", None),
            "출고↔세금계산서 검증": getattr(rddbc_io_views, "view_rddbc120_tax_check", None),
            "제품수불현황 조회": getattr(rddbc_io_views, "view_rddbc250", None),
            "제품재고현황 조회": getattr(rddbc_io_views, "view_rddbc260", None),
        }

        handler = action_map.get(action)
        if not callable(handler):
            logger.warning("[nlq.router] io action mapped but no handler: %r", action)
            return False

        try:
            payload = _call_any(handler, params)
            payload = _normalize_payload(payload, action, params)
        except Exception:
            logger.exception("[nlq.router] io view fallback failed action=%r params=%r", action, params)
            return False

    payload = _ensure_io_summary_meta(payload, action, params)

    meta = dict(payload.get("meta") or {})
    meta.update(
        {
            "nlq": True,
            "nlq_query": txt,
            "_force_push": True,
            "_nlq_nonce": str(uuid.uuid4()),
        }
    )
    payload["meta"] = meta
    
    try:
        push_sims_result_to_chat(payload, action)
    except Exception:
        logger.exception("[nlq.router] push_sims_result_to_chat failed action=%r", action)
        return False

    session_state["__sims_last_nlq_action"] = action
    session_state["__sims_last_nlq_params"] = params

    pending_rows = meta.get("pending_product_candidates")
    pending_action = str(meta.get("pending_product_action") or action).strip()
    pending_params = dict(meta.get("pending_product_params") or params)

    logger.info(
        "[nlq.router] pending product candidates action=%r rows=%s meta_keys=%s",
        pending_action,
        len(pending_rows) if isinstance(pending_rows, list) else None,
        sorted(list(meta.keys())),
    )


    if isinstance(pending_rows, list) and pending_rows:
        session_state["__io_pending_product_pick"] = {
            "action": pending_action,
            "params": pending_params,
            "candidates": pending_rows,
        }
    else:
        if session_state.pop("__io_keep_pending_product_pick_after_result", False):
            logger.debug("[nlq.router] keep pending product pick after resolved product flow result")
        else:
            session_state.pop("__io_pending_product_pick", None)        

    session_state["__scroll_to_msg"] = (
        session_state.get("__sims_last_msg_id") or session_state.get("__scroll_to_msg")
    )

    logger.info("[nlq.router] io handled action=%r params=%r", action, params)
    return True

def _clean_road_token(value: str) -> str:
    s = str(value or "").strip()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"(조회|검색|찾아줘|찾아봐|찾아|보여줘|알려줘|해줘)$", "", s).strip()
    s = re.sub(r"(이|가|을|를|은|는|의|도|만)$", "", s).strip()
    return s


def _extract_road_label_token(txt: str, label_patterns: str) -> str | None:
    # 예: 시도명 서울 조회 / 도로명 테헤란로 조회 / 도로명주소 강남대로 조회
    m = re.search(
        rf"{label_patterns}\s*(?:에|이|가|은|는)?\s*([^\s,?.!]+)",
        txt,
    )
    if m:
        v = _clean_road_token(m.group(1) or "")
        return v or None
    return None


def _extract_loose_road_keyword(txt: str) -> str | None:
    """
    예:
      - 강남구 도로명주소 조회 -> 강남구
      - 테헤란로 도로명 조회 -> 테헤란로
      - 없는주소가나다빵 도로명주소 조회 -> 없는주소가나다빵
    """
    t = str(txt or "").strip()
    if not t:
        return None

    # 뒤쪽 실행어 제거
    t = re.sub(
        r"\s*(?:조회|검색|찾아줘|찾아봐|찾아|보여줘|알려줘|해줘)\s*$",
        "",
        t,
    ).strip()

    # 뒤쪽 도로명주소/도로명/주소 앵커만 제거
    # 단어 중간의 '주소'는 제거하지 않는다.
    t = re.sub(
        r"\s*(?:도로명주소|도로명|주소)\s*$",
        "",
        t,
    ).strip()

    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return None

    stop = {"전체", "전부", "모두", "목록"}
    parts = [_clean_road_token(x) for x in t.split()]
    parts = [x for x in parts if x and x not in stop]

    if not parts:
        return None

    return parts[0]


def _build_road_query_summary(params: Dict[str, Any]) -> str:
    parts = []

    road_cd = str(params.get("road_cd") or "").strip()
    dong_seq = str(params.get("dong_seq") or "").strip()
    sido_nm = str(params.get("sido_nm") or "").strip()
    gugun_nm = str(params.get("gugun_nm") or "").strip()
    dong_nm = str(params.get("dong_nm") or "").strip()
    road_nm = str(params.get("road_nm") or "").strip()
    road_addr_kw = str(params.get("road_addr_kw") or "").strip()
    keyword = str(params.get("keyword") or "").strip()

    if road_cd:
        parts.append(f"도로명코드 {road_cd}")
    if dong_seq:
        parts.append(f"도로명코드상세번호 {dong_seq}")
    if sido_nm:
        parts.append(f"시도명 {sido_nm}")
    if gugun_nm:
        parts.append(f"시구군명 {gugun_nm}")
    if dong_nm:
        parts.append(f"법정읍면동명 {dong_nm}")
    if road_nm:
        parts.append(f"도로명 {road_nm}")
    if road_addr_kw:
        parts.append(f"도로명주소 {road_addr_kw}")
    elif keyword:
        parts.append(f"통합검색 {keyword}")

    return " / ".join(parts)


def _road_summary_line(query_summary: str) -> str:
    qs = str(query_summary or "").strip()
    return f"조회조건: {qs}" if qs else "조회조건: 전체"


def _try_handle_road_address_nlq(
    txt: str,
    *,
    room: Dict[str, Any],
    session_state: Dict[str, Any],
    make_ts: Callable[[], str],
    next_seq: Callable[[], int],
    logger,
) -> bool:
    """
    Rddbc021 도로명주소 단독 NLQ.
    거래처가 포함된 문장은 nlq_vendors 쪽에서 처리한다.

    지원 조건:
    - 시도명
    - 시구군명
    - 법정읍면동명 / 법정동명
    - 도로명
    - 도로명주소 통합검색

    건물명/우편번호는 현재 도로명주소 화면 레이아웃에 없으므로 처리하지 않는다.
    """
    t = (txt or "").strip()
    if not t:
        return False

    road_signals = (
        "도로명주소",
        "도로명",
        "시도명",
        "시구군명",
        "법정읍면동명",
        "법정동명",
    )
    if not any(k in t for k in road_signals):
        return False

    # 거래처 주소 조건은 거래처 NLQ가 처리한다.
    if "거래처" in t:
        return False

    try:
        from app.services import rddbc021_service as R21
        from app.sims.views.road_address import _prepare_road_address_display
        from app.ui.chat_middleware import push_sims_result_to_chat
    except Exception:
        logger.exception("[nlq.router] failed to import road address handler")
        return False

    sido_nm = _extract_road_label_token(t, r"(?:시도명|시도)")
    gugun_nm = _extract_road_label_token(t, r"(?:시구군명|시군구명|시구군|시군구|구군)")
    dong_nm = _extract_road_label_token(t, r"(?:법정읍면동명|법정동명|읍면동명|동명)")
    road_addr_kw = _extract_road_label_token(t, r"(?:도로명주소)")
    road_nm = _extract_road_label_token(t, r"(?:도로명)(?!주소)")

    keyword = ""
    if not any([sido_nm, gugun_nm, dong_nm, road_nm, road_addr_kw]):
        keyword = _extract_loose_road_keyword(t) or ""

    params = {
        "sido_nm": sido_nm or "",
        "gugun_nm": gugun_nm or "",
        "dong_nm": dong_nm or "",
        "road_nm": road_nm or "",
        "road_addr_kw": road_addr_kw or "",
        "keyword": keyword or "",
    }

    query_summary = _build_road_query_summary(params)
    summary_line = _road_summary_line(query_summary)

    try:
        df_raw = R21.search_road_address(
            sido_nm=params["sido_nm"],
            gugun_nm=params["gugun_nm"],
            dong_nm=params["dong_nm"],
            road_nm=params["road_nm"],
            keyword=params["road_addr_kw"] or params["keyword"],
            top=5000,
        )

        df_view = _prepare_road_address_display(df_raw)

        # 0건 처리
        if df_view is None or df_view.empty:
            display_message = f"해당 조회조건의 자료가 없습니다.\n\n{summary_line}"

            payload = {
                "final": True,
                "type": "text",
                "title": "도로명주소 조회",
                "action": "도로명주소 조회",
                "params": params,
                "data": display_message,
                "message": display_message,
                "meta": {
                    "nlq": True,
                    "master_nlq": True,
                    "domain": "road_address",
                    "source": "도로명주소마스터(Rddbc021)",
                    "nlq_query": txt,
                    "_force_push": True,
                    "_nlq_nonce": str(uuid.uuid4()),
                    "row_count": 0,
                    "row_count_total": 0,
                    "display_row_count": 0,
                    "show_n": 0,
                    "condition": query_summary or "전체",
                    "query_summary": query_summary,
                    "summary_md": summary_line,
                    "note": "조회결과: **0건**",
                    "analysis_type": "road_address_master",
                    "summary_basis": "전체 조회결과 기준",
                    "ts": dt.datetime.now().isoformat(timespec="seconds"),
                },
            }

            push_sims_result_to_chat(payload, "도로명주소 조회")
            session_state["__scroll_to_msg"] = (
                session_state.get("__sims_last_msg_id") or session_state.get("__scroll_to_msg")
            )
            logger.info("[nlq.router] road address handled no rows params=%r", params)
            return True

        # 정상 table 처리
        total = int(len(df_view))
        show_n = min(total, 500)
        df_show = df_view.head(show_n).copy()

        if show_n >= total:
            note = f"조회결과: **{total:,}건** (전부 표시)"
        else:
            note = f"조회결과: **{total:,}건** (표시는 상위 {show_n:,}건)"

        payload = {
            "final": True,
            "type": "table",
            "title": "도로명주소 조회",
            "action": "도로명주소 조회",
            "params": params,
            "df": df_view,
            "df_display": df_show,
            "columns": list(df_show.columns),
            "records": df_show.to_dict(orient="records"),
            "message": f"도로명주소 조회 {total:,}건",
            "meta": {
                "nlq": True,
                "master_nlq": True,
                "domain": "road_address",
                "source": "도로명주소마스터(Rddbc021)",
                "nlq_query": txt,
                "_force_push": True,
                "_nlq_nonce": str(uuid.uuid4()),
                "row_count": total,
                "row_count_total": total,
                "display_row_count": show_n,
                "show_n": show_n,
                "condition": query_summary or "전체",
                "query_summary": query_summary,
                "summary_md": f"{summary_line}\n\n{note}",
                "note": note,
                "analysis_type": "road_address_master",
                "llm_summary_kind": "road_address_master_summary",
                "analysis_row_count": total,
                "row_count_total_for_analysis": total,
                "summary_basis": "전체 조회결과 기준",
                "table_profile": "road_address",
                "hide_meta_expander": True,
                "field_notes": (
                    "도로명주소 마스터 분석은 전체 조회결과 기준으로 답합니다. "
                    "화면 표시는 일부 행으로 제한될 수 있습니다."
                ),
                "ts": dt.datetime.now().isoformat(timespec="seconds"),
            },
        }

        push_sims_result_to_chat(payload, "도로명주소 조회")
        session_state["__scroll_to_msg"] = (
            session_state.get("__sims_last_msg_id") or session_state.get("__scroll_to_msg")
        )
        logger.info("[nlq.router] road address handled params=%r rows=%s", params, total)
        return True

    except Exception:
        logger.exception("[nlq.router] road address nlq failed params=%r", params)
        return False    

def try_handle_nlq(
    user_text: str,
    room: Dict[str, Any],
    session_state: Dict[str, Any],
    make_ts: Callable[[], str],
    next_seq: Callable[[], int],
    logger,
) -> bool:
    """
    자연어 입력을 가로채어(가능하면) DB 조회 표로 반환한다.
    - 성공하면: room.messages에 assistant 메시지(표/텍스트)를 추가하고 True
    - 실패/미해당이면: False (기존 LLM 흐름 진행)
    """
    raw = (user_text or "").strip()
    if not raw:
        return False


#  NLQ 라우팅 우선순위:
# 1) 코드마스터 화면에서 코드마스터 NLQ
# 2) 분석/KPI NLQ
# 3) 입출고/명세서/재고 NLQ (명시적 입출고/명세서/재고 NLQ는 거래처 마스터 NLQ보다 우선)
# 4) 거래처 마스터 NLQ
#   - 거래처 마스터 NLQ는 '재고' 단어가 있어도 입출고/재고 NLQ보다 우선한다.
# 5) 그 외 화면에서 코드마스터 NLQ
#   - 코드마스터 NLQ는 '업무코드' 단어가 없어도 코드마스터 화면에서는 우선 태운다.
#   - 예: "그룹코드 거래처 조회"는 코드마스터 NLQ로 해석한다. "업무코드 거래처 조회"도 코드마스터 NLQ로 해석한다.
#   - 단, 코드마스터 화면이더라도 '업무코드' 단어가 없는 문장이 코드마스터 NLQ로 보이지 않는다면
#    분석/KPI NLQ로 먼저 태운다. 예: "품목별 재고부족현황" 같은 문장은 "재고" 신호 때문에 입출고 계열과 혼동될 수 있다.
# 6) 그 외 화면에서 코드마스터 NLQ
#   - 코드마스터 NLQ는 '업무코드' 단어가 없어도 코드마스터 화면에서는 우선 태운다.
#   - 예: "그룹코드 거래처 조회"는 코드마스터 NLQ로 해석한다. "업무코드 거래처 조회"도 코드마스터 NLQ로 해석한다.
#  - 단, 코드마스터 화면이더라도 '업무코드' 단어가 없는 문장이 코드마스터 NLQ로 보이지 않는다면
#   분석/KPI NLQ로 먼저 태운다. 예: "품목별 재고부족현황" 같은 문장은 "재고" 신호 때문에 입출고 계열과 혼동될 수 있다.
#   - 단, 코드마스터 화면이더라도 '업무코드' 단어가 없는 문장이 코드마스터 NLQ로 보이지 않는다면
#   분석/KPI NLQ로 먼저 태운다. 예: "품목별 재고부족현황" 같은 문장은 "재고" 신호 때문에 입출고 계열과 혼동될 수 있다.
#  - 단, 코드마스터 화면이더라도 '업무코드' 단어가 없는 문장이 코드마스터 NLQ로 보이지 않는다면
#  분석/KPI NLQ로 먼저 태운다. 예: "품목별 재고부족현황" 같은 문장은 "재고" 신호 때문에 입출고 계열과 혼동될 수 있다.
# - 단, 코드마스터 화면이더라도 '업무코드' 단어가 없는 문장이 코드마스터 NLQ로 보이지 않는다면

    # 1) 키보드 보정
    txt = keyboard_fix(raw)

    txt_io = _normalize_io_action_spacing(txt)
    if txt_io != txt:
        logger.debug("[nlq.router] io-action-spacing-fix: %r -> %r", txt, txt_io)

    if txt != raw:
        logger.debug("[nlq.router] keyboard-fix: %r -> %r", raw, txt)

    # 후보 선택 취소: 명시적으로 취소하면 안내 메시지를 표준 SIMS push 경로로 띄운다.
    if _is_pending_pick_cancel_text(txt):
        had_pending_pick = _has_pending_product_pick(session_state)

        _clear_pending_product_pick(session_state)

        try:
            from app.ui.chat_middleware import clear_product_candidate_tables_from_chat
            clear_product_candidate_tables_from_chat()
        except Exception:
            logger.exception("[nlq.router] clear product candidate tables failed")

        msg = "제품 후보 선택을 취소했습니다."

        try:
            from app.ui.chat_middleware import push_sims_result_to_chat

            payload = {
                "final": True,
                "type": "text",
                "title": "제품 후보 선택 취소",
                "action": "제품 후보 선택 취소",
                "data": msg,
                "message": msg,
                "content": msg,
                "meta": {
                    "nlq": True,
                    "nlq_query": txt,
                    "_force_push": True,
                    "_nlq_nonce": str(uuid.uuid4()),
                    "row_count": 0,
                    "row_count_total": 0,
                    "condition": "제품 후보 선택 취소",
                    "query_summary": "제품 후보 선택 취소",
                    "summary_md": msg,
                    "had_pending_product_pick": had_pending_pick,
                },
            }

            push_sims_result_to_chat(payload, payload["action"])

            session_state["__scroll_to_msg"] = (
                session_state.get("__sims_last_msg_id")
                or session_state.get("__scroll_to_msg")
            )

        except Exception:
            logger.exception("[nlq.router] pending product pick cancel push failed")

            msg_id = str(uuid.uuid4())
            room.setdefault("messages", []).append(
                {
                    "id": msg_id,
                    "role": "assistant",
                    "content": msg,
                    "time": make_ts(),
                    "seq": next_seq(),
                }
            )
            session_state["__scroll_to_msg"] = msg_id

        logger.info(
            "[nlq.router] pending product pick cancel handled had_pending=%s",
            had_pending_pick,
        )

        return True
    
    # 후보표가 열려 있는데 번호 선택이 아닌 다른 정상 NLQ가 들어오면
    # 기존 후보 선택 상태를 조용히 닫는다.
    pending_choice_idx = _extract_pending_product_choice_index(txt)
    if _has_pending_product_pick(session_state) and pending_choice_idx is None:
        _clear_pending_product_pick(session_state)
        logger.debug("[nlq.router] cleared pending product pick by new NLQ text=%r", txt)


    current_selected = session_state.get("__sims_selected") or {}
    current_category = str(current_selected.get("category") or "").strip()

    def _looks_like_codes_short_query(text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if "업무코드" in t:
            return True
        code_signals = (
            "그룹코드", "그룹코드명", "그룹명",
            "상세코드", "항목코드",
            "코드종류", "코드종류명",
            "코드명", "한글명", "이름",
            "영문명", "영문이름", "약칭",
            "기타", "기타1", "기타2", "기타3",
            "설명", "비고", "메모",
            "수정자", "수정자명", "변경자",
            "수정일자", "수정일", "변경일자", "변경일",
        )
        return any(k in t for k in code_signals)

    # 2) 코드마스터 화면에 있을 때는 '업무코드' 문구가 없어도 codes NLQ를 먼저 태운다.
    #    예:
    #    - 그룹코드 거래처 조회
    #    - 그룹명 거래처 조회
    #    - 코드종류명 거래처 조회
    try:
        if current_category == "코드마스터" and _looks_like_codes_short_query(txt):
            from app.sims.nlq.nlq_codes import try_handle_codes_nlq

            txt_for_codes = txt if "업무코드" in txt else f"업무코드 {txt}"
            if try_handle_codes_nlq(
                txt_for_codes,
                room=room,
                session_state=session_state,
                make_ts=make_ts,
                next_seq=next_seq,
                logger=logger,
            ):
                return True
    except Exception:
        logger.exception("[nlq.router] failed in codes-category-first routing")

    # 2-1) 분석/KPI NLQ
    # 반드시 io_nlq보다 먼저 태운다.
    # 이유:
    # - "품목별 재고부족현황" 같은 문장은 "재고" 신호 때문에 입출고 계열과 혼동될 수 있다.
    # - "요약" 단어는 컨텍스트 meta 질문보다 먼저 분석 요약표로 처리해야 한다.
    try:
        if _try_handle_analytics_nlq(
            txt,
            room=room,
            session_state=session_state,
            make_ts=make_ts,
            next_seq=next_seq,
            logger=logger,
        ):
            return True
    except Exception:
        logger.exception("[nlq.router] analytics-nlq handler failed")

    # 2-1-1) 도로명주소 단독 NLQ
    # "도로명주소 강남대로 조회"는 거래처 주소가 아니라
    # Rddbc021 도로명주소 마스터로 먼저 처리한다.
    try:
        if _try_handle_road_address_nlq(
            txt,
            room=room,
            session_state=session_state,
            make_ts=make_ts,
            next_seq=next_seq,
            logger=logger,
        ):
            return True
    except Exception:
        logger.exception("[nlq.router] road-address-nlq handler failed")

    # 2-1-1-1) 현재 SIMS 결과 후속 분석 질문
    # 예:
    # - 제품별 매출 금액 및 수량을 조회하고, 상위 20개 제품을 보여줘
    # - 거래처별 매출 상위 20개 보여줘
    # - 재고위치별 출고수량 합계 알려줘
    #
    # 이런 문장은 새 출고명세/입고명세 조회가 아니라,
    # 직전 SIMS 결과 컨텍스트를 LLM이 분석해야 한다.
    try:
        if _looks_like_current_sims_followup_analysis(txt):
            logger.info(
                "[nlq.router] current SIMS follow-up analysis → let LLM handle content=%r",
                txt,
            )
            return False
    except Exception:
        logger.exception("[nlq.router] current SIMS follow-up analysis guard failed")

    # 2-1-2) 명시적 입출고/명세서/재고 NLQ
    # 예:
    # - 입고명세 거래처명 동제 조회
    # - 출고명세 영업사원명 김 조회
    # - 거래명세서 공통 거래처명 동제 조회
    # - 입고 거래명세서 불일치 조회
    #
    # 이런 문장은 거래처 마스터 NLQ가 먼저 가로채면 안 된다.
    try:

        if _is_explicit_io_nlq_phrase(txt_io):
            if _try_handle_io_nlq(
                txt_io,

                room=room,
                session_state=session_state,
                make_ts=make_ts,
                next_seq=next_seq,
                logger=logger,
            ):
                return True
    except Exception:
        logger.exception("[nlq.router] explicit io-nlq handler failed")

    # 2-2) 거래처 마스터 NLQ
    # 거래처 마스터 NLQ는 "재고적용처"처럼 '재고' 단어가 들어가도
    # 입출고/재고 NLQ가 아니라 거래처 마스터 조건으로 우선 해석한다.
    try:
        master_vendor_words = (
            "거래처",
            "거래처명",
            "거래처코드",
            "대표자",
            "대표자명",
            "사업자",
            "사업자번호",
            "사업자등록번호",
            "영업사원",
            "영업사원명",
            "단가적용처",
            "단가적용처명",
            "재고적용처",
            "재고적용처명",
            "전화",
            "전화번호",
            "연락처",
            "휴대폰",
            "핸드폰",
            "팩스",
            "팩스번호",
            "주소",
            "소재지",
        )

        if (not _is_explicit_io_nlq_phrase(txt)) and any(w in txt for w in master_vendor_words):
            from app.sims.nlq.nlq_vendors import try_handle_vendors_nlq

            if try_handle_vendors_nlq(
                txt,
                room=room,
                session_state=session_state,
                make_ts=make_ts,
                next_seq=next_seq,
                logger=logger,
            ):
                return True
    except Exception:
        logger.exception("[nlq.router] vendors nlq failed")

    # 3) 입출고/명세서/재고 NLQ 우선 처리
    try:
        if _try_handle_io_nlq(
            txt,
            room=room,
            session_state=session_state,
            make_ts=make_ts,
            next_seq=next_seq,
            logger=logger,
        ):
            return True
    except Exception:
        logger.exception("[nlq.router] io-nlq handler failed")

    # 3-1) 도로명주소 단독 NLQ
    try:
        if _try_handle_road_address_nlq(
            txt,
            room=room,
            session_state=session_state,
            make_ts=make_ts,
            next_seq=next_seq,
            logger=logger,
        ):
            return True
    except Exception:
        logger.exception("[nlq.router] road-address-nlq handler failed")

    # 4) 컨텍스트 meta 질문
    try:
        if _try_answer_ctx_meta_question(
            txt,
            room=room,
            session_state=session_state,
            make_ts=make_ts,
            next_seq=next_seq,
            logger=logger,
        ):
            return True
    except Exception:
        logger.exception("[nlq.router] ctx-meta handler failed")

    # 5) 업무코드 명시 의도면 codes를 users보다 먼저 태운다.
    try:
        if any(
            k in txt
            for k in (
                "업무코드", "그룹코드", "상세코드", "항목코드",
                "코드종류", "코드종류명", "코드명",
                "한글명", "영문명", "영문이름", "약칭",
                "기타", "기타1", "기타2", "기타3", "설명", "비고", "메모",
                "수정자", "수정자명", "변경자", "수정일자", "수정일", "변경일자", "변경일",
            )
        ):
            from app.sims.nlq.nlq_codes import try_handle_codes_nlq
            if try_handle_codes_nlq(
                txt,
                room=room,
                session_state=session_state,
                make_ts=make_ts,
                next_seq=next_seq,
                logger=logger,
            ):
                return True
    except Exception:
        logger.exception("[nlq.router] failed in codes-first routing")

    # 6) 제품 마스터 의도면 goods를 users보다 먼저 태운다.
    try:
        if _should_try_goods_before_users(txt) and not _is_io_inventory_phrase(txt_io):
            from app.sims.nlq.nlq_goods import try_handle_goods_nlq
            if try_handle_goods_nlq(
                txt,
                room=room,
                session_state=session_state,
                make_ts=make_ts,
                next_seq=next_seq,
                logger=logger,
            ):
                return True
    except Exception:
        logger.exception("[nlq.router] failed in goods-first routing")

    # 7) 거래처 등록/수정 속성 질의는, 거래처 anchor가 분명할 때만
    #    users보다 먼저 vendors로 보낸다.
    #    - 예: 거래처 등록자 관리자 조회
    #    - 예: 등록일자 201508 거래처 조회
    #    - 예: 영업사원명 김 거래처 조회
    #    - 단, 사용자/제품/업무코드 anchor가 있으면 여기서 가로채지 않는다.
    try:
        vendor_anchor_words = (
            "거래처", "거래처명", "거래처코드",
            "거래처목록", "거래처 목록",
            "거래처마스터", "거래처 마스터",
        )
        vendor_master_attr_words = (
            "등록자", "등록자명", "등록일자", "등록일",
            "수정자", "수정자명", "수정일자", "수정일",
            "대표자", "대표자명",
            "영업사원", "영업사원명",
            "사업자번호", "사업자등록번호",
            "주소", "소재지",
            "매입처", "매출처", "제조사", "발주처",
            "단가적용처", "단가적용처명",
            "재고적용처", "재고적용처명",
        )
        user_anchor_words = (
            "사용자", "사용자명", "사용자코드", "사용자ID", "사번",
            "부서", "직책", "영업지역", "재고위치",
        )
        goods_anchor_words = (
            "제품", "상품", "제품코드", "제품명", "상품명",
            "보험코드", "바코드", "제품그룹", "제품분류",
        )
        code_anchor_words = (
            "업무코드", "그룹코드", "상세코드", "항목코드",
            "코드종류", "코드종류명", "코드명",
        )

        has_vendor_anchor = any(k in txt for k in vendor_anchor_words)
        has_vendor_attr = any(k in txt for k in vendor_master_attr_words)
        has_user_anchor = any(k in txt for k in user_anchor_words)
        has_goods_anchor = any(k in txt for k in goods_anchor_words)
        has_code_anchor = any(k in txt for k in code_anchor_words)

        if (
            has_vendor_anchor
            and has_vendor_attr
            and not _has_vendor_txn_signal(txt)
            and not has_user_anchor
            and not has_goods_anchor
            and not has_code_anchor
        ):
            from app.sims.nlq.nlq_vendors import try_handle_vendors_nlq
            if try_handle_vendors_nlq(
                txt,
                room=room,
                session_state=session_state,
                make_ts=make_ts,
                next_seq=next_seq,
                logger=logger,
            ):
                return True
    except Exception:
        logger.exception("[nlq.router] failed in vendors-anchored-first routing")




    # 8) 사용자 NLQ
    try:
        from app.sims.nlq.nlq_users import try_handle_users_nlq
        if try_handle_users_nlq(
            txt,
            room=room,
            session_state=session_state,
            make_ts=make_ts,
            next_seq=next_seq,
            logger=logger,
        ):
            return True
    except Exception:
        logger.exception("[nlq.router] failed to import/handle users handler")

    # 9) 거래처 마스터 의도면 vendors를 goods보다 먼저 태운다.
    try:
        if _should_try_vendors_before_goods(txt):
            from app.sims.nlq.nlq_vendors import try_handle_vendors_nlq
            if try_handle_vendors_nlq(
                txt,
                room=room,
                session_state=session_state,
                make_ts=make_ts,
                next_seq=next_seq,
                logger=logger,
            ):
                return True
    except Exception:
        logger.exception("[nlq.router] failed in vendors-first routing")

    # 10) 제품(상품) NLQ fallback
    try:
        if not _is_io_inventory_phrase(txt):
            from app.sims.nlq.nlq_goods import try_handle_goods_nlq
            if try_handle_goods_nlq(
                txt,
                room=room,
                session_state=session_state,
                make_ts=make_ts,
                next_seq=next_seq,
                logger=logger,
            ):
                return True
    except Exception:
        logger.exception("[nlq.router] failed to import/handle goods handler")
        

    # 11) 업무코드 NLQ
    try:
        from app.sims.nlq.nlq_codes import try_handle_codes_nlq
        if try_handle_codes_nlq(
            txt,
            room=room,
            session_state=session_state,
            make_ts=make_ts,
            next_seq=next_seq,
            logger=logger,
        ):
            return True
    except Exception:
        logger.exception("[nlq.router] failed to import/handle codes handler")

    return False
