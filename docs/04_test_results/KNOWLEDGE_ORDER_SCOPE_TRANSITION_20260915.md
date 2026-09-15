# 발주 계산 공통 Knowledge 범위 전환 준비

- 기준일: 2026-09-15
- 코드 HEAD: e75fdc49872b03cc765207715d8bc118833cca9e
- 운영 상태: 사용자 확인 기준으로 양 서버 COMPANY7 v1 ACTIVE/APPROVED.
- 이번 실행: 문서·계획·파일 검사만 수행. 운영 corpus/ERP 접근·변경 없음.
- 이 보고서는 운영자 작업 기록이며 RAG 등록 대상이 아니다.

> 후속 완료: GLOBAL v2는 양 서버 ACTIVE/APPROVED이며 기존 COMPANY7 v1은
> 양 서버 RETIRED다. 회사7 Closeout COMPANY7 v1은 ACTIVE/APPROVED로 유지한다.
> 아래 실행 명령은 전환 전 준비 기록이므로 다시 실행하지 않는다.

## 실제 기능 범위와 결론

`erp_table_feature_registry.py`의 발주 계산 feature는 IO_READ를 요구하며 회사별
허용 목록을 두지 않는다. `nlq_router.py`의 계산 라우팅과
`order_calculation_view.py` 역시 회사7 제한 없이 선택 회사 context를 사용한다.
`order_calculation_service.py`는 현재 선택 회사와 요청 회사의 일치 여부를 검사하고,
해당 회사 profile/Snapshot/원천을 사용한다. 따라서 기능은 공통이다.
활성 회사와 사용자 접근권한, profile·운영 Snapshot 준비 여부는 별도 조건이다.
코드로 특정 회사 번호의 성공 목록을 확정할 수 없으며 이번에 DB 전수조회는 하지 않았다.
회사7 외 실제 검증 완료를 일괄 주장하지 않는다.

권장 등록은 GLOBAL / company_id=null / ERP_DB_INTERNAL이다.
Knowledge GLOBAL은 공통 설명의 열람 범위이지 회사별 ERP 계산자료를 공유하는 기능이 아니다.
RAG_USE·KNOWLEDGE_ERP_DB_READ·기술상세 모드 제한과 계산 결과 회사격리는 유지한다.

## 문서 분리

발주 계약 기존 13절에 회사7 제품 64063/83315/23976의 실제 수량·가격·검증 근거가 있었다.
그 내용은 기존 회사7 Closeout에 이미 보존되어 있어 중복 복사하지 않고 공통 계약에서 제거했다.
NLQ 예시의 실제 제조사·제품코드도 매개변수형 예시로 바꾸었다.
본문의 수량 산식 예는 일반 산술 설명이며 회사별 실측 근거로 사용하지 않는다.
공통 문서 v1.5에는 회사7 실제 제품·가격·건수·배포 완료 사례를 포함하지 않는다.
Closeout 원문 및 계획은 그대로 COMPANY7 v1로 유지한다.

## 식별키와 버전

| 대상 | 준비한 정의 | 처리 |
|---|---|---|
| 공통 발주 계약 | document:order-calculation-phase1-business-contract, GLOBAL/null, v2 | 같은 키 유지, 신규 문서 ID 생성 |
| 기존 발주 계약 | 같은 키, COMPANY/7, v1 | 신규 검증 완료 후 정확한 기존 ID로 RETIRED |
| 회사7 Closeout | document:order-calculation-phase1-closeout-20260914, COMPANY/7, v1 | 유지, 재등록·퇴역 금지 |

저장소의 동일 논리 문서 판정 및 자동 SUPERSEDED 처리는 source_key뿐 아니라
scope/company_id/user_id도 비교한다 (`knowledge_document_service.py`).
따라서 GLOBAL v2 승인만으로 COMPANY7 v1은 자동 SUPERSEDED되지 않는다.
버전 2는 공통 문서의 개정 이력을 이어가기 위한 제안이며 cross-scope 자동 교체 번호가 아니다.
다른 키를 만들면 공통 계약의 중복 식별과 검색 충돌이 늘어 동일 키 유지가 적절하다.
동일 GLOBAL 키/버전이 먼저 등록되어 있으면 실행을 중단하고 버전 전략을 재검토한다.
기존 v1 내용을 덮어쓰거나 scope를 manifest에서 직접 수정하지 않는다.

## 승인 후 서버별 절차

1. 원문·계획 복사와 hash·validate 확인. 사용자/권한/실제 corpus 경로 및 기존 ID/버전 재확인.
2. 해당 서버 corpus를 기존 승인된 백업 절차로 보존. 이번 작업은 백업도 실행하지 않았다.
3. GLOBAL v2를 apply하고 readback으로 키·범위·분류·hash·ACTIVE/APPROVED를 확인.
4. 회사7 및 다른 허용 회사의 기술상세 검색에서 공통 계약을 확인하고 일반 사용자 차단을 확인.
   잠시 v1/v2가 함께 ACTIVE이므로 이 구간에 회사7 중복 인용을 완료 상태로 보지 않는다.
5. COMPANY7 발주 계약 v1의 정확한 ID로 retire 미리보기 후 별도 승인하에 retire --apply.
6. v1 RETIRED, v2 ACTIVE/APPROVED, Closeout COMPANY7 v1 ACTIVE/APPROVED를 readback.
   회사7 Closeout의 다른 회사 검색/인용 차단도 확인한다.
7. 다음 서버에서 같은 절차를 수행한다. 두 서버 ID가 같다고 가정하지 않는다.

권장 순서는 1호기 완료 후 2호기다. retire를 먼저 하면 회사7 공통 설명이 일시적으로
사라질 수 있어 권장하지 않는다. v2 실패 시 v1을 그대로 두고 중단한다.
retire 실패 시 이중 ACTIVE 상태를 보고하고 같은 키로 무작정 재등록하지 않는다.

### 실행 명령 준비 (아직 실행하지 않음)

각 서버에서 프로젝트 위치를 맞춘 뒤 해당 root 한 개만 설정한다.

```powershell
Set-Location 'C:\New\Python_Project\LmStudion_project1'
# 1호기
$root = 'C:\SSAI_TEST_DATA\knowledge_poc'
# 2호기에서는 위 값 대신 다음 값 사용
# $root = 'D:\SSAI_DATA\knowledge_poc'
$plan = 'docs/02_design/ORDER_CALCULATION_PHASE1_BUSINESS_CONTRACT.knowledge.json'
$key = 'document:order-calculation-phase1-business-contract'
$rows = @((Get-Content -LiteralPath (Join-Path $root 'manifest.json') -Raw | ConvertFrom-Json).documents)
$old = @($rows | Where-Object { $_.source_key -eq $key -and $_.scope -eq 'COMPANY' -and $_.company_id -eq 7 -and $_.version -eq 1 -and $_.status -eq 'ACTIVE' -and $_.approval_status -eq 'APPROVED' })
if ($old.Count -ne 1) { throw '기존 회사7 v1을 유일하게 확인할 수 없습니다.' }
if (@($rows | Where-Object { $_.source_key -eq $key -and $_.scope -eq 'GLOBAL' }).Count -ne 0) { throw 'GLOBAL 등록 이력이 있습니다. 재검토가 필요합니다.' }
$oldId = $old[0].document_id
& .\venv\Scripts\python.exe tools/knowledge_document_manage_cli.py validate --plan $plan
if ($LASTEXITCODE -ne 0) { throw '계획 검증 실패' }
```

백업·신규 등록 승인을 받은 뒤에만 다음 단계:

```powershell
& .\venv\Scripts\python.exe tools/knowledge_document_manage_cli.py apply --plan $plan --manifest-root $root --actor-user-id 1 --selected-company-id 7
if ($LASTEXITCODE -ne 0) { throw 'GLOBAL v2 등록 실패. 기존 v1을 유지합니다.' }
```

readback/search/hash·권한 검증 후 기존 ID 재확인 및 퇴역 미리보기:

```powershell
& .\venv\Scripts\python.exe tools/knowledge_document_manage_cli.py retire --manifest-root $root --document-id $oldId --version 1 --actor-user-id 1 --selected-company-id 7
if ($LASTEXITCODE -ne 0) { throw '기존 v1 퇴역 미리보기 실패' }
```

퇴역 승인을 별도로 받은 뒤에만 같은 명령에 --apply를 추가한다.

```powershell
& .\venv\Scripts\python.exe tools/knowledge_document_manage_cli.py retire --manifest-root $root --document-id $oldId --version 1 --actor-user-id 1 --selected-company-id 7 --apply
if ($LASTEXITCODE -ne 0) { throw '기존 v1 퇴역 실패. 상태를 확인합니다.' }
```

actor 1/company 7은 앞서 확인한 실행 context이며 서버별 실효권한을 재확인해야 한다.
GLOBAL 관리권한과 ERP 기술 읽기권한, 기존 회사 문서 퇴역용 회사 관리권한이 모두 필요하다.
GLOBAL v2의 document company_id는 null이고 selected-company-id 7은 권한 실행 context다.

## 2호기 복사 파일

반드시 함께 복사:

- docs/02_design/ORDER_CALCULATION_PHASE1_BUSINESS_CONTRACT.md (공통 v1.5, 변경됨)
- docs/02_design/ORDER_CALCULATION_PHASE1_BUSINESS_CONTRACT.knowledge.json (GLOBAL v2, 변경됨)

운영 안내·검사 동기화용, 등록 자체에는 불필요:

- docs/README.md
- docs/03_runbook/RUNBOOK_SIMSAI.md
- docs/04_test_results/KNOWLEDGE_WAVE4_PLAN_REVIEW_20260914.md (과거 명령 재실행 방지 정정)
- docs/04_test_results/KNOWLEDGE_ORDER_SCOPE_TRANSITION_20260915.md
- tools/check_order_documentation_closeout.py
- tools/check_knowledge_wave4_plans.py

Closeout 원문/계획·기간정책 원문/계획은 변경하지 않았으므로 복사 불필요하다.
운영 manifest/artifact와 Snapshot 코드 변경 파일은 복사 대상이 아니다.

## 파일 검증 결과

- GLOBAL 공통 계약과 COMPANY7 Closeout 격리·권한 fixture: 69/69 PASS
- 공식 문서·계획·링크: 9개 문서, 51개 링크, 5개 후보 PASS
- 공통 계획 CLI validate: PASS, write_count=0
- 새 공통 source/content SHA-256: 8411e011727095864c514a96b6d3e929273f43c610873dc5a10c06b7328e1bb5
- 실제 apply/retire·manifest/artifact write: 0

현재 기능의 계산 코드·가격·입고예정·source budget은 변경하지 않았다.
운영 전환 완료가 아니라 실행 승인을 위한 준비 완료 상태다.
