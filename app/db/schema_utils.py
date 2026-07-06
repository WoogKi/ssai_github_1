# app/db/schema_utils.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Mapping


def al(mapping: Mapping[str, Any] | None, key: str, default: str) -> str:
    """
    schema_map/aliases/cols 등에서 안전하게 값을 꺼내는 공용 헬퍼.
    - dict/Mapping 아니면 default
    - key / key.lower() 순서로 조회
    - 값이 None/빈문자/"None"/"null"/"nan" 류면 default
    """
    try:
        if not isinstance(mapping, Mapping):
            return default
        k = (key or "").strip()
        if not k:
            return default

        v = mapping.get(k)
        if v is None and k.lower() != k:
            v = mapping.get(k.lower())

        if v is None:
            return default

        # 문자열 "None"/"null"/"nan" 같은 애매 값 방어
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return default
            if s.lower() in ("none", "null", "nan"):
                return default
            return s

        # 숫자/기타 타입이면 문자열로 변환해도 되지만, 여기선 그대로 사용
        return str(v)
    except Exception:
        return default
