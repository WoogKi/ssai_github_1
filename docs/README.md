# SSAI 문서 운영 기준

## 목적

이 디렉터리는 SSAI/SIMS AI 프로젝트의 공식 계획, 단계 보고서, 설계,
운영 절차, 테스트 증적과 역사자료를 관리한다. 문서는 현재 구현의 동작을
설명하는 기준이면서, 특정 시점의 의사결정과 검증 결과를 보존하는 기록이다.

## 현재 공식 문서

- Knowledge 권한: [읽기·관리·회사격리·소스 최신성 계약](02_design/KNOWLEDGE_ACCESS_POLICY_CONTRACT.md)
- 마스터·재고정보 사용자 안내: [일반 업무 설명](03_runbook/SIMS_AI_MASTER_INVENTORY_USER_GUIDE.md)
- 마스터·계약단가·구매원가: [현재 조회 기술 계약](02_design/SIMS_MASTER_PRICE_QUERY_CONTRACT.md)
- 제품재고장: [기간 재고·단가 조회 계약](02_design/PRODUCT_INVENTORY_LEDGER_CONTRACT.md)
- Snapshot 2.1·제품정보: [운영 조회 계약](02_design/SNAPSHOT_PRODUCT_INFORMATION_V21_CONTRACT.md)
- Knowledge Wave 3: [authority 보강·등록 준비 검토](04_test_results/KNOWLEDGE_WAVE3_AUTHORITY_REVIEW_20260914.md) (등록 대상 아님)
- 발주 계산 1차 업무 정의: [발주 계산 업무 계약](02_design/ORDER_CALCULATION_PHASE1_BUSINESS_CONTRACT.md) (v1.5, 회사 공통 계약·회사별 사례 분리)
- 발주 1차 운영 마감: [발주 계산/NLQ Closeout](04_test_results/ORDER_CALCULATION_PHASE1_CLOSEOUT_20260914.md)
- 일반 사용자 Knowledge: [업무질문 사용 예시와 발주 업무 의미](03_runbook/SIMS_AI_업무질문_사용_예시.md)
- 통합 로드맵·일정: [SIMS AI Platform Roadmap 2026-09-03](00_roadmap/SIMS_AI_PLATFORM_ROADMAP_20260903.md)
- 공통 운영: [SIMS AI 공통 운영 Runbook](03_runbook/RUNBOOK_SIMSAI.md)
- 2호기 운영: [SIMS AI 2호기 운영 점검 Runbook](03_runbook/RUNBOOK_2HO_OPERATION_CHECK.md)
- LM Studio 확장 설계: [LM Studio Intelligence Extension Plan](02_design/LM_STUDIO_INTELLIGENCE_EXTENSION_PLAN.md)
- Dashboard Lite 설계: [Dashboard Lite v0.1 설계](02_design/DASHBOARD_LITE_V01_DESIGN.md)
- NLQ·현재고·현재표 계약: [SIMS NLQ·현재고·현재표 공식 계약](02_design/SIMS_NLQ_CURRENT_STOCK_CURRENT_TABLE_CONTRACT.md)
- 공식 테스트 결과: [6c83962 NLQ·Dashboard·현재고 테스트 결과](04_test_results/TEST_RESULT_6C83962_NLQ_DASHBOARD_STOCK_20260809.md)

발주 1차의 운영 기준선은 `feat/dashboard-stock-extension-20260727` 브랜치의
`7283865dcf8709ec9673404b0321adfe208c65be` 커밋이다. 다른 기능의 날짜별 완료 기록은
그 문서에 명시된 기준 commit을 유지하며 역사자료를 덮어쓰지 않는다.

## 발주·Knowledge 공식 색인

| 업무 | 일반 사용자 설명 authority | 기술·운영 authority |
|---|---|---|
| 발주 계산·NLQ·대표매입처·발주단위·공식 Calendar | 업무질문 사용 예시의 발주 계산/공식 영업일 | 발주 업무 계약 제2~5·9·12절 |
| 계약단가·최종 매입가 | 같은 문서의 계약단가/수량과 가격 | 발주 업무 계약 제6절, 기존 계약단가 서비스 |
| 제품정보·제조사·현재고·Snapshot/profile | 같은 문서의 제품정보와 현재고 | 발주 업무 계약 제7·9절, NLQ·현재고·현재표 계약 |
| 입고예정 | 같은 문서의 입고예정 조회 | 발주 업무 계약 제4절, 기존 발주/입고예정 서비스 |
| 단가적용처·재고적용처 | 같은 문서의 단가적용처와 재고적용처 | 발주 업무 계약 제6절 |
| 기존 발주 조회 | 같은 문서의 발주 조회 | 발주 업무 계약 제1·12절 및 등록된 기존 조회 경로 |

현행 NLQ 설명은 위 색인과 공식 [기간정책](02_design/SIMS_AI_NLQ_기간정책_공식기준.md),
[현재고·현재표 계약](02_design/SIMS_NLQ_CURRENT_STOCK_CURRENT_TABLE_CONTRACT.md)을 따른다.
`90_archive/nlq_legacy/root/nlq_docs_index.md`와 `master_nlq_summary.md`는 2026-05 역사 색인이다.
이를 현행 RAG에 재등록하거나 같은 이름의 새 기준을 만들지 않는다.
등록 대상·분류·권한·순서는 [공통 Runbook 제9절](03_runbook/RUNBOOK_SIMSAI.md#9-발주-1차-knowledge-등록-준비)을 따른다.
Codex 보고서, diff, 원시 로그, probe와 중간 성능 조사는 RAG authority에서 제외한다.

## 공식 폴더 구조

```text
docs/
  00_roadmap/       마스터 로드맵, 일정, 전체 실행계획
  01_phase_reports/ Phase 완료 보고서와 단계별 결과
  02_design/        기능, 데이터, 화면, AI 확장 설계
  03_runbook/       현재 운영에 사용하는 실행·배포·복구 절차
  04_test_results/  기준 시점이 명시된 테스트 결과와 검증 증적
  90_archive/       구버전, 완료 계획, 임시 문서, 대체된 문서
```

일부 기존 문서는 루트, `archive/`, 생성물 폴더 등에 남아 있다. 공식 기준에서
대체된 로드맵과 예상 일정은 `90_archive/roadmap_versions/`에 보관하며, 나머지
보관 후보는 링크와 역사적 의미를 확인한 뒤 별도 문서 정리 작업에서 이동한다.

## Source Of Truth

- Markdown(`.md`)을 공식 source-of-truth로 사용한다.
- DOCX와 PDF는 배포 또는 열람을 위한 export/generated artifact다.
- XLSX, CSV, PNG, ZIP, 로그와 실행 결과도 별도 승인된 배포 정책이 없는 한
  공식 원본으로 사용하지 않는다.
- 생성물을 다시 만들 때는 대응하는 최신 Markdown을 원본으로 사용한다.
- 동일 목적의 공식 문서는 한 개만 유지한다. 새 버전이 공식화되면 이전 버전은
  `90_archive/` 대상으로 분류한다.

## 개정과 보관 원칙

- 최신 구현과 다른 공식 문서는 즉시 개정하거나 공식 기준에서 제외한다.
- 구버전, 완료된 계획, 임시 조사자료, 중복 export는 `90_archive/` 대상이다.
- 완료 보고서와 테스트 결과는 당시 기준의 증적이다. 최신 내용으로 덮어쓰지
  않고, 새로운 기준의 결과를 새 문서로 작성한다.
- 문서의 기준일, branch, commit, 적용 범위가 불명확하면 공식 기준으로 사용하지
  않는다.
- 설계 목표와 현재 구현이 다르면 두 상태를 구분하여 기록하고, 미구현 목표를
  현재 동작처럼 표현하지 않는다.
- archive 실제 이동은 링크와 중복 관계를 확인한 후 별도 작업으로 수행한다.

## 문서 작성 기준

- 제목, 버전, 기준일, 기준 branch/commit, 문서 성격을 문서 앞부분에 기록한다.
- ERP 용어는 매입처, 발주처, 공급처, 제약사/제조사, 재고적용처의 의미를
  구분하여 사용한다.
- 구현 계약은 원천, 기간, grain, metric, 필터 우선순위와 예외 상태를 함께 쓴다.
- 예정 기능은 조사, PoC, 운영 채택을 구분한다.
- 민감한 DB 연결정보, 비밀번호, API token 원문, 고객 데이터는 기록하지 않는다.

## Git 운영 원칙

- 공식 Markdown만 경로를 명시하여 선택적으로 stage한다.
- `git add .` 또는 `git add -A`로 문서와 로컬 산출물을 함께 stage하지 않는다.
- 로그, 임시 실행 결과, 로컬 export, 다운로드, 업로드, cache는 commit하지 않는다.
- 문서 변경 전후에 `git status`, `git diff --check`, 내부 상대링크를 확인한다.
