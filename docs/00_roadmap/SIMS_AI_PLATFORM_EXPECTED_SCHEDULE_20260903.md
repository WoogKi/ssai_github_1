---
title: "SIMS AI Platform 예상 일정"
subtitle: "2026-09-03 통합 로드맵 Rev.1 기준 - 중기·장기 상세 보강본"
author: "김욱기 / SSART"
date: "2026-09-03"
version: "2026.09.03-r1"
---

# SIMS AI Platform 예상 일정

- 기준일: 2026-09-03
- 기준 로드맵: SIMS AI Platform Roadmap 2026-09-03 Rev.1
- 기준 HEAD: ad609f71206081c26a551e57ca216b5fcb31f5b3
- 일정 원칙: 기능 의미·조회 범위·권한·provenance를 줄여 날짜를 맞추지 않는다.
- 운영 원칙: Snapshot 재생성과 대용량 검증은 주말 또는 비업무시간에 수행한다.
- 일정 성격: 확정 납기표가 아니라 선행조건과 Gate를 기준으로 한 예상 실행 창이다.

## 1. 예상 일정 요약

| 예상 기간 | 작업 | 우선순위 | 완료 Gate / 비고 |
| --- | --- | --- | --- |
| 9/3~9/4 | 로드맵·문서 마감 및 Snapshot 실행 준비 | P0 | 공식 로드맵/예상 일정 확정, 대상 회사·Snapshot 상태·실행 순서 확인 |
| 9/5~9/6 (주말) | Snapshot 월초 재실행·전환·검증 | P0 | 비업무시간 재생성, 승인/latest-eligible 확인, 회사별 3개 경로 결과 동등성·elapsed·provenance 확인 |
| 9/7~9/8 | 회사 4 SIMS 일일점검 85초 병목 조사 | P1 | 단계별 elapsed 분해, Snapshot 사용 여부, 기존 병목과 연결, 원인 확정 후 적용/보류 판단 |
| 9/9~9/11 | 대용량 공통 처리 기반 설계 | P1 | Display Fast Path / Analysis Path / Export Path / CurrentTableContext-query contract 초안 확정 |
| 9/14~9/16 | 대용량 공통 계약 Gate 및 소규모 PoC | P1 | 화면 표본 vs 전체범위 분리, aggregate/provenance, timeout/cancel/export 정책 검증 |
| 9/17~9/23 | 계약단가 기능 설계·구현·회귀 | P2 | 테이블/필드 의미 확정, 권한·기간·grain 계약, NLQ/현재표/대용량 경로 재사용 |
| 9/24~10/2 | 발주 기능 설계·구현·회귀 | P2 | 계약단가·재고·예측 기준 연결, 사용자 승인 경계, 회귀/2호기 Smoke |
| 10/5~10/9 | 입출금 기능 설계 입력·1차 구현 | P3 | 회계 grain·민감정보·권한 범위 확정 후 읽기 중심 1차 기능 |
| 자료 확보 시 병행 | 외부 MCP/OpenAPI 실서비스 연결 | P2 | 승인 API 명세 확보 후 endpoint/secret/allowlist/timeout/retry=0/감사/Smoke |
| 조건 발생 시 | Embedding 재검토 | 조건부 | corpus 확대 또는 lexical 30/30 기준에서 재현 가능한 한계 발생 시만 착수 |
| 계정 생성 시 | WHOLESALE_READONLY 실제 로그인 Smoke | P2 운영 | Knowledge 비노출 및 조회전용 역할 경계 확인 |

## 2. 주차별 실행 흐름

### 9/3~9/6 - Snapshot P0 마감
- 9/3~9/4: 현재 Snapshot 상태, latest-eligible, 회사별 대상과 실행 순서를 읽기 전용으로 재확인한다.
- 9/5~9/6: 주말/비업무시간에 필요한 Snapshot만 재생성하고 승인·전환한다.
- 회사별 필수 비교는 반드시 다음 3개로 고정한다.
  1. 현재고 출고빈도 A
  2. SIMS 일일점검
  3. 제품재고장 출고빈도 A
- 완료 판정은 재생성 성공이 아니라 결과 동등성, elapsed, source provenance, fallback 여부까지 포함한다.

### 9/7~9/11 - 성능 원인 확정 + 대용량 설계
- 회사 4 SIMS 일일점검 약 85초 로그를 단계별로 분해한다.
- 이미 알려진 판매/매입/재고/입고 원천과 facts 조립 병목을 현재 Snapshot 구조와 다시 연결한다.
- 성능 때문에 기간·조건·결과 의미를 줄이지 않는다.
- 병행하여 월 100,000행 이상을 전제로 공통 대용량 처리 계약을 설계한다.

### 9/14~9/16 - 공통 대용량 계약 확정
- Display Fast Path: 첫 화면은 빠르게, 화면 표본은 전체 분석 원본과 분리한다.
- Analysis Path: 현재표 분석은 화면 200행이 아니라 전체 조회조건 범위를 의미한다.
- Export Path: 단순 100,000행 상향이 아니라 chunk/stream/background, timeout, 취소, 보존, 권한을 함께 검토한다.
- CurrentTableContext/query contract에는 query 조건, source identity, total row count, aggregate, provenance, capabilities를 포함하는 방향을 검증한다.

### 9/17 이후 - ERP 기능 확장
- 계약단가 -> 발주 -> 입출금 순서를 유지한다.
- 신규 기능마다 개별 대용량 우회 구현을 만들지 않고 공통 계약을 재사용한다.
- 사용자 명시기간, NLQ 기본기간, 권한, 회사 격리, display/full/current-table/export 분리를 유지한다.

## 3. 중기 상세 흐름

### 중기 흐름 A - 대용량 공통 기반 -> ERP 업무 확장
큰 흐름:
`대용량 공통 계약 확정 -> 계약단가 -> 발주 -> 입출금`

상세:
- 공통 query/analysis/export 계약을 먼저 확정한다.
- 계약단가에서는 거래처·품목·기간·적용처 등 업무 grain과 권한을 고정한다.
- 발주는 계약단가, 현재고, 재고부족, 매출예상 등 기존 deterministic 기준과 연결한다.
- 입출금은 회계 grain, 민감정보, 권한 범위를 먼저 확정하고 읽기 중심으로 시작한다.
- 모든 기능은 새 화면마다 별도 대용량 우회로를 만들지 않고 공통 Fast/Analysis/Export Path를 재사용한다.
- 완료 Gate는 1호기 회귀, 2호기 Smoke, 전체 조회조건 범위의 current-table 분석, export provenance까지 포함한다.

### 중기 흐름 B - 외부 지식·도구 운영화
큰 흐름:
`승인 API 명세 확보 -> 외부 MCP/OpenAPI 연결 -> 운영 보안/감사 -> 일반 Knowledge corpus 확대`

상세:
- 식약처·심평원 등 승인된 API의 endpoint, request/response, 이용조건을 먼저 확정한다.
- secret은 코드/문서에 넣지 않고 별도 보안 저장소 또는 환경설정으로 관리한다.
- outbound allowlist, timeout, retry=0, 감사로그, 장애격리를 갖춘 뒤 실서비스 Smoke를 수행한다.
- 일반 Knowledge는 소유부서·provenance·승인정책이 명확한 문서부터 확대한다.
- 도움말에는 실제 권한과 ACTIVE/APPROVED/freshness를 만족하는 기능·자료만 노출한다.
- MCP 외부 실연동과 Knowledge corpus 확대는 서로 병행할 수 있지만, 어느 쪽도 ERP 핵심 조회 경계를 우회하지 않는다.

## 4. 병행·조건부 일정

| 항목 | 일정 방식 | 착수 조건 |
| --- | --- | --- |
| 외부 MCP/OpenAPI | 자료 확보 시 별도 트랙 | 식약처·심평원 승인 API의 endpoint, request/response 명세, 이용조건 확인 |
| 일반 Knowledge corpus 확대 | 업무문서 준비 시 | 소유부서, provenance, 승인 가능성, GENERAL 분류가 명확한 문서 확보 |
| Embedding/vector retrieval | 조건부 보류 | corpus 확대 또는 lexical quality 한계가 재현될 때 |
| VLM/OCR, STT | 조건부 백로그 | 실제 업무 요구와 설치환경 지원이 확인될 때 개별 PoC |
| Tokenization/API Token/llmster/CLI | 운영 필요 시 | 2호기 운영 자동화·접근통제 필요성이 확인될 때 |
| Speculative Decoding/LM Link | 후순위 | 기능 안정화 후 생성속도/분산 필요성이 명확할 때 |

## 5. 일정 운영 Gate

1. NLQ 기본기간 계약은 유지한다. KPI 표시 7개월, 지원기간 포함 계산, 제품수불부 1주, 재고장/재고현황 기준월 1개월, 입고/출고/매입/매출 상세 기본 1일 정책을 임의로 축소하지 않는다.
2. 운영 질문은 미검수 로그 -> 사람 검수 -> 공식 사례집 -> 자동 회귀 fixture 순서로 승격한다.
3. parsed -> resolved -> query -> result 로그가 실제 predicate, metric, grain, 기간, source_call_count와 일치해야 한다.
4. 1호기 검증 -> 선택 stage -> commit/push -> 2호기 fetch/pull --ff-only -> process/health/smoke 순서를 유지한다.
5. DB write/DDL/index 변경, 외부 서비스 실제 연결, secret 변경은 별도 승인 없이 진행하지 않는다.
6. 완료 시 자동 Gate와 1·2호기 운영 Smoke 증적을 남긴다.

## 6. 예상 일정 변경 조건

- Snapshot 운영 창 확보가 늦어지면 9/5~9/6 작업은 다음 비업무시간으로 이동한다.
- 회사 4 성능 조사에서 근본적인 DB/원천 구조 변경이 필요하면 대용량 공통 설계보다 먼저 별도 승인 판단을 한다.
- 대용량 공통 계약이 9/16까지 확정되지 않으면 계약단가 착수를 미루며 임시 우회 구현을 만들지 않는다.
- 외부 MCP/OpenAPI는 승인 API 자료가 없으면 일정 지연으로 보지 않고 준비 대기 상태를 유지한다.
- Embedding, Fine-tuning, Agent는 재개 조건이 충족되지 않으면 일정에 강제로 넣지 않는다.

## 7. 장기 상세 흐름

### 장기 흐름 A - AI Data Platform -> ML 예측·추천
큰 흐름:
`공통 metric/grain/provenance -> AI Data Platform / KPI Library -> 검증 Dataset -> ML 예측·추천`

상세:
- ERP 원본과 집계값을 자동 대조할 수 있는 Validation Framework를 먼저 둔다.
- AI 집계/통계 구조와 KPI Library는 metric, grain, source version, 기준일을 명시한다.
- Feature Store나 별도 저장구조는 필요성이 확인되고 승인된 뒤 적용한다.
- ML은 deterministic baseline을 먼저 고정하고, 미래누수 없는 학습/검증 Dataset으로 비교한다.
- 매출예상, 재고부족, 발주추천 등은 근거·신뢰도·기준일과 함께 제공하며 자동 실행은 사용자 승인 없이 하지 않는다.
- 장기 목표는 Dashboard, Alert, 분석, 예측이 같은 metric/provenance 계약을 공유하는 것이다.

### 장기 흐름 B - Knowledge Center -> Agent/Workflow -> Service AI
큰 흐름:
`Knowledge Center 운영화 -> RAG/Tool 한계 검증 -> Fine-tuning·Agent/Workflow 조건부 도입 -> Service AI 확장`

상세:
- 일반 Knowledge corpus가 커지면 권한·버전·freshness·conflict를 운영하는 Knowledge Center로 발전시킨다.
- Embedding/vector retrieval은 lexical 품질 한계가 실제로 재현될 때만 도입한다.
- Fine-tuning/LoRA는 RAG, Tool, Structured Output으로 해결되지 않는 고정 능력 격차가 확인될 때만 검토한다.
- Agent/Workflow는 승인, 중복방지, timeout, retry 정책, 감사로그, 실패 복구를 갖춘 업무에 한해 단계적으로 적용한다.
- Service AI는 SIMS ERP 사용설명, 교육, 설치·업데이트, 고객지원, 개발지원, Groupware 업무지식으로 점진 확장한다.
- 장기 확장에서도 ERP 현재값은 deterministic SIMS 경로가 책임지고 LLM은 설명·추론·도구 선택을 담당한다.

## 8. 장기 Phase 흐름

`Phase 4 안정화·대용량 공통 기반`
-> `Phase 4.5 AI Data Platform / KPI Library`
-> `Phase 5 ML 예측·추천`
-> `Phase 6 Knowledge Center / 자체 RAG 검토`
-> `Phase 7 Fine-tuning·Agent·Workflow 조건부 검토`
-> `Phase 8 SIMS ERP·Groupware Service AI 점진 확장`
