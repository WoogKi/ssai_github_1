# SIMS AI Platform Master Roadmap v2.4

**기준일:** 2026-10-27
**코드 기준:** `feat/dashboard-stock-extension-20260727` / `3018c5c5f5ef9e712ca34f3e6105e88706697605`
**문서 지위:** 2026-10-27 로컬 공식 로드맵 기준. Git 반영·2호기 배포는 별도 단계다. 이전 Git 기준은 [9/3 로드맵](SIMS_AI_PLATFORM_ROADMAP_20260903.md)과 [일정](SIMS_AI_PLATFORM_EXPECTED_SCHEDULE_20260903.md)이며 두 문서의 본문은 9/14 상태까지 갱신됐다. 9/15 로드맵·일정은 Git 미추적 로컬 판본으로, 당시 공식 승인 여부는 확인되지 않았다.

## 1. 방향과 완료 판정

SIMS AI는 회사·사용자 격리를 기반으로 조회 근거와 계산 정확성을 먼저 보장하고, 검증된 Snapshot/월별 Fact·Knowledge·Tool을 업무에 단계적으로 연결한다. 원 계획의 Phase 3~8을 유지하고, [v2.3의 Phase 4.x LM Studio 지능 확장](../90_archive/roadmap_versions/SIMS_AI_PLATFORM_MASTER_ROADMAP_v2.3_20260809.md)을 복원한다. **MCP는 Phase 7 Agent의 하위가 아닌 독립적인 Tool/MCP 통합 트랙**이다.

상태는 `구현 및 검증 완료`, `구현 완료·운영 검증 필요`, `부분 완료`, `미착수`, `업무 정책 결정 보류`로 나눈다. 과거의 Gate PASS는 그 날짜·회사·원천/fixture의 증거일 뿐이다. GitHub Push, 1호기 실행, 2호기 설치·Health·사용자 Smoke, 실제 운영 승인/게시를 서로 다른 완료 단계로 기록한다. 현재 HEAD의 2호기 설치 버전은 미확인이다.

## 2. Phase별 실행 상태와 남은 목표

| Phase | 현재 상태(2026-10-27 확인 범위) | 유지·보완할 목표와 최종 Gate |
| --- | --- | --- |
| **3 인증·권한·회사 DB** | **구현 및 당시 운영 검증 완료.** `ssai_auth_service.py`, `ssai_permission_policy.py`, `ssai_storage_service.py`, 회사별 DB/채팅 경계와 Phase3 Closeout. | 회사 전환 후 현재표·context·export·Knowledge/MCP 권한 혼입 0. 신규 배포마다 역할별 1/2호기 Smoke. 현재 2호기 설치 HEAD는 별도 확인. |
| **4 NLQ·Analytics·KPI·Dashboard** | **부분 완료.** NLQ/현재표/다운로드·Dashboard 운영 경로 존재. 10/27 선행 Gate 공식 NLQ 224/227(미등록 지역별 매출 3건), Golden 250/250, 발주 43/43. 회사3 Compact Dashboard 업무 데이터 Exact 기록. 회사40은 1호기 Compact 처리시간을 단발 실측했으나 전체 결과 Exact·2호기 검증은 미완료. | 202611 월마감 결과 뒤 성능·원천 경계 점검, 3개 기존 NLQ 사례집 불일치의 업무 결정, Analytics 기존 3 FAIL 분류, 전체/표시·기간·매출/반품·KPI grain 계약. 새 회귀 0, 회사별 동일 조건 Exact·호출/시간 측정. |
| **4 LLM 분석** | **구현 완료·운영 검증 필요.** Python 검증 facts/합계·구조화 해석·최종 화면/저장 경로는 `analysis_facts.py` 및 앱에 구현. | 입고예정/입고/출고/현재표·매출 등 실제 질문에서 정확한 전체 수치와 업무 특징·중립적 확인 제안, 근거 없는 원인/허위 수치 0, 화면/채팅 저장 일치. Offline fixture와 실제 품질 판정을 분리. |
| **4.x LM Studio/Tool/MCP** | **혼합 상태.** datetime·Web·Structured trace·첨부/VLM/OCR/STT 경로는 기능별 구현/PoC 수준이 다르다. MCP Phase1 구조 조사·Phase2 Mock·Phase3 공식 SDK 로컬 STDIO 정적 Fixture **기술 PoC 완료**; 외부 Resource/LM Studio Native MCP/운영 권한·배포는 **미완료**. | 기능별 버전·모델/API/권한 Gate, 명시 읽기 도구의 오류·감사·출처 확인. 월마감 이후 승인 외부 MCP 서버 1개만 독립 read-only PoC 검토. 기본 OFF·전용 권한·Allowlist·timeout·retry0·fallback0. Native 전환은 별도 비교 후 판단. |
| **4.5 Snapshot·AI Data Platform** | **부분 완료.** Snapshot schema2.1/통일 algorithm, Migration010 통합 `monthly_io_fact`, 011 작업이력, 증분 Draft·독립 Exact/승인 도구가 존재. 202610 회사3/6/40 게시 이력. 회사3는 승인 Fact 202603~09와 저장 Checksum을 확인한 조건부 A, 운영 회사는 승인된 실조회 전 D. | 202611 회사별 Profile/Scope·10월 원천→신규월 Fact Exact·명시 승인→Snapshot Inspection/별도 게시 승인→Reader/세 조회 Exact. 기존 승인본·12개월 보존. 최종 Snapshot은 아직 R120 최근 3개월 직접 조회이므로 완전 1개월 증분이라 쓰지 않음. 범용 Validation/KPI Library/통계/Feature Store는 단계적 확장. |
| **5 재고부족·발주·예측·ML** | **1차 규칙 기반 발주계산/예측은 구현·회귀 검증**, 학습 ML·AI Score는 미착수. 발주가 실제 ERP 전표로 등록되는 기능과 혼동하지 않음. | 11월 기존 계산/추천·수요예측 해석/예외를 업무 피드백으로 고도화. ML 플랫폼 완료까지 1차 개선을 미루지 않음. 이후 기간 누수 없는 Dataset과 baseline 대비 모델 검증; 실제 발주 쓰기는 별도 승인·감사·복구 Gate. |
| **6 Knowledge Center/RAG·외부 정보** | **핵심 부분 완료.** 승인 자료 검색·권한/회사 경계·인용 및 9/15 읽기 전용 감사 기록. 외부 MCP/OpenAPI/그룹웨어 실서비스는 별도 미완료. | 11~12월 문서 소유자·ACTIVE/APPROVED·최신성·권한·1/2호기 corpus 동기화·검색 품질을 운영 확장. Embedding/Vector는 lexical 한계가 재현될 때. 외부 업무자료는 Tool/MCP 트랙과 출처·권한을 공통 검증하되 Knowledge 증거로 자동 승격 금지. |
| **7 Feedback·Fine-tuning·Agent·Workflow** | **Feedback 수집/검토 기반 부분 완료**, 학습 LoRA/GGUF·Agent·실행 Workflow는 미착수/조건부. MCP 로컬 PoC가 Agent 완료 근거는 아님. | 사람 검수 실패사례→공식 사례집/평가셋. 기존 RAG/Tool/규칙으로 해결되지 않는 반복 격차가 확인돼야 학습·Agent 후보로 올림. Tool 허용목록, 단계 승인, 실패/중복/취소·감사·회사 격리. |
| **8 SIMS ERP·Groupware Service AI** | **내부 앱/Runbook 기반만 존재; 고객 대상 서비스 미착수.** | 사용설명·문의·장애·설치·교육·그룹웨어·개발자 지원을 독립 서비스로 검토. 공식 콘텐츠·고객 권한·SLA·감사·원격 변경 승인·복구/배포 Gate 후 단계 공개. |

**회사40 Dashboard 성능 근거(10/26 1호기 단발 로그):** 기존 상세 경로 두 번의 응답은 192.929초·329.386초, R120 반환은 2,121,885행·2,124,388행, SQL은 87.655초·229.680초였다. 이후 Dashboard Compact 경로는 응답 83.873초, 228,974행, SQL 56.851초였다. 같은 평가월 `202610`·완료월 `202604~202609`에서 호출 계약은 `source_call_count=3`, `physical_query_count=4`로 유지됐다. 서로 다른 시각의 단발 실행이며 원천 행수도 달라 확정 개선율이나 전체 결과 Exact로 판정하지 않는다. 출처: 10/26 공유 보관 로그 `1st_ssai_test_20261026_085441 befor .txt`, `1st_ssai_test_20261026_095141  after2 .txt`, `1st_ssai_test_20261026_151620.txt`. 기존 Snapshot·월별 Fact 사용으로 인한 개선이라고 주장하지 않는다.

## 3. 공통 운영 정책과 미결정 업무

- 데이터 숫자는 검증된 Python/ERP·승인 Snapshot에서 결정하고 LLM은 근거를 해석한다. 수량·보험금액·매출액·원가, 전체 조회·화면 표본·부분 원본, 원본/확정 금액을 섞지 않는다.
- 현 Dashboard의 R220 장부 매출과 R210 실재고 분리, 7xx/8xx/9xx 미결 매출의 익월 정산 및 2차 확정마감은 **업무 정책 결정 보류**다. 승인 전 자료원을 일괄 전환하거나 확정 규칙으로 문서화하지 않는다.
- Snapshot/월별 Fact는 회사·월·저장 Profile·제품 Universe·입출고구분·알고리즘·Generation/Checksum이 일치할 때만 재사용한다. 승인 Draft를 운영 Reader에 노출하지 않고, 기존 승인 이력과 최근 12개월 마감월 보존을 지킨다.
- MCP는 기본 OFF·명시 요청·전용 `MCP_EXTERNAL_RESOURCE_READ`·등록 Resource Allowlist·timeout/retry0·실패 시 다른 업무 도구 자동 전환 금지를 유지한다. 운영 ERP DB 쓰기는 금지. 외부 서버·LM Studio Native API는 현재 채팅의 OpenAI 호환 호출과 다른 계약이므로 별도 검증 전 통합하지 않는다.
- 6/21~9/15 계획에서 일정이 지난 항목은 자동 실패/완료로 처리하지 않는다. 기능마다 코드/Offline, 승인 실데이터, 1호기 사용성, 별도 2호기 설치 증거를 기록한다.

## 4. 실행 순서와 사용자 결정

- **P0:** 10/30 준비·11/1 월마감(회사8 14:00 이후), 원본/승인 Exact·Reader 보호.
- **P1:** 실제 마감 후 문서 기준선·운영 Runbook 현행화, Dashboard/LLM/NLQ 품질·성능.
- **P2:** 11월 규칙 기반 발주/수요예측 1차 개선, KPI/데이터 계약, Knowledge/RAG 운영 확대. 월마감 안정 후 **독립 MCP 외부 1개 read-only PoC 검토**.
**P3/조건부:** 검증 Dataset 기반 ML, 품질 한계 확인 시 Embedding, 승인된 외부 서비스, 이후 Agent/Fine-tuning/고객 Service AI.

필요한 결정: 운영 회사 DB 점검 범위와 승인 참조값, R210/R220 및 미결 매출 마감, NLQ 기존 3건 신규 업무 여부, 외부 MCP 서버 신원·권한/키/네트워크, 실발주 쓰기 범위, 2호기 배포 승인. [월별 일정](SIMS_AI_PLATFORM_EXPECTED_SCHEDULE_v1.3_20261027.md), [과거→신규 비교](SIMS_AI_PLATFORM_ROADMAP_COMPARISON_v1_20261027.md), [항목별 추적 CSV](SIMS_AI_PLATFORM_TRACEABILITY_v1_20261027.csv), [Phase 세부 대응](SIMS_AI_PLATFORM_PHASE_MAPPING_v1_20261027.md), [MCP 이력·다음 단계](../02_design/SIMS_AI_MCP_CURRENT_STATE_AND_NEXT_PHASE_20261027.md)를 이 공식 기준의 추적 부록으로 사용한다. 개별 실행·운영 승인은 각 절차에 따른다.
