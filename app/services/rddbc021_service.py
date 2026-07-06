# app/services/rddbc021_service.py

from __future__ import annotations

import os
from typing import Optional, Any
import logging
import time

from app.db.mssql_client import read_df, log_sql
from app.db.schema_map import SCHEMA as S

log = logging.getLogger("ssai.sims.rddbc021")


try:
    import streamlit as st
    _cache_data = st.cache_data
except Exception:
    def _cache_data(*args, **kwargs):
        def deco(fn):
            return fn
        return deco


T = S["tables"]["rddbc021"]
C = S["cols"]["rddbc021"]


COL_ROAD_CD = C.get("road_cd", "Rd021_RoadCd")
COL_DONG_SEQ = C.get("dong_seq", "Rd021_DongSeq")
COL_ROAD_NM = C.get("road_nm", "Rd021_RoadNm")
COL_ROAD_ENM = C.get("road_enm", "Rd021_RoadEnm")
COL_SIDO = C.get("sido", "Rd021_Sido")
COL_GUGUN = C.get("gugun", "Rd021_Gugun")
COL_DONG_GU = C.get("dong_gu", "Rd021_DongGu")
COL_DONG_CD = C.get("dong_cd", "Rd021_DongCd")
COL_DONG_NM = C.get("dong_nm", "Rd021_DongNm")


def _trim(expr: str) -> str:
    return f"LTRIM(RTRIM({expr}))"


def _clean_text(value: Optional[Any]) -> str:
    return str(value or "").strip()


def _like(value: Optional[Any]) -> Optional[str]:
    text = _clean_text(value)
    if not text:
        return None
    return f"%{text}%"


def _service_master_max_rows(default: int = 30000) -> int:
    """
    마스터 조회 공통 상한.
    새 env를 만들지 않고 기존 SIMS_PANEL_DISPLAY_MAX_ROWS /
    SIMS_CHAT_DISPLAY_MAX_ROWS 값을 사용한다.
    """
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


def _normalize_top(value: int, default: int = 200, max_value: Optional[int] = None) -> int:
    try:
        v = int(value)
    except Exception:
        v = default

    if v < 1:
        v = default

    if max_value is None:
        max_value = _service_master_max_rows()

    return min(v, int(max_value))


def _run_df(action: str, sql: str, params: tuple):
    t0 = time.perf_counter()
    try:
        df = read_df(sql, params)
        ms = int((time.perf_counter() - t0) * 1000)
        try:
            log.info(
                "[svc.rddbc021] action=%s rows=%s elapsed_ms=%s params=%s",
                action, len(df), ms, params
            )
        except Exception:
            pass
        return df
    except Exception:
        log_sql(f"rddbc021.{action}.ERROR", sql, params)
        raise


@_cache_data(ttl=60, show_spinner=False)
def search_rows(
    *,
    road_cd: str = "",
    dong_seq: str = "",
    sido_nm: str = "",
    gugun_nm: str = "",
    dong_nm: str = "",
    road_nm: str = "",
    keyword: str = "",
    top: int = 500,
):
    top = _normalize_top(top)

    where = ["1=1"]
    params: list[Any] = []

    road_cd = _clean_text(road_cd)
    if road_cd:
        where.append(f"{_trim(f'A.{COL_ROAD_CD}')} = ?")
        params.append(road_cd)

    dong_seq = _clean_text(dong_seq)
    if dong_seq:
        where.append(f"{_trim(f'A.{COL_DONG_SEQ}')} = ?")
        params.append(dong_seq)

    kw_sido = _like(sido_nm)
    if kw_sido:
        where.append(f"{_trim(f'A.{COL_SIDO}')} LIKE ?")
        params.append(kw_sido)

    kw_gugun = _like(gugun_nm)
    if kw_gugun:
        where.append(f"{_trim(f'A.{COL_GUGUN}')} LIKE ?")
        params.append(kw_gugun)

    kw_dong = _like(dong_nm)
    if kw_dong:
        where.append(f"{_trim(f'A.{COL_DONG_NM}')} LIKE ?")
        params.append(kw_dong)

    kw_road = _like(road_nm)
    if kw_road:
        where.append(f"{_trim(f'A.{COL_ROAD_NM}')} LIKE ?")
        params.append(kw_road)

    kw = _like(keyword)
    if kw:
        keyword_terms = [
            f"{_trim(f'A.{COL_ROAD_CD}')} LIKE ?",
            f"{_trim(f'A.{COL_DONG_SEQ}')} LIKE ?",
            f"{_trim(f'A.{COL_ROAD_NM}')} LIKE ?",
            f"{_trim(f'A.{COL_ROAD_ENM}')} LIKE ?",
            f"{_trim(f'A.{COL_SIDO}')} LIKE ?",
            f"{_trim(f'A.{COL_GUGUN}')} LIKE ?",
            f"{_trim(f'A.{COL_DONG_CD}')} LIKE ?",
            f"{_trim(f'A.{COL_DONG_NM}')} LIKE ?",
        ]
        where.append("(" + " OR ".join(keyword_terms) + ")")
        params.extend([kw] * len(keyword_terms))

    sql = f"""
    SELECT TOP {top}
           A.{COL_ROAD_CD}  AS Rd021_RoadCd,
           A.{COL_DONG_SEQ} AS Rd021_DongSeq,
           A.{COL_SIDO}     AS Rd021_Sido,
           A.{COL_GUGUN}    AS Rd021_Gugun,
           A.{COL_DONG_NM}  AS Rd021_DongNm,
           A.{COL_ROAD_NM}  AS Rd021_RoadNm,
           A.{COL_ROAD_ENM} AS Rd021_RoadEnm,
           A.{COL_DONG_GU}  AS Rd021_DongGu,
           A.{COL_DONG_CD}  AS Rd021_DongCd
    FROM {T} AS A WITH (NOLOCK)
    WHERE {" AND ".join(where)}
    ORDER BY
           A.{COL_SIDO},
           A.{COL_GUGUN},
           A.{COL_DONG_NM},
           A.{COL_ROAD_NM},
           A.{COL_ROAD_CD},
           A.{COL_DONG_SEQ}
    """

    return _run_df("search_rows", sql, tuple(params))


def search_road_address(
    *,
    road_cd: str = "",
    dong_seq: str = "",
    sido_nm: str = "",
    gugun_nm: str = "",
    dong_nm: str = "",
    road_nm: str = "",
    keyword: str = "",
    top: int = 500,
):
    return search_rows(
        road_cd=road_cd,
        dong_seq=dong_seq,
        sido_nm=sido_nm,
        gugun_nm=gugun_nm,
        dong_nm=dong_nm,
        road_nm=road_nm,
        keyword=keyword,
        top=top,
    )