# Knowledge Wave 4 등록 계획 최종 점검

> 2026-09-15 후속 정정: 이 기록의 발주 계약 COMPANY7 v1 제안은 공통 기능 범위와
> 회사7 사례가 섞인 당시 분류다. 현재 전용 계획은 사례를 제외한 GLOBAL v2이며,
> 전환에는 별도 v1 퇴역이 필요하다. 아래 당시 등록 명령을 다시 실행하지 말고
> [범위 전환 검토](KNOWLEDGE_ORDER_SCOPE_TRANSITION_20260915.md)를 따른다.

- 기준일: 2026-09-14
- 점검 HEAD: e75fdc49872b03cc765207715d8bc118833cca9e
- 실제 등록·retire·운영 manifest/artifact·ERP 접근·Snapshot 변경: 없음

## 1호기 → 2호기 수동 복사 필요 파일

등록에 필요한 새 계획:

- docs/02_design/ORDER_CALCULATION_PHASE1_BUSINESS_CONTRACT.knowledge.json
- docs/02_design/SIMS_AI_NLQ_기간정책_공식기준.knowledge.json
- docs/04_test_results/ORDER_CALCULATION_PHASE1_CLOSEOUT_20260914.knowledge.json

대응 원문:

- docs/02_design/ORDER_CALCULATION_PHASE1_BUSINESS_CONTRACT.md
- docs/02_design/SIMS_AI_NLQ_기간정책_공식기준.md
- docs/04_test_results/ORDER_CALCULATION_PHASE1_CLOSEOUT_20260914.md

세 원문은 1호기 현재 HEAD와 내용이 일치하며 이번에 변경하지 않았다.
2호기도 같은 HEAD의 동일 원문을 유지하면 원문 복사는 불필요하다.
2호기의 현재 HEAD/작업 파일은 이번에 원격 확인하지 않았으므로 다르면 원문도 함께 맞춘다.
원문과 계획의 상대 위치는 그대로 유지한다.

등록 실행에는 불필요하지만 운영 안내·검사 일치를 위해 별도 동기화할 파일:

- docs/03_runbook/ORDER_CALCULATION_PHASE1.knowledge.json (단일 계획 참조 색인)
- docs/03_runbook/RUNBOOK_SIMSAI.md (색인 직접 검증/등록 방지 안내)
- tools/check_order_documentation_closeout.py (분리된 계획 참조 검사)
- tools/check_knowledge_wave4_plans.py (신규 검사)
- docs/04_test_results/KNOWLEDGE_WAVE4_PLAN_REVIEW_20260914.md (이 보고서, RAG 제외)

README는 이번 작업에서 변경하지 않았다. 운영 corpus 파일 복사는 이 작업 범위가 아니다.

## 등록 단위

| 문서 | source_key | version | scope | company_id | classification | 별칭 수 |
|---|---|---:|---|---|---|---:|
| 발주 계산 계약 | document:order-calculation-phase1-business-contract | 1 | COMPANY | 7 | ERP_DB_INTERNAL | 5 |
| NLQ 기간정책 | document:sims-ai-nlq-period-contract | 1 | GLOBAL | null | ERP_DB_INTERNAL | 2 |
| 회사7 마감 기록 | document:order-calculation-phase1-closeout-20260914 | 1 | COMPANY | 7 | ERP_DB_INTERNAL | 4 |

모두 DOCUMENT이며 user_id=null이다. source_name/content_file은 대응 원문의 실제 파일명이다.
기존 묶음 계획의 식별키와 별칭을 전부 유지했다. 신규 식별키를 만들거나 중복 정의하지 않았다.
관리 CLI의 apply는 정확히 한 DOCUMENT만 허용하므로 3개 정의를 전용 단일 계획으로 옮겼다.
기존 ORDER_CALCULATION_PHASE1.knowledge.json은 items가 없는 색인으로 바꾸었다.
색인의 external_registration_plans는 자동 연쇄 등록 기능이 아니다.
이 색인 파일을 validate/apply 대상으로 사용하지 않는다.

## 내용·책임 점검

- 발주 계약 v1.4는 배포된 1차 업무·기술 기준이며 회사7 사례 때문에 COMPANY 7로 제한한다.
  발주조회/계산 분리, 안전/적정/마감일, 월 예상수량 우선, 현재고·유효 잔량 차감,
  대표매입처, 적용처 50002/50001, 단가 우선순위, 단위환산, 실제수량 편집,
  generation 2-statement/Dashboard 3-call 및 발주 별도 source 예산을 보존한다.
- 기간정책은 GLOBAL 공통 기준이다. date는 거래 조회범위, month는 지정 월 범위,
  as_of는 기준일 유효성이라는 업무별 경계를 유지한다. 일반 날짜는 달력 기간이며
  발주 수요와 유효 입고예정은 공식 Calendar의 영업일이다. 현재월·과거월의 명시 월은
  해당 월 전체이며, 기간을 생략한 제품재고장 등 별도 기본값은 현재 서비스 계약을 따른다.
  발주 서비스의 build_contract_price_params가 일반 date_from/date_to/month_from/month_to를
  제외하고 발주일 as_of를 전달하는 것을 확인했다. R070 이력 자체의 시작일 필터는 유지한다.
- 마감 기록은 회사7 당시 실제 검증과 사용자 운영 확인 기록이다. 당시 처리시간·source 수를
  고정 예산이나 현재 단가표로 해석하지 않는다. 64063=2,798.19, 83315=12,233.46,
  23976=9,200 및 추천/실제 초기값 10/10, 7/7, 100/100 기록이 있고 폐기된 3,240은 없다.
- Wave 1 업무 설명, Wave 2 현재표/Dashboard/공통조건, Wave 3 마스터·가격/제품재고장/
  Snapshot은 공통 기준이다. 이번 발주 문서는 발주 계산 통합 계약·회사7 운영 확인에 한정한다.
  역할 중첩은 의존 관계이지 동일 source_key 중복이 아니다.

## 해시

원문 바이트와 validate가 읽은 UTF-8 본문 SHA-256은 각 문서에서 동일하다.

| 문서 | source/content SHA-256 |
|---|---|
| 발주 계산 계약 | 3d9d340c25bb35482cce3773e5bdbb1cc750fa1cc5742caf3ec5943bfe464de0 |
| 기간정책 | 405de44261849476041c7268e441ab60359d42d407acb50d7511c5e21627a7ce |
| 회사7 마감 기록 | 05f113ab80c0bcf0c3f5670fea3e3c0f9d6a9264bc288c2df7462b2005f51529 |

Windows 줄바꿈 변환이 있으면 파일 바이트 hash는 달라질 수 있으므로 복사 후 해당 서버의
validate와 원문 hash를 다시 확인한다. 의미가 다른 원문을 동일 버전으로 등록하지 않는다.

## 검증 결과

| 검사 | 결과 |
|---|---|
| 관리 CLI validate 전용 3개 계획 | 3/3 PASS, write_count=0 |
| 회사7/다른 회사·기술 모드·역할·필수권한 fixture | 69/69 PASS |
| 기존 권한 회귀 | 49/49 PASS |
| 공식 문서 마감 검사 | 9개 문서·51개 링크·5개 후보 PASS |
| Wave 1~3 비보관 계획과 source_key 충돌 | 없음 |
| py_compile 필수 7개 파일과 검사 도구 2개 | PASS |
| git diff --check | PASS (기존 LF/CRLF 주의 출력) |

SYSTEM_ADMIN/SSART_MANAGER도 RAG_USE·KNOWLEDGE_ERP_DB_READ·기술상세 모드 및
회사 일치를 요구한다. SSART_STAFF/WHOLESALE_MANAGER/WHOLESALE_STAFF는 기술 문서
열람이 차단됨을 실제 정책 함수로 확인했다. 관리자도 회사7 문서를 다른 회사에서 읽지 못한다.
GLOBAL 기간정책의 회사7 문서 링크는 대상 문서 권한을 낮추거나 자동 인용을 허용하지 않는다.
운영 corpus 최신 상태는 이번에 조회하지 않았다. 등록 직전 같은 키/버전 존재 여부와
actor 권한·저장 경로를 다시 확인해야 한다.

## 승인 후 실행할 명령

아래 명령은 준비만 했으며 실행하지 않았다. 순서는 공통 기간정책→발주 계약→회사7 기록이다.
두 서버의 프로젝트 경로는 기존 2호기 Runbook 기준으로 같다.
actor 1/selected company 7은 앞서 확인한 실행 context이며 등록 시 CLI가 권한을 재확인한다.
문서 company_id는 계획 정의대로 유지한다. 2호기의 actor 1이 동일 관리자인지도 실행 전에 확인한다.

### 1호기

```powershell
Set-Location 'C:\New\Python_Project\LmStudion_project1'
$plans = @(
  'docs/02_design/SIMS_AI_NLQ_기간정책_공식기준.knowledge.json',
  'docs/02_design/ORDER_CALCULATION_PHASE1_BUSINESS_CONTRACT.knowledge.json',
  'docs/04_test_results/ORDER_CALCULATION_PHASE1_CLOSEOUT_20260914.knowledge.json'
)
foreach ($plan in $plans) {
  & .\venv\Scripts\python.exe tools/knowledge_document_manage_cli.py apply --plan $plan --manifest-root 'C:\SSAI_TEST_DATA\knowledge_poc' --actor-user-id 1 --selected-company-id 7
  if ($LASTEXITCODE -ne 0) { throw "등록 실패: $plan. 자동 재시도하지 않습니다." }
}
```

### 2호기

```powershell
Set-Location 'C:\New\Python_Project\LmStudion_project1'
$plans = @(
  'docs/02_design/SIMS_AI_NLQ_기간정책_공식기준.knowledge.json',
  'docs/02_design/ORDER_CALCULATION_PHASE1_BUSINESS_CONTRACT.knowledge.json',
  'docs/04_test_results/ORDER_CALCULATION_PHASE1_CLOSEOUT_20260914.knowledge.json'
)
foreach ($plan in $plans) {
  & .\venv\Scripts\python.exe tools/knowledge_document_manage_cli.py apply --plan $plan --manifest-root 'D:\SSAI_DATA\knowledge_poc' --actor-user-id 1 --selected-company-id 7
  if ($LASTEXITCODE -ne 0) { throw "등록 실패: $plan. 자동 재시도하지 않습니다." }
}
```

각 등록 후 readback/status/hash와 회사7 기술 검색, 다른 회사 차단, 일반 사용자 차단을
검증한다. 중간 실패 시 이미 등록된 문서를 임의 삭제·재등록하지 않고 상태를 먼저 확인한다.
