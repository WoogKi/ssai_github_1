# app/services/utils.py
import pandas as pd
from typing import Iterable, Optional
from app.db.labels_map import LABELS as STATIC_LABELS
try:
    from app.db.labels_loader import load_labels_for_table
except Exception:
    load_labels_for_table = None

def apply_labels(df: pd.DataFrame, table: str, table_name_in_db: Optional[str] = None) -> pd.DataFrame:
    """
    Rename DataFrame columns to human-readable labels.
    Priority: dynamic(DB extended properties) > static LABELS map.
    `table_name_in_db` allows overriding the physical table name if it differs.
    """
# ============ delete
#    # dynamic (선택)
#    import logging
#    log = logging.getLogger("ssai")
#    log.info("[DEBUG.labels] apply_labels table=%s table_name_in_db=%s", table, table_name_in_db)
# ============ delete
    if df is None or df.empty:
        return df

    # 1) 동적 라벨 (DB 메타) - rddbc060 위주로만 사용
    dyn = {}
    USE_DYNAMIC = (table == "rddbc060")

    if load_labels_for_table  and USE_DYNAMIC:
        if table_name_in_db:
            # 호출부에서 명시적으로 이름을 넘긴 경우에만 그대로 사용
            dyn = load_labels_for_table(table_name_in_db)
        else:
            infer = {
                # "rddbc010": "Rddbc010",
                "rddbc060": "Rddbc060",
            }.get(table)
            if infer:
                dyn = load_labels_for_table(infer)
    # 2) 정적 라벨 (labels_map.py)
    static = STATIC_LABELS.get(table, {})
    # ⚠ 동적 라벨 중 값이 None / "" 인 것은 무시한다.
    #    그렇지 않으면 컬럼명이 실제로 None 이 되어버리고,
    #    make_unique_columns() 과정에서 'None', 'None_2', 'None_3' 같은 이름이 생긴다.
    merged = static.copy()
    for k, v in (dyn or {}).items():
        # 라벨 값이 비어있으면(숨기기 용도 등) 여기서는 덮어쓰지 않음
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        merged[k] = v

    # 🔹 rddbc060: 삭제 플래그 한글명 강제 통일
    #   - DB Extended Property 에서 '태삭제상' 같은 값이 와도
    #     화면/컨텍스트에서는 항상 '삭제상태' 로 보이도록 오버라이드
    if table == "rddbc060":
        if "Rd06_Del_Flag" in merged:
            merged["Rd06_Del_Flag"] = "삭제상태"

    return df.rename(columns=merged)

def mask_series(s: pd.Series, show_last: int = 4, mask_char: str = "•") -> pd.Series:
    def _m(v):
        if not isinstance(v, str):
            v = str(v) if v is not None else ""
        keep = v[-show_last:] if show_last > 0 else ""
        return (mask_char * max(0, len(v) - len(keep))) + keep
    return s.map(_m)

def mask_columns(df: pd.DataFrame, cols: Iterable[str], show_last: int = 4) -> pd.DataFrame:
    df = df.copy()
    for c in cols:
        if c in df.columns:
            df[c] = mask_series(df[c], show_last=show_last)
    return df

def make_unique_columns(df):
    """중복 열명을 _2, _3로 고유화. 원본을 변경하지 않고 새 DF 반환."""
    cols = list(df.columns)
    seen = {}
    out = []
    for c in cols:
        base = str(c)
        if base not in seen:
            seen[base] = 1
            out.append(base)
        else:
            seen[base] += 1
            out.append(f"{base}_{seen[base]}")
    df2 = df.copy()
    df2.columns = out
    return df2
