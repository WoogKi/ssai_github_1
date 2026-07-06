# tools/ssai_test_permission_policy.py
#
# 권한 매핑 파일 테스트 용
#
# Create 2026/06/22


from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.ssai_permission_policy import (  # noqa: E402
    describe_permission,
    get_required_permission,
)


TEST_CASES = [
    {"category": "사용자", "action": "사용자목록 + 부서명"},
    {"category": "거래처", "action": "거래처 목록"},
    {"category": "제품", "action": "제품코드 목록"},
    {"category": "입고", "action": "입고명세 조회"},
    {"category": "출고", "action": "출고명세 조회"},
    {"category": "재고", "action": "제품재고현황 조회"},
    {"category": "거래명세서", "action": "거래명세서 공통 조회"},
    {"category": "세금계산서", "action": "세금계산서 공통 조회"},
    {"category": "분석/KPI", "action": "품목별 매출 예상"},
    {"category": "분석/KPI", "action": "품목별 재고부족현황"},
    {"special": "excel_download"},
    {"special": "file_upload"},
    {"special": "machine_learning"},
]


def main() -> None:
    for case in TEST_CASES:
        permission = get_required_permission(
            category=case.get("category"),
            action=case.get("action"),
            special=case.get("special"),
        )

        print(
            f"[OK] case={case} "
            f"=> permission={permission} "
            f"({describe_permission(permission)})"
        )


if __name__ == "__main__":
    main()