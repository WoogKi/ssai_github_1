---
title: "SIMS AI Platform Master Roadmap v2.3"
subtitle: "운영 안정 기준과 LM Studio 조기 지능 확장을 반영한 Phase 3~8 계획"
author: "김욱기 / SSART"
date: "2026-08-09"
version: "v2.3"
related_schedule: "SIMS_AI_PLATFORM_EXPECTED_SCHEDULE_v1.2_20260809.md"
---

# SIMS AI Platform Master Roadmap v2.3

- **기준일:** 2026-08-09
- **기준 브랜치:** `feat/dashboard-stock-extension-20260727`
- **기준 커밋:** `6c83962bc1b079fe440d56a313de536cf9490651`
- **배포 상태:** 1호기 commit/push 완료, 2호기 동일 커밋 적용 및 smoke 완료
**문서 성격:** 공식 마스터 로드맵

관련 일정은 [SIMS AI Platform 예상 일정 v1.2](SIMS_AI_PLATFORM_EXPECTED_SCHEDULE_v1.2_20260809.md)를 따른다.
LM Studio 기능별 상세 계약은
[LM Studio Intelligence Extension Plan](../02_design/LM_STUDIO_INTELLIGENCE_EXTENSION_PLAN.md)에서 관리한다.
NLQ·현재고·현재표의 세부 구현 계약은
[SIMS NLQ·현재고·현재표 공식 계약](../02_design/SIMS_NLQ_CURRENT_STOCK_CURRENT_TABLE_CONTRACT.md),
기준선 검증 결과는
[6c83962 NLQ·Dashboard·현재고 테스트 결과](../04_test_results/TEST_RESULT_6C83962_NLQ_DASHBOARD_STOCK_20260809.md)를 따른다.

---

## 0. 개정 목적

v2.3은 v2.2의 Phase 3~8 장기 구조와 Facts Backed 원칙을 유지하면서,
2026-08-09의 실제 운영 기준을 공식 기준선으로 다시 설정한다. Dashboard,
NLQ, 현재고와 제품재고현황의 최근 안정화 결과를 완료 상태로 반영하고,
고급 자체 RAG에 앞서 LM Studio가 기본 제공하는 지능 기능을 작은 PoC로
검증하는 중간 단계를 추가한다.

### 0.1 개정 이력

| 버전 | 기준일 | 핵심 변경 |
|---|---|---|
| v2.0 | 2026-07-09 | AI Data Platform, Dashboard/Alert, Knowledge Center, Service AI 구조 도입 |
| v2.1 | 2026-07-19 | Phase 3 완료, Phase 4A~4D와 Analytics Facts 실행순서 정리 |
| v2.2 | 2026-07-19 | Show First·Facts Backed, Dashboard Lite, Phase 3~8 장기계획 확정 |
| v2.3 | 2026-08-09 | `6c83962` 운영 기준, Dashboard/NLQ/현재고 안정화, LM Studio 기본 지능 확장 PoC 반영 |

### 0.2 이번 기준에서 완료로 보는 범위

- Phase 3 인증, 권한, 회사별 DB, 사용자·회사별 저장소 분리
- Dashboard 핵심 facts, 재고위험, 수요급증, 주요 매입처, 상세표·Excel 흐름
- Dashboard deterministic NLQ와 명시조건 우선 처리
- 현재고 최종 20열, 제조사·제품 code-IN, 현재표 후속분석 원본 분리
- 제품재고현황 무라벨 통합검색 표시와 기존 OR-LIKE 의미 보존
- 현재표 제품별 재고수량 TOP의 7열 제품 grain 계약
- 1호기와 2호기의 동일 커밋 배포 및 smoke

완료는 해당 기준선의 기능과 회귀가 운영 확인되었다는 뜻이다. 성능 개선,
운영 자동화와 문서 고도화가 모두 끝났다는 뜻은 아니다.

---

## 1. 플랫폼 목표와 원칙

SIMS AI는 의약품 유통 ERP 원본을 안전하게 조회하고, 결정론적 계산 결과를
분석·시각화하며, 필요한 업무지식과 도구를 연결하는 ERP AI 플랫폼을 목표로 한다.

```text
SIMS ERP 원본
  -> 검증 가능한 deterministic 조회·facts
  -> Dashboard / NLQ / 현재표 / 다운로드
  -> LM Studio 기본 지능 기능과 안전한 Tool
  -> AI Data Platform / ML / Knowledge Center
  -> Workflow / Agent / Service AI
```

공통 원칙:

1. 정확성과 ERP 원본 일치를 속도보다 우선한다.
2. 회사, 사용자, 채팅방, 현재표와 export cache를 분리한다.
3. 숫자와 표는 생산 코드가 확정하고 LLM은 확정된 결과만 설명한다.
4. 명시한 질문 조건은 저장조건보다 우선한다.
5. 화면 표본과 다운로드 원본, 현재표 원본을 구분한다.
6. 신규 SQL이나 모델 기능은 원천 호출 수, 성능과 회귀 영향을 함께 검증한다.
7. LM Studio 기본 제공 기능을 먼저 검증하고 부족한 기능만 SIMS AI에서 구축한다.
8. PoC 성공과 운영 채택을 구분하고 권한·출처·실패 경계를 먼저 정한다.

---

## 2. 전체 Phase 체계

| Phase | 명칭 | 핵심 목표 | 2026-08-09 판단 |
|---|---|---|---|
| Phase 3 | 인증·권한·회사별 DB | 사용자/회사 격리와 운영 보안 | 완료·운영 유지 |
| Phase 4 | Dashboard·KPI·NLQ 안정화 | facts, 현재표, 현재고, UX, 성능, 의미 계약 | 핵심 안정화 완료·후속 보완 |
| Phase 4.x | LM Studio 기본 지능 확장 PoC | 문서 주입, Tool, Web, 멀티모달, 구조화 출력 | 조기 검증 예정 |
| Phase 4.5 | AI Data Platform | Validation, 집계·통계 DB, Feature Store, KPI Library | 설계 예정 |
| Phase 5 | ML 예측·추천 | 매출예상, 재고부족, 발주추천, AI Score | Phase 4.5 이후 |
| Phase 6 | Knowledge Center·자체 RAG | 문서 수집·검색·권한·버전 운영화 | 기본 기능 PoC 이후 |
| Phase 7 | Fine-tuning·Agent·Workflow | 부족한 모델 능력과 업무흐름 자동화 | 필요성 검증 후 |
| Phase 8 | SIMS ERP·Groupware Service AI | 고객지원, 교육, 설치, 장애, 개발 지원 | 점진 확장 |

추진 순서는 선행 Phase가 모든 세부 기능을 완전히 끝내야만 다음 조사를 시작한다는
뜻이 아니다. 데이터·보안 계약을 훼손하지 않는 작은 읽기 전용 조사와 PoC는 앞당길
수 있지만, 운영 채택은 해당 Phase의 Gate를 통과해야 한다.

---

# Phase 3. 인증 / 권한 / 회사별 DB

## 3.1 완료 상태

Phase 3는 운영 기준으로 완료되었다.

| 영역 | 완료 계약 |
|---|---|
| 인증 | 로그인과 사용자 승인·취소·재승인 |
| 회사 DB | 접근 가능한 회사만 선택하고 회사별 ERP 연결 사용 |
| 권한 | 조회, KPI, 다운로드, 업로드, 관리자 기능 분리 |
| 저장소 | 사용자·회사별 채팅, 업로드, 다운로드, 로그 분리 |
| 현재표 | 회사 변경 시 이전 회사 context와 export cache 제거 |
| 운영 | 1호기 개발·검증 후 2호기 동일 커밋 반영 |

## 3.2 유지 Gate

- 로그인하지 않은 사용자의 SIMS 기능 접근 차단
- 권한 없는 회사 DB와 다른 사용자의 저장소 접근 차단
- 회사 변경 시 current table, stale payload, export cache 재사용 금지
- 인증·회사 DB 변경 시 Master/Analytics/IO와 권한 회귀 수행

---

# Phase 4. Dashboard / KPI / NLQ 안정화

## 4.1 현재 안정 기준선

| 영역 | 현재 공식 계약 |
|---|---|
| Dashboard | 정상 조회의 원천 호출 수 `source_call_count=3` |
| Dashboard 기간 | 미지정 시 완료된 직전 6개월, 명시기간 우선 |
| Dashboard 공급조건 | 명시 제약사/발주처/담당자 우선, 미명시 조건만 저장 profile 사용 |
| Dashboard no-data | 조건 resolver 성공 후 facts가 0건이면 빈 Dashboard 대신 `no_data` |
| 현재고 | 화면·CSV·Excel 공통 최종 20열 |
| 현재고 검색 | 명시 제조사·제품은 code-IN과 AND, 무라벨은 제조사/제품 범위 합집합 |
| 현재고 재고 | 전월 누계와 당월 상세를 기준일 현재까지 계산 |
| 제품재고현황 | 무라벨은 매입처·발주처·제품·제조사 OR-LIKE 통합검색 의미 유지 |
| 현재표 TOP | 제품별 재고수량 TOP은 제품 grain 7열 출력 |
| 결과 저장 | 채팅 결과는 1회 저장하고 현재표 원본과 display copy를 분리 |

현재고 20열:

`순번 / 제품코드 / 제품명 / 규격 / 재고위치명 / 재고수량 / 현보험약가 /
보험금액 / 표준코드 / 제품그룹명 / 제품구분명 / 제품분류명 / 발주처코드 /
발주처명 / 제조사코드 / 재고위치코드 / KD코드 / EDI코드 / 제조사명 /
포장단위`

화면, CSV, Excel과 current-table 원본은 이 열 계약을 공유하되, 반복값 공란은
화면용 display copy에만 적용한다.

현재표 제품별 재고수량 TOP 출력:

`순번 / 제품코드 / 제품명 / 규격 / 제조사명 / 재고수량 / 보험금액`

## 4.2 완료된 핵심 안정화

- Dashboard 재고위험, 수요급증 세부, 주요 매입처 위험, 위험 상세와 Excel
- Dashboard aliases를 동일 deterministic handler로 연결
- 담당자·제약사·발주처·기간의 순서 독립 파싱과 명시조건 우선
- 현재고 제조사/제품 LIKE resolver 결과를 코드 범위로 조회
- 현재고 display copy의 반복행·제품합계 가독성과 원본 보존
- current-table 판정 dimension, 제조사별 매출과 inventory TOP 후속분석
- partial source와 download limit 근거 분리
- 제품재고현황 무라벨 조건의 `통합검색 <값>` 표시

## 4.3 남은 후속 과제

1. 제품재고장 무라벨 OR-LIKE 경로의 약 60초 병목을 의미 보존 상태에서 개선한다.
2. 2호기 ScheduledTask 중지 후 child Streamlit process lifecycle을 안정화한다.
3. 무라벨 제품재고현황의 `nlq.trace.parsed search_fields`와 실제 통합검색 의미를 맞춘다.
4. 문서와 공식 회귀 결과를 `6c83962` 기준으로 정리한다.
5. 임계값은 저장 profile과 실제 전달값을 사용하고 특정 준비율을 플랫폼 고정값으로 간주하지 않는다.

## 4.4 Phase 4 Gate

- 정상 Dashboard `source_call_count=3` 유지
- no-data, input-required, routing-error를 빈 성공화면과 분리
- 현재고 display/full/current-table/export 경계 유지
- 현재표 후속질문은 최신 회사·방의 source table만 사용
- 무라벨 성능 개선 시 결과 제품 집합과 수량·금액 완전 일치
- 1호기·2호기 동일 commit과 smoke 증적 확보

---

# Phase 4.x. LM Studio 기본 지능 확장 PoC

## 4.x.1 목적

자체 플랫폼을 크게 만들기 전에 현재 LM Studio와 OpenAI-compatible API가 제공하는
기능을 읽기 전용으로 확인하고, 업무 가치가 높은 기능부터 작은 PoC로 검증한다.

## 4.x.2 우선 검증 순서

| 순서 | 기능 | PoC 목표 |
|---:|---|---|
| 1 | 문서 주입 / RAG | PDF·DOCX·TXT 근거를 답변과 함께 제시 |
| 2 | 현재 날짜/시간 Tool | 모델 기억이 아닌 실행 시점의 기준시각 제공 |
| 3 | Web/latest information | 최신 뉴스·제도 자료에 출처와 기준일 표시 |
| 4 | 첨부 문서 | 업로드 파일의 텍스트·표를 회사/사용자 권한 안에서 분석 |
| 5 | VLM / image | 화면·이미지 입력을 설명하되 민감정보 노출 차단 |
| 6 | OCR | 스캔 문서 인식 품질과 표 구조 보존 평가 |
| 7 | STT / 음성 | 음성을 SIMS 질문으로 변환하고 사용자 확인 후 실행 |
| 8 | Structured Output | action, params, status를 schema로 제한 |
| 9 | Tool Use / MCP | 허용된 읽기 도구만 명시적으로 호출 |
| 10 | Embeddings | 기본 RAG로 부족할 때 검색 품질·비용 기준 확보 |

## 4.x.3 운영 원칙

- LM Studio 기본 제공 기능을 먼저 사용한다.
- 기본 기능으로 충족되지 않는 권한, 검색 품질, 버전, 감사 기능만 자체 구축한다.
- Web, 현재시각, ERP 값은 모델 학습 기억이 아니라 Tool과 검증 원천을 사용한다.
- 외부 정보는 URL, 수집시각과 기준일을 표시한다.
- 문서·이미지·음성은 사용자와 회사 권한을 적용하고 원본을 무단 재사용하지 않는다.
- Tool은 허용 목록, 파라미터 검증, timeout, source_call_count와 감사 로그를 갖춘다.
- PoC에서는 DB 쓰기와 DDL을 허용하지 않는다.
- 기능별 상세 계약은
  [LM Studio Intelligence Extension Plan](../02_design/LM_STUDIO_INTELLIGENCE_EXTENSION_PLAN.md)에서 관리한다.

## 4.x.4 완료 Gate

- 1호기와 2호기의 LM Studio 버전·모델·API 기능표 확보
- 기능별 지원/미지원과 실패 메시지 확인
- 최소 한글 업무자료로 정확성·성능·권한 검증
- 운영 채택, 자체 구축, 보류 중 하나로 판정

---

# Phase 4.5. AI Data Platform

## 4.5.1 목적

ERP 원본을 기능마다 반복 계산하지 않고 검증 가능한 공통 데이터 기반을 만든다.

핵심 영역:

- Validation Framework와 원본 대조
- AI 집계 DB와 통계 DB
- Feature Store와 데이터 품질 지표
- AI KPI Library와 metric/grain/provenance 계약
- Dashboard·Alert 공통 facts
- ML/RAG 평가 Dataset

## 4.5.2 Gate

- ERP 원본과 집계 결과의 자동 대조
- 회사·기간·필터·grain·metric·source version 기록
- 갱신 실패와 stale data 감지
- 신규 저장구조는 DDL 검토와 별도 승인 후 적용

---

# Phase 5. ML 예측 / 추천

## 5.1 범위

- 매출예상 baseline과 ML 비교
- 재고부족 위험 예측
- 근거를 포함한 발주 추천
- 영업·재고·거래 위험 AI Score

## 5.2 원칙과 Gate

- 먼저 deterministic rule baseline을 고정한다.
- 학습/검증 기간과 미래 누수를 분리한다.
- 품목·제약사·계절·신제품별 오차를 함께 본다.
- 예측값만 표시하지 않고 근거, 신뢰도, 데이터 기준일을 제공한다.
- 실제 발주·수정은 사용자 승인 없이 자동 실행하지 않는다.

---

# Phase 6. Knowledge Center / 자체 RAG

## 6.1 목적

Phase 4.x의 기본 문서 주입 PoC로 해결되지 않는 검색·권한·버전 요구를
운영 가능한 Knowledge Center로 확장한다.

## 6.2 핵심 구성

| 영역 | 운영 계약 |
|---|---|
| 수집 | 사내 매뉴얼, 업무규칙, 공지, 외부 제도자료의 승인된 원본만 사용 |
| chunking | 문서 종류와 표·제목 구조를 보존하는 분할 정책 |
| embeddings | 모델명, 차원, 생성일, 재생성 이력 관리 |
| vector index | 회사·권한·문서상태 필터를 검색 전에 적용 |
| metadata | 문서명, 버전, 기준일, 소유부서, 보안등급, source URL |
| version | 개정·폐기 문서가 최신 답변에 섞이지 않도록 상태 관리 |
| 평가 | 정답성, 근거 적합성, 검색 누락, 오래된 문서 사용 여부 검증 |
| 운영 | ingestion 실패, stale index, 권한 오류와 인용 근거 감사 |

## 6.3 Gate

- 답변에 문서명·섹션·기준일·출처 표시
- 회사·사용자·역할별 검색 권한 분리
- 폐기 문서와 미승인 문서 제외
- 검색 실패 시 추측하지 않고 자료부족 안내

---

# Phase 7. Fine-tuning / LoRA / Agent / Workflow

## 7.1 적용 원칙

Fine-tuning과 LoRA는 RAG, Structured Output, Tool과 prompt 계약으로 해결되지 않는
반복적이고 안정적인 능력 차이가 확인될 때만 검토한다.

학습 대상으로 적합할 수 있는 항목:

- 변하지 않는 업무 용어와 질문 해석 패턴
- 검수된 응답 형식과 안전한 거절 패턴
- 충분한 양과 품질을 갖춘 익명화 사례

학습 대상으로 사용하지 않는 항목:

- 계속 변하는 ERP 현재값
- 뉴스, 법령, 가격, 재고와 날짜·시간
- 권한에 따라 달라지는 문서 내용
- 비밀번호, 연결정보, 고객 식별정보

## 7.2 Agent / Workflow Gate

- Tool 허용 목록과 단계별 사용자 승인
- 읽기와 쓰기 Tool 분리
- 재시도·중복 실행·부분 실패 처리
- 전체 실행 provenance와 감사 로그
- 사람이 취소·수정할 수 있는 제어점

---

# Phase 8. SIMS ERP & Groupware Service AI

## 8.1 서비스 확장 범위

- SIMS ERP 사용설명서 AI
- 고객 문의와 장애 대응 지원
- 설치·업데이트·운영 점검 지원
- 사용자 교육과 역할별 업무 안내
- 개발자 코드·문서 탐색 지원
- 그룹웨어 업무지식과 승인 흐름 지원

## 8.2 운영 원칙

- 고객사와 사용자 권한을 기능보다 먼저 적용한다.
- 공식 문서와 현재 버전을 근거로 답한다.
- 원격 실행과 변경 작업은 명시적 승인과 감사 기록을 요구한다.
- 서비스별 품질·보안·복구 Gate를 통과한 기능만 단계적으로 공개한다.

---

## 9. 공통 운영 원칙

### 9.1 정확성과 provenance

모든 표·facts·예측·문서 답변은 기준 회사, 기간, 필터, metric, grain,
source version과 생성시각을 추적할 수 있어야 한다.

### 9.2 데이터·권한 격리

회사 변경 시 이전 회사의 current table, context, export cache와 stale payload를
사용하지 않는다. RAG, 첨부, Tool 결과도 같은 격리 기준을 사용한다.

### 9.3 배포와 검증

1호기에서 선택 파일만 검토·commit/push하고, 2호기는 fetch 후 fast-forward만
적용한다. 배포 뒤 commit 일치, Streamlit health, 로그, 핵심 smoke를 확인한다.

### 9.4 성능

성능을 위해 조회 범위나 결과 의미를 축소하지 않는다. 병목 단계와 결과 동등성을
측정한 뒤 최적화하고, SQL Server 제약과 인덱스는 별도 승인 절차로 다룬다.

---

## 10. 2026-08-09 기준 후속 우선순위

1. 2호기 Streamlit child-process lifecycle 안정화
2. 제품재고장 무라벨 OR-LIKE 성능 조사와 적용 가능성 판정
3. LM Studio 1·2호기 기능 인벤토리와 문서 주입/RAG PoC
4. 현재 날짜/시간과 Web/latest information Tool PoC
5. 첨부·VLM·OCR·STT·Structured Output·Tool/MCP PoC
6. NLQ 공식 사례집과 RAG 업무자료 정리
7. Embeddings/자체 RAG 최소판 필요성 결정
8. AI Data Platform 상세 설계

상세 날짜와 Gate는 [예상 일정 v1.2](SIMS_AI_PLATFORM_EXPECTED_SCHEDULE_v1.2_20260809.md)를 따른다.

---

## 11. 문서 운영

- 공식 마스터는 이 v2.3 한 개를 사용한다.
- v2.2와 이전 로드맵은 실제 이동 전까지 역사 기준으로만 참조하며 이후
  `90_archive/roadmap_versions/` 대상으로 분류한다.
- 예상 일정은 v1.2를 공식 일정으로 사용한다.
- 테스트 결과는 당시 증적을 수정하지 않고 새 기준 문서를 추가한다.
- LM Studio 상세 설계는
  [LM Studio Intelligence Extension Plan](../02_design/LM_STUDIO_INTELLIGENCE_EXTENSION_PLAN.md)을
  공식 기준으로 사용한다.
