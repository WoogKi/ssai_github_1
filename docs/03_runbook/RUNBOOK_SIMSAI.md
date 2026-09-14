---
title: "SSAI 공통 운영 Runbook"
date: "2026-08-09"
version: "v1.0"
status: "official"
baseline_branch: "feat/dashboard-stock-extension-20260727"
baseline_commit: "6c83962bc1b079fe440d56a313de536cf9490651"
---

# SSAI 공통 운영 Runbook

## 1. 목적과 적용 범위

이 문서는 1호기 개발, 자동 회귀, pre-commit 감사, 선택적 commit/push,
2호기 fast-forward 적용, Streamlit 재시작과 smoke까지의 공식 운영 순서를 정한다.

| 구분 | 공식 기준 |
|---|---|
| 1호기 프로젝트 | `C:\New\Python_Project\LmStudion_project1` |
| 1호기 Python | `venv` |
| 2호기 프로젝트 | `C:\New\Python_Project\LmStudion_project1` |
| 2호기 Python | `.venv` |
| 공식 브랜치 | `feat/dashboard-stock-extension-20260727` |
| 현재 안정 기준 | `6c83962` |

2호기 프로세스 점검과 안전한 재기동은
[2호기 운영 점검 Runbook](RUNBOOK_2HO_OPERATION_CHECK.md)을 함께 따른다.

## 2. 변경 전 확인

```powershell
cd C:\New\Python_Project\LmStudion_project1
git status --short --branch
git rev-parse HEAD
git branch --show-current
```

확인 기준:

- 브랜치와 작업 목적이 일치한다.
- 기존 unrelated 변경과 untracked 파일을 기록하고 보존한다.
- `.env`, 로그, 업로드, 다운로드, 고객 데이터와 DB 연결정보를 diff에 넣지 않는다.
- 작업 범위 밖 파일을 reset, restore, checkout 또는 stash로 정리하지 않는다.

## 3. 1호기 검증

### 3.1 Python 문법 검사

프로젝트 필수 파일과 이번 수정 Python 파일을 함께 검사한다.

```powershell
.\venv\Scripts\python.exe -m py_compile `
  app\Lmstudio_SSAI_chat_main.py `
  app\ui\sims_panel.py `
  app\ui\chat_middleware.py `
  app\services\rddbc060_service.py `
  app\services\ssai_auth_service.py `
  app\services\ssai_storage_service.py `
  app\db\mssql_client.py
```

### 3.2 회귀 순서

```powershell
.\venv\Scripts\python.exe tools\check_io_nlq_regression.py
.\venv\Scripts\python.exe tools\check_analytics_regression.py
.\venv\Scripts\python.exe tools\check_nlq_action_inventory.py
.\venv\Scripts\python.exe -m pip check
git diff --check
```

현재 알려진 허용 실패는 Analytics의
`sales_chart_target_markers_missing` 1건뿐이다. 그 외 실패를 이 항목으로 묶어
합격 처리하지 않는다.

### 3.3 pre-commit 감사

```powershell
git status --short --branch
git diff --stat
git diff --check
git diff --name-only
```

감사 항목:

- 변경 파일이 요청 범위와 일치한다.
- 신규 SQL, fallback, 임시 debug 코드와 fixture 전용 값이 운영 코드에 없다.
- 회사 격리와 current-table 원본 경계가 유지된다.
- 허용 실패 외 회귀 실패가 없다.
- 실제 수동 테스트와 로그에서 신규 `ERROR`, `Traceback`, 예상 밖 `WARNING`이 없다.

## 4. Git 선택 stage와 배포 준비

`git add .`과 `git add -A`는 사용하지 않는다. 검토가 끝난 대상만 경로를
명시하여 stage한다.

```powershell
git add -- <검토가_끝난_파일1> <검토가_끝난_파일2>
git diff --cached --name-only
git diff --cached --stat
git diff --cached --check
```

기존 unrelated untracked 파일, 로그, export와 로컬 진단자료가 staged 목록에
있으면 commit하지 않는다. commit 후에는 다음을 확인한다.

```powershell
git status --short --branch
git log -1 --oneline
git push origin feat/dashboard-stock-extension-20260727
```

push는 1호기에서 승인된 브랜치에만 수행한다.

## 5. 2호기 적용

2호기는 운영 작업트리에서 임의 commit 또는 push하지 않는다.

```powershell
cd C:\New\Python_Project\LmStudion_project1
git status --short --branch
git fetch origin
git pull --ff-only origin feat/dashboard-stock-extension-20260727
git rev-parse HEAD
```

적용 기준:

- pull 전 로컬 변경이 없어야 한다.
- `--ff-only`가 실패하면 merge나 rebase로 우회하지 않는다.
- 1호기 push commit과 2호기 `HEAD`가 같은지 확인한다.
- 전체 Streamlit process tree를 안전하게 재기동한다.
- 단순 Health 성공만으로 새 코드 반영을 확정하지 않는다.

## 6. 배포 후 Health와 smoke

2호기 재기동 후 다음 순서로 확인한다.

1. ScheduledTask 상태와 실제 process tree 확인
2. 포트 8501 Listen PID 1개 확인
3. `http://127.0.0.1:8501/_stcore/health`의 HTTP 200 및 `ok` 확인
4. 실제 OwningProcess 시작시각이 이번 재기동과 일치하는지 확인
5. `app.log`와 Streamlit 서버 로그의 신규 `ERROR`/`Traceback` 확인
6. 로그인·회사 선택·채팅방 격리 확인
7. 최소 smoke 실행

최소 smoke:

- `제품재고장 제조사 한미`
- `제품재고장 한미`
- `현재고 바이엘`
- `SIMS 일일점검`

smoke에서는 결과 상태, 조건 표시, 행 구조, 응답시간, 채팅 저장 1회와
현재표 후속질문 가능 여부를 함께 본다.

## 7. 배포 완료 기준

- 1호기와 2호기의 commit이 같다.
- 2호기 8501 Listen PID가 하나다.
- Health가 HTTP 200/`ok`다.
- 최소 smoke가 기존 계약과 일치한다.
- 신규 `ERROR`, `Traceback`과 설명되지 않은 `WARNING`이 없다.
- 롤백이 필요하면 승인된 이전 commit으로 별도 절차를 수립한다.

## 8. 현재 알려진 후속 과제

- `제품재고장 한미` 무라벨 통합검색 약 60초 성능
- 무라벨 제품재고현황 `nlq.trace.parsed search_fields` 정합성
- 2호기 ScheduledTask 하위 Streamlit child-process lifecycle

후속 과제를 이유로 결과 범위를 줄이거나 의미를 바꿔 smoke를 통과시키지 않는다.

## 9. 발주 1차 Knowledge 등록 준비

2026-09-14, 기준 commit `7283865dcf8709ec9673404b0321adfe208c65be`.
발주 계산/NLQ는 배포 완료됐다. 본 절은 등록 계획이며 운영 corpus 등록 완료를 뜻하지 않는다.

### 공식 authority와 후보 순서

| 순서 | 문서 | 내용/domain | scope·분류 | 등록 식별 source_key |
|---|---|---|---|---|
| 1 | [업무질문 사용 예시](SIMS_AI_업무질문_사용_예시.md) | 업무 안내: 발주/계약단가/제품정보/입고예정/적용처/영업일 | GLOBAL·GENERAL | document:sims-ai-business-question-examples |
| 2 | [발주 업무 계약](../02_design/ORDER_CALCULATION_PHASE1_BUSINESS_CONTRACT.md) | 발주 계산의 상세 업무·기술 계약 및 회사7 사례 | COMPANY(7)·ERP_DB_INTERNAL | document:order-calculation-phase1-business-contract |
| 3 | [NLQ·현재고·현재표 계약](../02_design/SIMS_NLQ_CURRENT_STOCK_CURRENT_TABLE_CONTRACT.md) | 제품/재고·회사격리·full/current-table | GLOBAL·ERP_DB_INTERNAL | document:sims-nlq-stock-current-table-contract |
| 4 | [NLQ 기간정책](../02_design/SIMS_AI_NLQ_기간정책_공식기준.md) | 일반 조회기간과 발주 계약단가 기준일의 경계 | GLOBAL·ERP_DB_INTERNAL | document:sims-ai-nlq-period-contract |
| 5 | [발주 1차 Closeout](../04_test_results/ORDER_CALCULATION_PHASE1_CLOSEOUT_20260914.md) | 회사7/배포일/가격 수정·성능·관찰 기록 | COMPANY(7)·ERP_DB_INTERNAL | document:order-calculation-phase1-closeout-20260914 |

계약단가·입고예정·발주조회·적용처의 사용자 authority는 1번 문서의 해당 절이고,
기술 authority는 2번의 제1/4/6/9/12절이다. 제품정보·Snapshot/profile는 2·3번을 연결한다.
별도 동일 목적 문서를 만들지 않는다. 회사7 실제 가격은 GENERAL/GLOBAL 문서에 넣지 않는다.
category/domain은 등록 검토용 분류이며 현재 CLI의 독립 저장 필드가 아니다.
title은 `source_name`, 문서의 안정 ID는 `source_key`다. 실제 `document_id`는 등록 시 UUID로 생성된다.

### 권한과 저장 위치

- 읽기는 selected company의 effective `RAG_USE`를 전제로 한다.
- GENERAL 업무 안내는 일반 승인 Knowledge 경로다. ERP_DB_INTERNAL은 technical-detail 경로와
  `KNOWLEDGE_ERP_DB_READ`를 함께 요구하며 선언 역할 기준 SYSTEM_ADMIN/SSART_MANAGER에 한정한다.
- GLOBAL 관리 권한은 `KNOWLEDGE_GLOBAL_MANAGE`, COMPANY 관리 권한은 해당 회사의
  `KNOWLEDGE_COMPANY_MANAGE`다. 관리 권한만으로 기술 읽기 권한이 생기지 않는다.
- 선언 역할이 아니라 실제 사용자·회사의 effective permission으로 최종 인가한다.
- 저장 경로 authority는 `KnowledgeDocumentRepository`의 `get_storage_root()/knowledge_poc`다.
  `<SSAI_STORAGE_ROOT>/knowledge_poc/manifest.json`과 `artifacts/`를 함께 관리한다.
- 기존 운영 절차의 1호기 검증 위치는 `C:\SSAI_TEST_DATA\knowledge_poc`, 2호기는
  `D:\SSAI_DATA\knowledge_poc`다. 이번에는 환경값/운영 manifest를 읽거나 변경하지 않았다.
  등록 승인 전에 실제 프로세스 설정 경로와 기존 문서/version을 읽기 전용으로 확인해야 한다.

### 승인 전 준비와 승인 후 순서

1. [등록 후보 JSON](ORDER_CALCULATION_PHASE1.knowledge.json)을 파일 검증한다.
   `knowledge_document_manage_cli.py validate --plan ...`은 파일만 검사하며 DB/manifest write를 하지 않는다.
2. 운영자에게 source_key/title, 내용, 회사 scope, 분류, 등록 순서와 적용할 기존 version을 제시한다.
   기존 일반 안내 문서는 v3 후보이고 기술 문서는 v1 후보다. 운영 manifest에서 이미 쓰는 버전과
   충돌하면 승인 전에 버전을 재결정한다. 이번에는 기존 v2 계획을 덮어쓰지 않았다.
3. 승인 후에만 `apply`를 수행한다. CLI는 **실행당 DOCUMENT 1개만** 허용하므로
   후보 JSON을 단일 item 계획으로 나누고 위 순서대로 등록·승인한다.
4. `apply --plan <단일계획> --manifest-root <확인경로> --actor-user-id <승인관리자> --selected-company-id 7`은
   실제 권한 DB 조회 및 corpus write를 수행한다. 승인 전 실행 금지다.
5. 등록 후 document_id/version/hash와 ACTIVE/APPROVED 상태를 readback하고 이전 같은 source_key
   version의 SUPERSEDED 상태를 확인한다. DOCUMENT 등록을 Git 배포나 자동 freshness 갱신으로 오인하지 않는다.
6. 일반 업무 질문과 기술 질문을 분리해 조회/no-leak/company-isolation Smoke를 수행한다.
   로컬 파일 간 링크는 자동 연쇄 retrieval이 아니므로 dependency 문서도 독립 source_key로 등록·검증한다.
7. 2호기 corpus 배포는 [2호기 운영 절차](RUNBOOK_2HO_OPERATION_CHECK.md)를 따라 승인 후 manifest와
   참조 artifact를 함께 백업/배포/readback한다. 승인 없이 운영 파일을 복사하지 않는다.

현재 archive의 `nlq_docs_index.md`와 `master_nlq_summary.md`는 과거 색인으로 보존하며
현행 색인은 [docs README](../README.md)를 따른다.
Codex status/diff, 원시 로그, probe, 중간 성능 조사는 corpus에 넣지 않는다.
업무 예시 생성 도구로 기존 사용자 문서의 이번 확정 추가 절을 덮어쓰지 않는다.
