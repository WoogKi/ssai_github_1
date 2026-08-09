---
title: "SIMS AI Platform 예상 일정 v1.2"
subtitle: "6c83962 안정 기준 이후 LM Studio 조기 확장과 Phase 4~8 예상 일정"
author: "김욱기 / SSART"
date: "2026-08-09"
version: "v1.2"
related_roadmap: "SIMS_AI_PLATFORM_MASTER_ROADMAP_v2.3_20260809.md"
---

# SIMS AI Platform 예상 일정 v1.2

- **기준일:** 2026-08-09
- **기준 브랜치:** `feat/dashboard-stock-extension-20260727`
- **기준 커밋:** `6c83962bc1b079fe440d56a313de536cf9490651`
- **배포 상태:** 1호기 commit/push 완료, 2호기 동일 커밋 적용 및 smoke 완료
**문서 성격:** 구현·검증 결과에 따라 조정되는 공식 예상 일정

관련 전체 방향은 [SIMS AI Platform Master Roadmap v2.3](SIMS_AI_PLATFORM_MASTER_ROADMAP_v2.3_20260809.md)을 따른다.
LM Studio 기능별 일정과 Gate는
[LM Studio Intelligence Extension Plan](../02_design/LM_STUDIO_INTELLIGENCE_EXTENSION_PLAN.md)에 연결한다.

---

## 1. 일정 원칙

1. 조사, 설계, 구현, 자동회귀, 1호기 수동검증, 2호기 적용·smoke 순서를 유지한다.
2. 예정일은 구현·검증 결과에 따라 조정할 수 있다.
3. 기능 의미, 조회 범위, 결과 정확성이나 보안 경계를 축소해 일정을 맞추지 않는다.
4. PoC 완료는 운영 채택을 뜻하지 않는다. 각 PoC는 지원 여부와 정확성,
   성능, 권한, 실패 처리, 운영 적용 여부를 판정한다.
5. LM Studio 기본 제공 기능을 먼저 검증하고 부족한 기능만 자체 구축한다.
6. DB 쓰기, DDL, index 변경과 외부 서비스 운영 연결은 별도 승인과 검증을 거친다.
7. 일정이 지연되면 후속 작업 시작일을 조정하고 미검증 기능을 완료로 표시하지 않는다.

---

## 2. 2026-08-09까지 완료

| 완료 시점 | 범위 | 완료 기준 |
|---|---|---|
| ~2026-08-09 | 현재고 안정화 | 최종 20열, 제조사·제품 code-IN, 복합조건, 원본·display 분리 |
| ~2026-08-09 | Dashboard 안정화 | deterministic NLQ, 명시조건 우선, facts 0건 no-data, source_call_count=3 |
| ~2026-08-09 | 현재표/NLQ 안정화 | 제품별 재고수량 TOP 7열, 최신 source binding, 판정·제조사 계약 |
| ~2026-08-09 | 제품재고현황 | 무라벨 통합검색 표시, 기존 OR-LIKE 의미와 상세/합계 계약 유지 |
| ~2026-08-09 | 배포 | `6c83962` commit/push, 2호기 동일 커밋 적용 및 smoke 완료 |

완료 범위에는 다음 후속 이슈가 포함되지 않는다.

- 제품재고장 무라벨 OR-LIKE 약 60초 성능 개선
- 2호기 Streamlit child-process lifecycle 안정화
- 무라벨 제품재고현황 `nlq.trace.parsed search_fields` 정합성
- LM Studio 지능 확장 기능의 운영 채택

---

## 3. 단기 실행 일정

| 기간 | 작업 | 핵심 검증 | 산출물/판정 |
|---|---|---|---|
| 2026-08-09 ~ 2026-08-10 | 2호기 Streamlit lifecycle 안정화 | ScheduledTask 중지, child process tree, 8501 PID, Health 200/ok, 재시작 로그 | 운영 runbook 후보와 smoke 결과 |
| 2026-08-11 ~ 2026-08-13 | 제품재고장 무라벨 OR-LIKE 60초 성능 조사·판정 | month_carry/last_cost, 결과 동등성, 기존 resolver 재사용, 인덱스 영향 | 적용/보류/별도 DB 개선 판정 |
| 2026-08-14 | LM Studio 1·2호기 읽기 전용 환경 조사 | 버전, 모델, API, RAG, VLM, OCR, STT, Tool, MCP, Structured Output, Embeddings | 기능 지원표와 PoC Gate |
| 2026-08-15 ~ 2026-08-16 | 문서 주입/RAG 1차 PoC | 한글 PDF/DOCX/TXT, 출처, 표, 회사·사용자 격리, 실패 안내 | 기본 기능 채택 가능성 |
| 2026-08-17 | 현재 날짜/시간 Tool PoC | 시스템 현재시각, timezone, 모델 기억과 분리, 감사 로그 | 읽기 Tool 계약 |
| 2026-08-18 ~ 2026-08-19 | 최신 뉴스/Web Search Tool PoC | 최신성, URL, 수집시각, 출처 신뢰도, timeout | 외부정보 사용 정책 |
| 2026-08-20 ~ 2026-08-21 | PDF/DOCX/TXT 첨부 분석 연결 조사 | 업로드 경계, 텍스트·표 추출, 권한, 용량, 보존정책 | 첨부 분석 설계 입력 |
| 2026-08-22 ~ 2026-08-23 | VLM/이미지/스캔/OCR PoC | 화면·문서 이미지, OCR 정확도, 표 구조, 민감정보 | VLM/OCR 적용 판정 |
| 2026-08-24 ~ 2026-08-25 | STT → SIMS 질문 PoC | 한글 업무용어, 사용자 확인, 오인식, 실행 전 확인 | 음성 입력 안전 계약 |
| 2026-08-26 ~ 2026-08-28 | NLQ 공식 사례집/RAG 업무자료 정리 | 중복, 기준일, 정답 contract, 민감정보, 문서 버전 | 승인된 평가·업무자료 후보 |
| 2026-08-29 ~ 2026-08-31 | Structured Output + Tool routing PoC | schema validation, action/params/status, fallback 차단 | deterministic routing 연계 판정 |
| 2026-09-01 ~ 2026-09-05 | Embeddings/자체 RAG 최소판 필요성 결정 | 기본 RAG 한계, 검색 품질, 비용, 권한, 버전관리 | 자체 구축/기본 사용/보류 결정 |
| 2026-09-06 ~ 2026-09-10 | Tool Use/MCP 통합 PoC | allowlist, 파라미터 검증, source_call_count, timeout, 감사 | 통합 운영 가능성 판정 |

### 3.1 PoC 공통 완료 기준

- 1호기와 2호기의 기능 차이를 기록한다.
- 지원 기능과 실제 업무 정확성을 구분한다.
- 성공, no-data, unsupported, timeout, 권한 차단을 재현한다.
- 외부정보와 문서 답변에 출처·기준일을 표시한다.
- 모델이 DB를 직접 쓰거나 미승인 Tool을 호출하지 않는다.
- 회귀와 수동 테스트 결과가 없으면 운영 적용으로 표시하지 않는다.

---

## 4. 2026-09 중순 이후 평가

| 영역 | 평가 내용 | 선행조건 |
|---|---|---|
| 운영 적용 범위 | PoC 중 실제 운영에 넣을 기능과 사용자 범위 | 정확성·권한·성능 Gate 통과 |
| speculative decoding | 응답시간 개선과 답변 품질·호환성 | 동일 prompt 품질 비교 |
| llmster / CLI | 배포·점검 자동화와 운영 안전성 | 명령 권한과 로그 정책 |
| API token | 1·2호기 인증, rotation, 비밀정보 저장 | token 원문 문서화 금지 |
| Fine-tuning / LoRA | RAG·Tool·Structured Output으로 해결되지 않는 고정 능력 | 정제 Dataset과 baseline 평가 |
| Agent / Workflow | 단계별 Tool 실행과 사용자 승인 | Tool allowlist와 감사 로그 |

Fine-tuning은 일정상 자동 착수하지 않는다. 변하는 ERP 값, 뉴스, 법령, 가격,
재고와 현재 날짜/시간은 학습 대상이 아니며 검증된 원천과 Tool로 제공한다.

---

## 5. 중장기 Phase 일정

| Phase | 예상 시기 | 목표 | 착수 Gate |
|---|---|---|---|
| Phase 4.x | 2026-08 ~ 2026-09 | LM Studio 기본 지능 기능 PoC와 운영 적용 판정 | `6c83962` 안정 기준 유지 |
| Phase 4.5 | 2026-09 이후 단계적 | Validation, 집계·통계 DB, Feature Store, KPI Library | 원천·metric·grain 계약 승인 |
| Phase 5 | Phase 4.5 기반 이후 | ML 매출예상, 재고부족, 발주추천, AI Score | deterministic baseline과 Dataset |
| Phase 6 | 기본 RAG 평가 이후 | chunking, embeddings, vector index, metadata/version, 권한 운영화 | 자체 RAG 필요성 확인 |
| Phase 7 | RAG/Tool 운영 이후 | Fine-tuning, Agent, Workflow, Feedback | 고정 능력 차이와 안전한 Tool 계약 |
| Phase 8 | 기능별 Gate 통과 후 | SIMS/Groupware 고객지원·교육·설치·개발 지원 | 서비스별 품질·보안·복구 기준 |

중장기 날짜는 단기 PoC 결과와 운영 우선순위에 따라 별도 개정한다. 일정 숫자보다
검증 가능한 완료 기준을 우선한다.

---

## 6. 일정 위험과 대응

| 위험 | 영향 | 대응 |
|---|---|---|
| 2호기 child process 잔존 | 구버전 프로세스와 포트 충돌 | process tree·PID·Health를 runbook과 smoke에 포함 |
| 무라벨 OR-LIKE 병목 | 제품재고장 응답 지연 | 의미 완전 동일성 확인 후 SQL/인덱스 후보 판정 |
| LM Studio 버전 차이 | 1·2호기 PoC 결과 불일치 | 환경 인벤토리를 PoC보다 먼저 수행 |
| 문서 OCR·표 손실 | 잘못된 RAG 근거 | 문서 유형별 추출 정확도와 원문 링크 평가 |
| Web 출처 오류 | 최신정보 오판 | 도메인 정책, URL, 수집시각, 다중 출처 확인 |
| Tool 오라우팅 | 잘못된 조회·실행 | Structured Output, allowlist, 사용자 확인, 감사 로그 |
| 자체 RAG 조기 구축 | 중복 개발과 운영부담 | LM Studio 기본 기능의 한계를 측정한 뒤 결정 |
| 학습 데이터 노후화 | ERP·뉴스·날짜 오답 | 변동 정보는 Tool/RAG로 제공하고 학습에서 제외 |

---

## 7. 일정 변경 규칙

- 시작일이나 종료일이 바뀌면 이유, 영향받는 후속 작업과 새 Gate를 기록한다.
- 기능 일부를 생략하거나 의미를 축소한 결과는 완료가 아니라 부분 완료로 기록한다.
- 운영 장애나 정확성 결함이 발견되면 신규 기능보다 안정화 작업을 우선한다.
- 일정 문서를 과거 결과에 맞춰 덮어쓰지 않고 새 버전으로 개정한다.
- 공식 기준 commit이 변경되면 Master Roadmap과 일정의 기준선을 함께 갱신한다.
