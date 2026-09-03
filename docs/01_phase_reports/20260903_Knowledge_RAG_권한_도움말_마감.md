# Knowledge/RAG 권한 및 도움말 마감 기록

작성일: 2026-09-03

## 배포 기준

- 최종 HEAD: `ad609f71206081c26a551e57ca216b5fcb31f5b3`
- 사용자 확인 기준으로 1호기, GitHub, 2호기는 동일 HEAD이며 2호기 Scheduled Task는 Running, Health는 `200 / ok`이다.
- 이번 마감 점검은 애플리케이션 코드, DB, manifest, 승인 이력, Git stage/commit/push를 변경하지 않았다.
- 2호기 `D:\SSAI_DATA\knowledge_poc`은 사용자에 의해 읽기 전용으로 재확인됐다. v4는 CURRENT이며, Scheduled Task는 Running, Health는 `200 / ok`이다.

## 완료 기능

- `/knowledge`: 승인된 일반 Knowledge 범위만 검색하며, 권한 또는 근거가 없으면 fail-closed 한다.
- `/knowledge-tech`: ERP 및 Project Source 기술상세 권한과 ACTIVE/APPROVED/freshness 조건을 함께 확인한다. 자연어 wrapper는 기술상세 경로에서만 좁게 정규화한다.
- citation-bound follow-up: parent citation의 정확한 document/version, user, company, room을 재인가한 뒤에만 같은 문서 범위에서 후속질문을 처리한다.
- table-layout follow-up: 기술 문서의 필드, 컬럼, 수량, 금액, 날짜, 거래처 관련 질문을 citation-bound 범위 안에서만 처리한다.
- 관리자 실효권한 UI: 선택한 사용자와 회사의 역할 및 Knowledge 관련 effective permission을 읽기 전용으로 표시한다.
- SIMS 도움말: 명시적 도움말 intent를 deterministic하게 처리하며 LLM, RAG retrieval, Web, DB, MCP를 실행하지 않는다.
- 도움말 Knowledge 예시: effective permission과 ACTIVE/APPROVED/freshness를 만족하는 자료가 실제 존재할 때만 표시한다.

## 역할별 Knowledge 권한

| 역할 | RAG_USE | PROJECT_SOURCE_READ | ERP_DB_READ | COMPANY_MANAGE | GLOBAL_MANAGE |
| --- | --- | --- | --- | --- | --- |
| SYSTEM_ADMIN / SSART_ADMIN / SUPER | 허용 | 허용 | 허용 | 허용 | 허용 |
| SSART_MANAGER | 허용 | 허용 | 허용 | 허용 | 미허용 |
| SSART_STAFF | 허용 | 미허용 | 미허용 | 미허용 | 미허용 |
| WHOLESALE_MANAGER | 허용 | 미허용 | 미허용 | 허용 | 미허용 |
| WHOLESALE_STAFF | 허용 | 미허용 | 미허용 | 미허용 | 미허용 |
| WHOLESALE_READONLY | 미허용 | 미허용 | 미허용 | 미허용 | 미허용 |

`KNOWLEDGE_COMPANY_MANAGE`는 읽기 권한을 확장하지 않는다. 특히 WHOLESALE_MANAGER는 ERP DB 내부 자료나 Project Source 기술상세를 읽을 수 없다.

## 수동 Smoke

| 대상 | 결과 |
| --- | --- |
| SYSTEM_ADMIN / admin | PASS |
| SSART_MANAGER / test02 | PASS |
| WHOLESALE_MANAGER / test1111 | PASS |
| WHOLESALE_STAFF | PASS |
| WHOLESALE_READONLY | 실제 로그인 계정 없음. 관리자 readback 및 자동 Gate PASS |
| 2호기 Knowledge/도움말 Smoke | PASS, 사용자 확인 |

## Project Source Freshness

대상 source key:

`project-source:app/services/ssai_storage_service.py#get_user_file_path`

- 1호기 manifest `C:\SSAI_TEST_DATA\knowledge_poc`에서 v4는 `ACTIVE + APPROVED + CURRENT`로 확인됐다.
- v4 revision은 `ad609f71206081c26a551e57ca216b5fcb31f5b3`이며, source content hash와 현재 HEAD worktree가 일치한다.
- v1, v2, v3은 `SUPERSEDED` 상태이고 freshness 검사에서는 STALE로 유지된다.
- 2호기 manifest `D:\SSAI_DATA\knowledge_poc`은 사용자 읽기 전용 확인에서 v4 `CURRENT`, approved source revision `ad609f71206081c26a551e57ca216b5fcb31f5b3`, `current_source_content_hash_matches=true`, `worktree_matches_head=true`로 확인됐다.
- 2호기에는 v1과 v3이 존재하며 둘 다 STALE이다. v2가 존재하는 것으로 기록하지 않는다.

## 사용 방식

- 일반 승인 Knowledge: `/knowledge <업무 주제>`
- 기술상세 Knowledge: `/knowledge-tech <테이블명 또는 기술 주제>`
- 기술상세 예시: `/knowledge-tech Rddbc110 관련 기술 내용을 알려줘`
- 인용 답변의 후속질문은 답변 UI에서 자연어로 입력한다. 후속 retrieval은 parent citation의 document/version 범위를 벗어나지 않는다.

## 도움말 정책

- `SIMS 사용법 알려줘`, `SIMS 질문 예시 알려줘` 등 명시적 intent만 deterministic help으로 처리한다.
- 재고, 입고/출고, 매출/매입, KPI/예측, 현재표 분석, SIMS 일일점검 예시는 사용자 effective permission에 맞춰 표시한다.
- Knowledge 예시는 권한만이 아니라 현재 승인 corpus의 ACTIVE/APPROVED/fresh 상태까지 충족해야 표시한다.
- 일반 Knowledge 승인 corpus는 현재 충분하지 않아, 성공 근거가 없는 일반 `/knowledge` 예시는 숨긴다.
- 내부 구현 용어, DB/SQL 및 권한 없는 기술 자료의 존재 여부는 일반 도움말에 노출하지 않는다.

## 자동 Gate

Knowledge role/admin/scope/history/follow-up/document, real lexical 30/30, Project Source approval/freshness, SIMS help, structured response, datetime, Web, MCP, Analytics, IO, Master, current-table, `py_compile`, `pip check`, `git diff --check`가 모두 PASS했다.

## 별도 후속 항목

- WHOLESALE_READONLY 실제 로그인 계정으로 UI Smoke를 추후 한 번 확인한다.
- 외부 MCP/OpenAPI 실제 연동은 별도 과제로 진행한다.
- 100,000행 이상 대용량 구조 개선은 별도 백로그다.
- 회사 4 SIMS 일일점검은 2026-09-03 운영 로그에서 약 85초가 관찰됐다. Knowledge blocker가 아닌 별도 성능 후속 항목이다.
- SQL Server 진단 probe 3개는 미추적 상태이며 이번 마감 및 배포 대상에서 제외한다.

## 최종 판정

`CLOSEOUT_READY`

1호기와 2호기의 Project Source v4 CURRENT 및 freshness 조건이 모두 확인됐다. 코드 또는 권한 정책의 blocker는 없으며, 별도 후속 항목은 현재 마감 범위를 막지 않는다.
