# app/services/ssai_permission_policy.py
#
# 권한 매핑 파일부터 만듭니다
#
# Create 2026/06/20


from __future__ import annotations


# =========================================================
# SS AI Phase 3 권한 정책
#
# permission_code 기준:
# - MASTER_READ
# - IO_READ
# - KPI_READ
# - EXPORT_EXCEL
# - UPLOAD_FILE
# - RAG_USE
# - MACHINE_LEARNING_USE
# - COMPANY_MANAGE
# - USER_APPROVE
# - USER_MANAGE_ALL
# - USER_MANAGE_COMPANY
# =========================================================


CATEGORY_PERMISSION_MAP: dict[str, str] = {
    # 마스터 / 기준정보
    "업무코드": "MASTER_READ",
    "코드": "MASTER_READ",
    "코드마스터": "MASTER_READ",
    "사용자": "MASTER_READ",
    "거래처": "MASTER_READ",
    "제품": "MASTER_READ",
    "제품코드": "MASTER_READ",
    "도로명주소": "MASTER_READ",

    # 입출고 / 재고 / 문서
    "입고": "IO_READ",
    "출고": "IO_READ",
    "재고": "IO_READ",
    "입출고": "IO_READ",
    "거래명세서": "IO_READ",
    "세금계산서": "IO_READ",
    "검증": "IO_READ",

    # 분석 / KPI
    "분석": "KPI_READ",
    "KPI": "KPI_READ",
    "분석/KPI": "KPI_READ",
    "업무그룹": "KPI_READ",
}


ACTION_PERMISSION_MAP: dict[str, str] = {
    # 마스터
    "사용자목록 + 부서명": "MASTER_READ",
    "거래처 목록": "MASTER_READ",
    "제품코드 목록": "MASTER_READ",
    "제품코드 상세": "MASTER_READ",
    "업무코드 조회": "MASTER_READ",
    "코드명 검색": "MASTER_READ",
    "그룹코드조회": "MASTER_READ",
    "도로명주소 조회": "MASTER_READ",

    # 입출고 / 재고
    "입고명세 조회": "IO_READ",
    "출고명세 조회": "IO_READ",
    "제품재고현황 조회": "IO_READ",
    "제품수불현황 조회": "IO_READ",
    "실재고월집계 조회": "IO_READ",
    "장부재고월집계 조회": "IO_READ",

    # 문서
    "거래명세서 공통 조회": "IO_READ",
    "세금계산서 공통 조회": "IO_READ",

    # 검증
    "입고↔세금계산서 검증": "IO_READ",
    "출고↔세금계산서 검증": "IO_READ",
    "입고↔거래명세서 검증": "IO_READ",
    "출고↔거래명세서 검증": "IO_READ",

    # 분석/KPI
    "품목별 매출 추세 분석": "KPI_READ",
    "품목별 매출 추세 요약표": "KPI_READ",
    "제약사별 매출 추세 분석": "KPI_READ",
    "제약사별 매출 추세 분석 요약표": "KPI_READ",
    "품목별 매출 예상": "KPI_READ",
    "매출처별 매출 예상": "KPI_READ",
    "영업사원별 매출 예상": "KPI_READ",
    "지역별 매출 예상": "KPI_READ",
    "품목별 재고부족현황": "KPI_READ",
    "매입처별 재고부족 현황": "KPI_READ",
}


SPECIAL_PERMISSION_MAP: dict[str, str] = {
    "excel_download": "EXPORT_EXCEL",
    "csv_download": "EXPORT_EXCEL",
    "file_upload": "UPLOAD_FILE",
    "rag": "RAG_USE",
    "mcp_external_resource": "MCP_EXTERNAL_RESOURCE_READ",
    "chat_feedback_review": "CHAT_FEEDBACK_REVIEW",
    "machine_learning": "MACHINE_LEARNING_USE",
    "company_manage": "COMPANY_MANAGE",
    "user_approve": "USER_APPROVE",
    "user_manage_all": "USER_MANAGE_ALL",
    "user_manage_company": "USER_MANAGE_COMPANY",
}


def get_required_permission(
    *,
    category: str | None = None,
    action: str | None = None,
    special: str | None = None,
) -> str | None:
    """
    category/action/special 기준으로 필요한 권한 코드를 반환한다.

    우선순위:
    1. special
    2. action 정확 매칭
    3. category 정확 매칭
    4. category/action 부분 매칭
    """
    category_text = str(category or "").strip()
    action_text = str(action or "").strip()
    special_text = str(special or "").strip()

    if special_text:
        return SPECIAL_PERMISSION_MAP.get(special_text)

    if action_text in ACTION_PERMISSION_MAP:
        return ACTION_PERMISSION_MAP[action_text]

    if category_text in CATEGORY_PERMISSION_MAP:
        return CATEGORY_PERMISSION_MAP[category_text]

    # 부분 매칭 보강
    joined = f"{category_text} {action_text}"

    for key, permission in ACTION_PERMISSION_MAP.items():
        if key and key in joined:
            return permission

    for key, permission in CATEGORY_PERMISSION_MAP.items():
        if key and key in joined:
            return permission

    return None


def describe_permission(permission_code: str | None) -> str:
    labels = {
        "MASTER_READ": "마스터 조회",
        "IO_READ": "입출고/재고/문서 조회",
        "KPI_READ": "분석/KPI 조회",
        "EXPORT_EXCEL": "엑셀/CSV 다운로드",
        "UPLOAD_FILE": "파일 업로드",
        "RAG_USE": "RAG 사용",
        "MCP_EXTERNAL_RESOURCE_READ": "MCP 외부 Resource 읽기",
        "CHAT_FEEDBACK_REVIEW": "채팅 반응 운영 검수",
        "MACHINE_LEARNING_USE": "Machine Learning 사용",
        "COMPANY_MANAGE": "회사 관리",
        "USER_APPROVE": "사용자 승인",
        "USER_MANAGE_ALL": "전체 사용자 관리",
        "USER_MANAGE_COMPANY": "회사 사용자 관리",
    }

    if not permission_code:
        return "권한 제한 없음"

    return labels.get(permission_code, permission_code)
