# Knowledge Wave 5 운영·권한 문서 등록 준비

- 기준일: 2026-09-15
- 점검 HEAD: e75fdc49872b03cc765207715d8bc118833cca9e 및 이번 문서 보강분
- 운영 RAG apply/retire·manifest/artifact 변경·ERP 접근·Snapshot 변경·Git 등록/커밋/푸시: 없음
- 이 보고서는 검사 기록이며 운영 RAG 등록 대상이 아니다.

## 1호기 → 2호기 수동 복사 필요 파일

### 실제 등록에 필수 (원문과 계획을 함께 복사)

- docs/03_runbook/RUNBOOK_SIMSAI.md (현행 원문 수정)
- docs/03_runbook/RUNBOOK_SIMSAI.knowledge.json (신규)
- docs/03_runbook/RUNBOOK_2HO_OPERATION_CHECK.md (현행 원문 수정)
- docs/03_runbook/RUNBOOK_2HO_OPERATION_CHECK.knowledge.json (신규)
- docs/02_design/KNOWLEDGE_ACCESS_POLICY_CONTRACT.md (신규)
- docs/02_design/KNOWLEDGE_ACCESS_POLICY_CONTRACT.knowledge.json (신규)

Git 제외 *.knowledge.json은 수동 동기화가 필요하다. 원문도 이번에 수정/생성했으므로
현재 2호기 Git HEAD가 같다는 이유만으로 복사를 생략할 수 없다. 상대경로는 유지한다.

### 검증·보고용

- tools/check_knowledge_wave5_plans.py
- docs/04_test_results/KNOWLEDGE_WAVE5_PLAN_REVIEW_20260915.md

검증 도구는 2호기 파일 검증용이다. 보고서는 등록에 불필요하며 RAG 등록 금지.

### Git으로 나중에 동기화해도 되는 파일

- docs/README.md (공식 권한 문서 링크)

원문·검사 도구도 추후 승인된 Git 배포로 동기화할 수 있지만 등록 시점에는 동일 내용이
필요하다. 이번에는 stage/commit/push를 수행하지 않았다. 운영 corpus는 파일 복사 대상이 아니다.

## 공식 기준 판정

| 구분 | 대상 | 판정 |
|---|---|---|
| A 현재 공통 운영 | RUNBOOK_SIMSAI.md | 현행 보강, 등록 |
| B 1·2호기 운영/배포 확인 절차 | RUNBOOK_2HO_OPERATION_CHECK.md | 현행 보강, 등록 |
| C Knowledge 권한 | KNOWLEDGE_ACCESS_POLICY_CONTRACT.md | 독립 기준 신규, 등록 |
| D 과거 배포·마감 | 20260903_Knowledge_RAG_권한_도움말_마감.md 및 과거 Phase 보고 | 당시 기록, 등록 제외 |
| E 테스트·진단 | 과거 Smoke/테스트 결과와 Wave 조사 보고 | 현행 절차 대체 아님, 등록 제외 |
| F 구버전·중복 | legacy summary/index, 과거 CURRENT/Project Source revision | 현행 authority로 복원 금지 |

회사7 Closeout은 Wave 4의 회사 제한 공식 기록으로 유지하되 Wave 5 현행 운영 권한
authority로 승격하거나 재등록하지 않는다. 신규 권한 문서는 기존 Runbook의 권한 절과
같은 정책을 자세히 정의한다. Runbook은 실행 절차·링크 중심으로 유지한다.
계산·가격·제품·Snapshot 업무 계약을 이 세 문서에 복제하지 않는다.

## 등록 정의

모두 DOCUMENT / version=1 / GLOBAL / company_id=null / user_id=null / ERP_DB_INTERNAL.

| 순서 | 문서 | source_key | 별칭 수 |
|---|---|---|---:|
| 1 | 권한 계약 | document:knowledge-access-policy-contract | 7 |
| 2 | 공통 Runbook | document:sims-ai-runbook | 8 |
| 3 | 2호기 Runbook | document:sims-ai-2ho-operation-runbook | 6 |

source_name/content_file은 대응 실제 .md 파일명이며 계획과 원문은 같은 폴더다.
로컬 manifest를 읽기 전용으로 확인한 결과 세 키와 대응 source_name 등록본은 없다.
Wave 1~4의 비보관 계획과도 키 충돌이 없다. 2호기 corpus는 이번에 원격 확인하지 않았다.
2호기에 같은 키가 있으면 신규 v1을 강행하지 않고 내용·범위·버전을 먼저 비교한다.
같은 내용의 기존 등록은 유지하고 변경이 있으면 새 version 승인을 요청한다.

## 실제 운영 구성·개정

| 항목 | 1호기 | 2호기 |
|---|---|---|
| repo | C:\New\Python_Project\LmStudion_project1 | C:\New\Python_Project\LmStudion_project1 |
| Python 환경 | venv | .venv |
| Knowledge | C:\SSAI_TEST_DATA\knowledge_poc | D:\SSAI_DATA\knowledge_poc |
| 작업 | 개발·검증 환경 | SIMS_AI_2HO_Streamlit |

경로는 서버 운영 정보이지 특정 ERP 회사의 자료가 아니다. 따라서 GLOBAL 기술 분류로
등록하며 실제 회사 가격·검증 결과는 넣지 않는다. 실행 전에 프로세스 환경변수/설정의
SSAI_STORAGE_ROOT와 실제 경로를 다시 확인한다. 비밀값은 출력하지 않는다.

Runbook에서 과거 6c83962를 영구 현재 안정 기준으로 표시하던 부분과 과거 Analytics
허용 실패를 현재 예외로 재사용하던 부분을 제거했다. 배포 완료는 실행 당시 승인 commit,
process/Health/log/회사·권한 Smoke로 판단한다. Git pull과 Knowledge 동기화는 별도다.
서버별 등록 후 key/version/scope/company/classification/content hash를 맞추며 UUID는
서버별로 다를 수 있다. 원문 hash와 저장 content_hash는 각 계산 의미를 구분한다.
전체 corpus 백업/복원은 별도 승인된 일관성 절차이고 직접 manifest 편집은 금지다.

## 실제 권한 코드·검증

- GENERAL DOCUMENT: RAG_USE 필요.
- ERP_DB_INTERNAL: RAG_USE + KNOWLEDGE_ERP_DB_READ + 기술상세 모드.
- PROJECT_SOURCE: 별도 KNOWLEDGE_PROJECT_SOURCE_READ + 기술상세 모드.
  사용자 지시의 PROJECT_SOURCE_READ는 설명상 약칭이며 실제 코드명은 앞의 이름이다.
- PROJECT_SOURCE가 ERP_DB_INTERNAL이면 두 기술 읽기 권한이 모두 필요하다.
- 기본 SYSTEM_ADMIN/SSART_MANAGER는 기술 열람 가능, STAFF/도매 일반 역할은 차단.
  실제 판정은 사용자·선택 회사의 실효권한이며 역할 이름만으로 인가하지 않는다.
- COMPANY/USER 문서는 회사·사용자 일치를 요구하며 관리자도 회사 범위를 우회하지 않는다.
- 권한 정책 함수는 ACTIVE를 검사하고 repository 검색·인용 경계는 APPROVED도 검사한다.
- 관리권한은 GLOBAL/COMPANY 각각 별도다. SSART_MANAGER의 기술 읽기 권한은 GLOBAL 관리
  권한을 의미하지 않으므로 글로벌 등록에는 승인된 SYSTEM_ADMIN actor를 사용한다.

| 검사 | 결과 |
|---|---|
| 세 계획 관리 CLI validate | 3/3 PASS, write_count=0 |
| Wave 5 역할·모드·필수권한·선택회사 fixture | 81/81 PASS |
| 기존 scope/회사격리/Project Source 권한 회귀 | 49/49 PASS |
| 기본 역할 정책 회귀 | 10/10 PASS, db_write_count=0 |
| 새 원문 링크 | 19개 PASS |
| 기존 공식 문서 검사 | 9개 문서·53개 링크·5개 후보 PASS |
| 필수 Python 7개와 신규 검사 py_compile | PASS |
| git diff --check | PASS (기존 LF/CRLF 주의만 출력) |

실제 로그인 권한 Smoke와 2호기 원문/등록 확인은 승인 이후 실행한다.

## source/content SHA-256

이번 파일에서는 원문 바이트 hash와 CLI가 읽은 UTF-8 본문 hash가 동일하다.

| 문서 | SHA-256 |
|---|---|
| 공통 Runbook | 2f62f08565509b36a4303d647a06f9fe9f1c86fed00f6a95e1de20aae8b0803c |
| 2호기 Runbook | 4caa6cb6207c0c8de5de5f7a1013c7a7d80628b489e70012436253023bd4ef65 |
| 권한 계약 | d5e0c68b52a441f9aa6e1da6dee4007255893123d4752d98c54d59580d8cff5f |

## PROJECT_SOURCE v9 권고

로컬에서 정확한 키 project-source:app/services/ssai_storage_service.py#get_user_file_path의
v9 ACTIVE/APPROVED를 확인했다. ID는 983d68ca-a6c4-4164-8568-84704e51f6a4이며
revision=daf1661f972ff9cfe7c234f329320eeb2a4debd9로 현재 HEAD와 다르다.
2호기 해당 기록은 이번에 확인하지 않았으며 ID가 같다고 가정하지 않는다.

현재 `_project_source_is_current`는 HEAD/revision 불일치와 파일 수정 등을 검사해
검색·도움말·재인용 경계에서 차단한다. ACTIVE는 현재 코드 근거라는 뜻이 아니다.
함수 본문이 같아도 revision이 다르면 갱신을 생략할 수 없다.

권고: 현재 근거로 사용할 이유가 없는 STALE v9는 별도 관리 작업으로 퇴역 승인을 요청한다.
이번 DOCUMENT Wave에 섞어 retire하거나 무조건 v10을 등록하지 않는다.
향후 실제 Project Source 질문 지원이 필요하면 최종 승인·배포 HEAD에서 별도 CLI로
재추출/검증 후 다음 version(현재 기록 기준 v10)을 승인한다. 같은 논리 범위의 새 버전
승인은 기존 버전을 자동 대체한다. 이미 퇴역된 v9도 역사 기록은 남는다.
지속되는 HEAD 변경마다 최신 소스가 STALE이 될 수 있어 코드 배포와 갱신을 함께 계획해야 한다.
DOCUMENT 계획으로 소스 revision만 바꾸거나 artifact를 수동 수정해서 복원하지 않는다.

## 승인 후 apply 명령 준비

아래는 실행하지 않은 명령이다. 등록 전 양 서버 키·버전·저장 경로·actor 실효권한을
재확인하고 문서별 validate를 수행한다. 각 apply 후 readback/검색을 통과한 뒤 다음 문서로
이동한다. 실패 시 자동 재시도하지 않고 이미 생성된 등록 상태를 먼저 확인한다.
actor 1/company 7은 앞서 확인한 실행 context이며 서버별 권한 확인이 필요하다.
문서 company_id는 GLOBAL/null을 유지한다.

### 1호기

```powershell
Set-Location 'C:\New\Python_Project\LmStudion_project1'
& .\venv\Scripts\python.exe tools/knowledge_document_manage_cli.py apply --plan docs/02_design/KNOWLEDGE_ACCESS_POLICY_CONTRACT.knowledge.json --manifest-root 'C:\SSAI_TEST_DATA\knowledge_poc' --actor-user-id 1 --selected-company-id 7
# readback/권한 검색 확인 후 다음 명령
& .\venv\Scripts\python.exe tools/knowledge_document_manage_cli.py apply --plan docs/03_runbook/RUNBOOK_SIMSAI.knowledge.json --manifest-root 'C:\SSAI_TEST_DATA\knowledge_poc' --actor-user-id 1 --selected-company-id 7
# readback/권한 검색 확인 후 다음 명령
& .\venv\Scripts\python.exe tools/knowledge_document_manage_cli.py apply --plan docs/03_runbook/RUNBOOK_2HO_OPERATION_CHECK.knowledge.json --manifest-root 'C:\SSAI_TEST_DATA\knowledge_poc' --actor-user-id 1 --selected-company-id 7
```

### 2호기 (.venv 사용)

```powershell
Set-Location 'C:\New\Python_Project\LmStudion_project1'
& .\.venv\Scripts\python.exe tools/knowledge_document_manage_cli.py apply --plan docs/02_design/KNOWLEDGE_ACCESS_POLICY_CONTRACT.knowledge.json --manifest-root 'D:\SSAI_DATA\knowledge_poc' --actor-user-id 1 --selected-company-id 7
# readback/권한 검색 확인 후 다음 명령
& .\.venv\Scripts\python.exe tools/knowledge_document_manage_cli.py apply --plan docs/03_runbook/RUNBOOK_SIMSAI.knowledge.json --manifest-root 'D:\SSAI_DATA\knowledge_poc' --actor-user-id 1 --selected-company-id 7
# readback/권한 검색 확인 후 다음 명령
& .\.venv\Scripts\python.exe tools/knowledge_document_manage_cli.py apply --plan docs/03_runbook/RUNBOOK_2HO_OPERATION_CHECK.knowledge.json --manifest-root 'D:\SSAI_DATA\knowledge_poc' --actor-user-id 1 --selected-company-id 7
```

readback은 ACTIVE/APPROVED·키·버전·분류·본문 내용/해시를 확인한다. 일반 사용자는
이 세 문서 기술 상세·citation이 차단되어야 한다. GENERAL 업무 가이드는 RAG_USE 조건으로
유지하고 회사 제한 자료의 다른 회사 검색/인용 차단을 확인한다.
retire 명령은 이번 Wave 대상이 아니며 운영 원문 수정과 운영 corpus 변경을 혼동하지 않는다.
