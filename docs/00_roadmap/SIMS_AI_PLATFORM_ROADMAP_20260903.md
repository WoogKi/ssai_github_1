---
title: "SIMS AI Platform Roadmap"
subtitle: "2026-09-03 검증 상태 기준 통합 로드맵"
author: "김욱기 / SSART"
date: "2026-09-03"
version: "2026.09.03"
---

# SIMS AI Platform Roadmap

- **기준일:** 2026-09-03
- **기준 브랜치:** `feat/dashboard-stock-extension-20260727`
- **기준 커밋:** `ad609f71206081c26a551e57ca216b5fcb31f5b3`
- **배포 상태:** 1호기, GitHub, 2호기 동일 HEAD, 2호기 Health `200 / ok`
- **문서 성격:** 프로젝트 전체 일정과 우선순위를 관리하는 공식 최신본

이 문서는 과거 날짜 중심 계획을 현재 검증 상태에 맞춰 재구성한 통합 로드맵이다.
상세 구현 계약과 완료 증적은 설계 문서, 테스트 결과 및 Phase 마감 보고서에서
관리하며, 이 문서에는 완료 여부와 다음 착수 순서만 유지한다.

## 1. 일정 운영 원칙

1. 작업은 `완료 / 진행 / 다음 / 조건부 보류 / 백로그`로 구분한다.
2. 완료 판정은 자동 Gate, 운영 Smoke와 배포 증적을 기준으로 한다.
3. 불확실한 착수일을 만들지 않고 선행조건을 일정 기준으로 사용한다.
4. 기능 의미, 조회 범위, 권한 또는 provenance를 줄여 일정을 맞추지 않는다.
5. 운영시간에 부담이 큰 Snapshot 재생성 및 대용량 검증은 비업무시간에 수행한다.
6. 신규 ERP 기능은 공통 대용량 데이터 계약을 먼저 검토한 뒤 확장한다.

## A. 완료된 기반 작업

| 작업 | 상태 | 우선순위 | 착수 조건 또는 비고 |
| --- | --- | --- | --- |
| 인증·회사별 DB·사용자 저장소 격리 | 완료 | 운영 유지 | 회사 변경 시 current table, context, export cache 격리 유지 |
| Dashboard·Analytics·IO·Master NLQ 기반 | 완료 | 운영 유지 | deterministic 결과와 기존 회귀 Gate 유지 |
| NLQ 공식 사례집 185건 마감 | 완료 | 운영 유지 | PASS 183, 공식 `unsupported` 2, REVIEW/FAIL 0 기준 |
| RAG 승인자료와 provenance·permission 경계 | 완료 | 운영 유지 | 승인·분류·freshness 및 ERP 내부자료 no-leak 유지 |
| Structured Output + deterministic Tool Routing | 완료 | 운영 유지 | LLM route 선택 없이 schema와 route 계약 유지 |
| 날짜·시간 및 Web Tool 경계 | 완료 | 운영 유지 | 명시적 route, 출처, timeout, fail-closed 계약 유지 |
| Knowledge/RAG 제한 Chat | 완료 | 운영 유지 | `/knowledge`, `/knowledge-tech`, citation-bound 후속질문 완료 |
| Knowledge 권한관리 UI와 SIMS 도움말 | 완료 | 운영 유지 | effective permission readback 및 승인 corpus 기반 도움말 노출 |
| Project Source freshness | 완료 | 운영 유지 | v4 CURRENT, 1호기·2호기 운영 Smoke PASS |
| RAG lexical 품질 Gate | 완료 | 운영 유지 | 30/30 PASS, false positive 0 |
| MCP 기술 PoC | 완료 | 운영 유지 | mock, 공식 Python SDK, local STDIO handshake/discovery/read/lifecycle Gate 완료 |

Knowledge/RAG 마감의 상세 기준은
[2026-09-03 Knowledge/RAG 권한·도움말 마감](../01_phase_reports/20260903_Knowledge_RAG_권한_도움말_마감.md)을 따른다.

## B. 즉시 다음 작업

| 작업 | 상태 | 우선순위 | 착수 조건 또는 비고 |
| --- | --- | --- | --- |
| Snapshot 월초 재실행·전환·성능 비교 | 다음 | P0 | 주말 또는 비업무시간에 수행. 운영시간 전체 재생성 금지 |
| Snapshot 회사별 필수 성능 검증 | 다음 | P0 | `현재고 출고빈도 A`, `SIMS 일일점검`, `제품재고장 출고빈도 A` 세 경로 비교 |
| 회사 4 SIMS 일일점검 85초 병목 조사 | 다음 | P1 | 2026-09-03 운영 로그를 기준으로 단계별 elapsed와 기존 병목 조사 결과 연결 |
| 대용량 공통 처리 기반 설계 | 다음 | P1 | 입고 월 100,000행 이상 및 더 큰 매출 범위를 전제로 공통 계약부터 확정 |

Snapshot 작업은 재생성 성공만으로 마감하지 않는다. 회사별 세 경로에서 적용
전후의 결과 동등성, elapsed, source provenance와 fallback 여부를 함께 확인한다.

## C. 단기 후속

| 작업 | 상태 | 우선순위 | 착수 조건 또는 비고 |
| --- | --- | --- | --- |
| Display Fast Path 계약 | 준비 | P1 | 화면 표본과 전체 조회조건 범위를 분리하고 빠른 첫 표시 보장 |
| Analysis Path 계약 | 준비 | P1 | 현재표 분석이 화면 200행이 아닌 전체 조회조건 범위를 의미하도록 설계 |
| Export Path 계약 | 준비 | P1 | 100,000행 제한을 단순 상향하지 않고 chunk/stream/background 가능성 검토 |
| 공통 CurrentTableContext/query contract | 준비 | P1 | query 조건, source identity, 전체 범위 aggregate, provenance를 공통화 |
| 외부 MCP/OpenAPI 실서비스 연동 | 준비 대기 | P2 | 식약처·심평원 승인 API 명세 확인 후 endpoint, secret, allowlist, 권한, 운영 Smoke 진행 |
| WHOLESALE_READONLY 실제 로그인 Smoke | 운영 후속 | P2 | 실제 로그인 계정이 준비되면 권한 UI 및 Knowledge 비노출을 확인. 현재 blocker 아님 |

외부 MCP/OpenAPI는 완료된 MCP 기술 PoC와 별도다. 승인 API의 endpoint 및 이용조건이
확정되기 전에는 실서비스 연동 일정으로 승격하지 않는다.

## D. 중기 ERP 확장

| 작업 | 상태 | 우선순위 | 착수 조건 또는 비고 |
| --- | --- | --- | --- |
| 계약단가 | 자료 준비 중 | P2 | 관련 테이블 자료 확보와 대용량 공통 query/analysis/export 계약 확정 후 설계 |
| 발주 | 대기 | P2 | 계약단가 및 재고·예측 기준, 사용자 승인 경계가 정리된 뒤 착수 |
| 입출금 | 대기 | P3 | 계약단가·발주 다음 순서. 회계 grain과 권한·민감정보 범위 선확정 |

ERP 확장 순서는 `계약단가 -> 발주 -> 입출금`으로 유지한다. 각 기능은 개별 화면
최적화보다 공통 대용량 기반을 재사용하는 방향으로 설계한다.

## E. 조건부 재검토 및 백로그

| 작업 | 상태 | 우선순위 | 착수 조건 또는 비고 |
| --- | --- | --- | --- |
| Embedding / vector retrieval | 조건부 보류 | 조건부 | corpus 확대 또는 lexical Gate에서 재현 가능한 품질 한계가 확인될 때 재검토 |
| 일반 Knowledge corpus 확대 | 조건부 | P3 | 승인 가능한 일반 업무문서, provenance와 소유부서가 준비될 때 진행 |
| AI Data Platform·공통 KPI Library | 백로그 | 중기 | 대용량 공통 계약과 원천 metric/grain/version 기준 승인 후 구체화 |
| ML 예측·추천 고도화 | 백로그 | 중기 이후 | deterministic baseline과 검증 Dataset 확보 후 판단 |
| Agent·Workflow·Fine-tuning | 조건부 보류 | 장기 | RAG, Tool, Structured Output으로 해결되지 않는 반복 능력 차이가 입증될 때만 검토 |
| Service AI 확장 | 백로그 | 장기 | 기능별 품질·보안·복구 Gate 및 공식 문서 기반 확보 후 점진 확장 |

현재 lexical retrieval은 품질 Gate 30/30과 false positive 0을 충족한다. 따라서
Embedding은 구현 예정이 아니라 근거가 생길 때 다시 여는 조건부 항목이다.

## F. 운영 및 성능 후속

| 작업 | 상태 | 우선순위 | 착수 조건 또는 비고 |
| --- | --- | --- | --- |
| 회사 4 SIMS 일일점검 성능 | 조사 대기 | P1 | 약 85초 구간의 단계별 병목과 Snapshot 사용 여부를 근거로 판정 |
| Snapshot 운영 전환 | 작업 대기 | P0 | 비업무시간 재생성, 승인/latest-eligible 확인, 세 경로 동등성 Gate 통과 |
| 대용량 export 운영 방식 | 설계 대기 | P1 | 메모리, timeout, 취소, 보존, 다운로드 권한을 함께 검토 |
| Knowledge freshness 운영 확인 | 운영 유지 | P2 | source revision 변경 시 재추출·재승인·구버전 STALE 전환 확인 |
| 외부 MCP 운영 보안 | 연동 전 필수 | P2 | secret 저장, outbound allowlist, timeout, retry=0, 감사와 장애 격리 확정 |

## 2. 현재 우선순위 흐름

```text
Snapshot 비업무시간 재검증
  -> 회사 4 일일점검 성능 원인 확정
  -> 대용량 Display / Analysis / Export 공통 계약
  -> 계약단가
  -> 발주
  -> 입출금

승인 API 명세 확보
  -> 외부 MCP/OpenAPI 실서비스 연동

corpus 확대 또는 lexical 한계 확인
  -> Embedding 재검토
```

## 3. 완료 및 착수 판정 기준

- **완료:** 자동 Gate, 1호기 확인, 2호기 배포·Health·Smoke 증적이 갖춰진 상태
- **다음:** 선행조건이 명확하며 바로 조사 또는 실행할 수 있는 상태
- **준비:** 설계 입력은 있으나 선행 계약 또는 운영시간 확보가 필요한 상태
- **조건부 보류:** 현재 필요성이 낮고 재개 조건이 발생할 때만 착수하는 상태
- **백로그:** 장기 방향은 유지하지만 현재 착수 순서를 약속하지 않는 상태

## 4. 문서 관계

- 이 문서를 공식 최신 로드맵이자 일정 기준으로 사용한다.
- `SIMS_AI_PLATFORM_MASTER_ROADMAP_v2.3_20260809.md`와
  `SIMS_AI_PLATFORM_EXPECTED_SCHEDULE_v1.2_20260809.md`는 당시 계획의 역사자료로
  `docs/90_archive/roadmap_versions/`에 보관한다.
- Phase 완료 보고서와 테스트 결과는 당시 증적이므로 최신 상태로 덮어쓰지 않는다.
- 상세 날짜가 필요한 작업은 승인된 운영 창구와 선행조건이 확정된 뒤 별도 실행계획에 기록한다.
