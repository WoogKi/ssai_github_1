"""Shared product-master display projection without NLQ routing dependencies."""

from __future__ import annotations

from typing import Any

import pandas as pd

_GOODS_DISPLAY_FRONT_COLS = (
    "제품코드", "제품명", "규격", "단위", "제약사명", "제약사 담당자",
    "발주처명", "발주처 담당자", "제품그룹명", "구분명",
)


def _ensure_df(obj: Any) -> pd.DataFrame:
    if obj is None:
        return pd.DataFrame()
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    try:
        return pd.DataFrame(obj)
    except Exception:
        return pd.DataFrame()


def build_goods_display_df(df: pd.DataFrame, *, detail: bool = False) -> pd.DataFrame:
    work = _ensure_df(df)
    if work.empty:
        return work

    def _pick_col(df_src: pd.DataFrame, candidates: list[str]) -> str:
        for c in candidates:
            if c in df_src.columns:
                return c
        return ""

    def _norm_series(sr: pd.Series) -> pd.Series:
        return (
            sr.fillna("")
            .astype(str)
            .replace({"None": "", "nan": "", "<NA>": ""})
            .str.strip()
        )

    def _fmt_char8_date(sr: pd.Series) -> pd.Series:
        s = (
            sr.fillna("")
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
            .replace({
                "": None,
                "0": None,
                "00000000": None,
                "19000101": None,
                "20010101": None,
                "99999999": None,
                "None": None,
                "nan": None,
                "<NA>": None,
            })
        )
        dtv = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
        return dtv.dt.strftime("%Y-%m-%d").fillna("")

    def _fmt_datetime(sr: pd.Series) -> pd.Series:
        s = (
            sr.fillna("")
            .astype(str)
            .str.strip()
            .replace({
                "": None,
                "None": None,
                "nan": None,
                "<NA>": None,
            })
        )
        dtv = pd.to_datetime(s, errors="coerce")
        return dtv.dt.strftime("%Y-%m-%d %H:%M:%S").fillna("")

    out = work.copy()

    # 이름 컬럼 우선, 코드 컬럼은 fallback
    add_name_col = _pick_col(out, ["__raw_add_user_nm", "add_user_nm", "등록자명", "등록자"])
    add_code_col = _pick_col(out, ["등록자코드", "Rd04_Add_Cd"])
    mod_name_col = _pick_col(out, ["__raw_mod_user_nm", "mod_user_nm", "수정자명", "수정자"])
    mod_code_col = _pick_col(out, ["수정자코드", "Rd04_Mod_Cd"])

    if add_name_col:
        out["등록자"] = _norm_series(out[add_name_col])
    elif add_code_col:
        out["등록자"] = _norm_series(out[add_code_col])
    else:
        out["등록자"] = ""

    if mod_name_col:
        out["수정자"] = _norm_series(out[mod_name_col])
    elif mod_code_col:
        out["수정자"] = _norm_series(out[mod_code_col])
    else:
        out["수정자"] = ""

    if "등록일자" not in out.columns and "Rd04_Add_Date" in out.columns:
        out["등록일자"] = out["Rd04_Add_Date"]
    if "수정일자" not in out.columns and "Rd04_Mod_Date" in out.columns:
        out["수정일자"] = out["Rd04_Mod_Date"]
    for col in ["보험수가변경일자", "이전보험수가변경일자", "최종단가변경일자", "등록일자", "수정일자"]:
        if col in out.columns:
            out[col] = _fmt_char8_date(out[col])
    for col in [
        "표준코드수정일시", "대표코드수정일시", "보험코드수정일시",
        "Rddbc046 등록일자", "Rddbc046 수정일자",
    ]:
        if col in out.columns:
            out[col] = _fmt_datetime(out[col])


    if "계산단위" in out.columns:
        num = pd.to_numeric(out["계산단위"], errors="coerce")
        non_na = num.dropna()
        if not non_na.empty and ((non_na % 1) == 0).all():
            out["계산단위"] = num.round(0).astype("Int64")
        else:
            out["계산단위"] = num.round(3)

    for col in ["보험가격", "보험단가", "이전보험가격", "이전보험단가", "단가"]:
        if col in out.columns:
            num = pd.to_numeric(out[col], errors="coerce").round(0)
            out[col] = num.astype("Int64")

    if detail:
        preferred = [
            "제품코드", "보험코드", "제품명", "출력명", "약어명", "제약사명",
            "제품그룹명", "구분명", "제품플래그명", "함량명", "제품분류명",
            "규격", "단위",
            "계산단위",
            "보험수가변경일자", "보험가격", "보험단가",
            "이전보험수가변경일자", "이전보험가격", "이전보험단가",
            "최종단가변경일자", "단가",
            "표준코드제품명", "표준코드", "표준코드수정일시",
            "대표코드", "대표코드수정일시", "Rddbc046 보험코드", "보험코드수정일시",
            "WEB 재고사용여부", "보험수가변경사유", "보험코드변경사유",
            "Rddbc046 등록자코드", "Rddbc046 등록일자", "Rddbc046 수정자코드", "Rddbc046 수정일자",
            "표준코드정리사용여부", "신고계산단위", "신고환산단위", "신고환산단위사용구분",
            "특수관리제품코드", "특수관리제품",
            "바코드1", "바코드2", "바코드3", "바코드4", "바코드5",
            "사용구분", "삭제/사용여부",
            "등록자", "등록일자", "수정자", "수정일자",
                ]
    else:
        preferred = [
            "제품코드", "보험코드", "제품명", "제약사명",
            "제품그룹명", "구분명", "제품플래그명", "함량명", "제품분류명",
            "규격", "단위",
            "계산단위",
            "보험수가변경일자", "보험가격", "보험단가",
            "이전보험수가변경일자", "이전보험가격", "이전보험단가",
            "최종단가변경일자", "단가",
            "표준코드제품명", "표준코드", "표준코드수정일시",
            "대표코드", "대표코드수정일시", "Rddbc046 보험코드", "보험코드수정일시",
            "WEB 재고사용여부", "보험수가변경사유", "보험코드변경사유",
            "Rddbc046 등록자코드", "Rddbc046 등록일자", "Rddbc046 수정자코드", "Rddbc046 수정일자",
            "표준코드정리사용여부", "신고계산단위", "신고환산단위", "신고환산단위사용구분",
            "특수관리제품코드", "특수관리제품",
            "바코드1", "바코드2", "바코드3", "바코드4", "바코드5",
            "사용구분", "삭제/사용여부",
            "등록자", "등록일자", "수정자", "수정일자",
        ]

    preferred = [c for c in preferred if c not in _GOODS_DISPLAY_FRONT_COLS and c != "보험코드"]
    if "보험코드" in out.columns:
        insurance_position = preferred.index("보험단가") + 1 if "보험단가" in preferred else len(preferred)
        preferred.insert(insurance_position, "보험코드")
    preferred = [c for c in _GOODS_DISPLAY_FRONT_COLS if c in out.columns] + [c for c in preferred if c in out.columns]
    return out[preferred].copy() if preferred else out.copy()


def order_goods_full_columns(df: pd.DataFrame, display_df: pd.DataFrame) -> pd.DataFrame:
    """Match displayed columns first while retaining every original export column."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    displayed = [column for column in display_df.columns if column in df.columns]
    return df.loc[:, displayed + [column for column in df.columns if column not in displayed]].copy()

