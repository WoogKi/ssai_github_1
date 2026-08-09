---
title: "LM Studio Intelligence Extension Plan"
date: "2026-08-09"
version: "v1.0"
status: "official-design"
baseline_branch: "feat/dashboard-stock-extension-20260727"
baseline_commit: "6c83962bc1b079fe440d56a313de536cf9490651"
---

# LM Studio Intelligence Extension Plan

## 1. 목적

LM Studio가 현재 환경에서 기본 제공하는 기능을 먼저 검증하고, 업무 요구를
충족하지 못하는 부분만 SIMS AI에서 자체 구축한다. 처음부터 자체 RAG나 Vector
DB를 만들지 않으며, 조사·PoC·운영 채택을 구분한다.

관련 기준:

- [Master Roadmap v2.3](../00_roadmap/SIMS_AI_PLATFORM_MASTER_ROADMAP_v2.3_20260809.md)
- [Expected Schedule v1.2](../00_roadmap/SIMS_AI_PLATFORM_EXPECTED_SCHEDULE_v1.2_20260809.md)
- [SIMS AI 공통 운영 Runbook](../03_runbook/RUNBOOK_SIMSAI.md)

## 2. 핵심 원칙

1. LM Studio 기본 기능을 먼저 읽기 전용으로 조사하고 작은 PoC로 검증한다.
2. 특정 기능의 지원 여부는 1·2호기 버전, 모델과 API를 조사한 뒤 확정한다.
3. ERP 현재값은 기존 deterministic SIMS NLQ/DB 경로가 조회·계산한다.
4. LLM은 숫자 계산보다 질문 해석, 근거 설명과 허용된 도구 선택을 담당한다.
5. 날짜, 뉴스, 재고, 가격과 제품코드처럼 변하는 정보는 Fine-tuning하지 않는다.
6. RAG, Tool, Few-shot, Structured Output으로 해결 가능한 문제를 우선한다.
7. 회사·사용자 권한, 출처, 기준일, timeout과 실패 상태를 기능보다 먼저 정의한다.
8. PoC 성공만으로 운영 연결하지 않으며 DB 쓰기와 DDL은 별도 승인 없이는 금지한다.

## 3. 기능 분류와 PoC Gate

`LM Studio 기본 제공 여부`는 현재 문서 작성 시점의 확정값이 아니다. 2026-08-14
환경조사에서 1호기와 2호기를 각각 확인한다.

| 분류 | 기능명 | LM Studio 기본 제공 여부 | 1차 사용 방식 | SIMS AI 적용 목적 | 외부 추가 구축 가능성 | 우선순위 | PoC 합격 기준 |
|---|---|---|---|---|---|---:|---|
| A 문서 지식 | PDF/DOCX/TXT 입력 | 환경조사 필요 | 승인된 표본 파일 주입 | 문서 질의와 근거 확인 | DOCX·표 추출 보완 가능 | P0 | 한글 본문·표를 읽고 근거 섹션 제시 |
| A 문서 지식 | Chat with Documents | 환경조사 필요 | LM Studio UI/API 기능 확인 | 빠른 문서 대화 PoC | 권한·버전 계층 필요 가능 | P0 | 최신 문서 우선, 오래된 문서 충돌 탐지 |
| A 문서 지식 | 기본 RAG | 환경조사 필요 | 기본 검색·인용 기능 검증 | 사내 지식 검색 | 자체 index 필요 가능 | P0 | 정답·검색·인용·응답시간 Gate 통과 |
| A 문서 지식 | 프로젝트 문서/NLQ 사례 | 환경조사 후 연결 | 승인된 Markdown 묶음 | 코드·필드·업무규칙 설명 | ingestion manifest 가능 | P0 | 문서 버전과 근거 경로 보존 |
| B 실시간 | 현재 날짜/시간 | 모델 기억 사용 금지 | `get_current_datetime` Tool | 시간대가 정확한 현재시각 | 작은 로컬 Tool 필요 | P1 | Asia/Seoul 기준값·호출 로그 일치 |
| B 실시간 | Web/latest information | 환경조사 필요 | `web_search` 또는 승인 API | 최신 뉴스·외부정보 | 검색 provider 필요 가능 | P1 | URL·수집시각·기준일·timeout 표시 |
| B 실시간 | 외부 API | 환경조사 후 allowlist | 읽기 전용 Tool | 검증된 외부 데이터 | 인증·rate limit 계층 필요 | P2 | 허용 API만 호출하고 실패를 구분 |
| C 멀티모달 | 이미지 입력/VLM | 모델·API 조사 필요 | 화면·사진 표본 입력 | 화면·제품·문서 이해 | VLM 모델 선택 가능 | P2 | 한국어 설명과 객체·문맥 정확성 |
| C 멀티모달 | 스캔문서/OCR | 환경조사 필요 | 스캔 PDF·이미지 표본 | 문서 텍스트·표 복원 | OCR engine 필요 가능 | P2 | 한글·숫자·표 정확도와 민감정보 처리 |
| D 음성 | STT | 환경조사 후 확정 | 음성→텍스트→사용자 확인 | 음성 SIMS 질문 | 별도 STT 모델/API 가능 | P2 | 업무용어 인식 후 실행 전 확인 |
| E 구조화/도구 | Structured Output | 모델·API 조사 필요 | JSON schema 기반 응답 | action/params/status 안정화 | validator 필요 | P1 | schema 위반 차단과 오류 상태 보존 |
| E 구조화/도구 | Tool Use | 모델·API 조사 필요 | allowlist Tool 호출 | 시간·Web·문서·NLQ 연결 | routing/감사 계층 필요 | P1 | 미승인 Tool 0회, 파라미터 검증 통과 |
| E 구조화/도구 | MCP | 환경조사 필요 | 읽기 전용 서버 후보 연결 | 표준화된 도구·자료 접근 | MCP server 운영 가능 | P2 | 권한·timeout·감사·실패 격리 |
| E 구조화/도구 | Tool routing | SIMS AI 계약 필요 | deterministic router 우선 | 질문별 정확한 실행 경로 | 정책 엔진 보완 가능 | P1 | fallback 오라우팅 없이 1회 실행 |
| F 지식 심화 | Embeddings | 환경조사 필요 | 기본 embedding 품질 측정 | 한글·코드 검색 향상 | 별도 모델 가능 | P3 | 검색 평가셋에서 기본 RAG 대비 개선 |
| F 지식 심화 | 자체 RAG/Vector index | 기본 RAG 평가 후 판단 | 최소 index 후보 설계 | 권한·버전·metadata 운영 | 자체 저장소 필요 가능 | P3 | 기본 기능의 확인된 한계를 해결 |
| F 지식 심화 | Fine-tuning/LoRA | 환경조사·데이터 평가 필요 | 즉시 착수하지 않음 | 고정된 의도 해석 개선 | 학습 파이프라인 필요 | P4 | RAG/Tool로 해결 못 한 격차 입증 |
| F 지식 심화 | Agent/Workflow | Tool Gate 이후 | 승인 단계가 있는 workflow | 반복 업무 보조 | 상태·복구 계층 필요 | P4 | 사용자 승인과 재실행 안전성 확보 |

## 4. 문서 주입/RAG 최우선 PoC

문서 주입/RAG를 첫 번째 PoC로 수행한다. 목표는 자체 Vector DB 구축이 아니라
현재 LM Studio 기능으로 업무 문서를 얼마나 정확하고 안전하게 사용할 수 있는지
측정하는 것이다.

### 4.1 1차 문서 후보

1. SIMS AI Master Roadmap v2.3
2. Table/Field 설명서
3. 검증된 NLQ 공식 사례
4. Dashboard/KPI 정책
5. 공통 및 2호기 운영 Runbook

문서는 최신 공식 Markdown을 우선하며, archive와 당시 테스트 증적을 현재 정책과
섞지 않는다. 민감정보, 고객 데이터, DB 연결정보와 token은 입력하지 않는다.

### 4.2 평가 항목

| 평가 항목 | 확인 내용 |
|---|---|
| 정답 여부 | 질문의 핵심 사실과 정책이 공식 문서와 일치하는가 |
| 검색 적합성 | 관련 문서를 실제로 검색했는가 |
| 근거 정확성 | 문서명과 섹션이 답변 근거와 일치하는가 |
| 버전 충돌 | 오래된 버전보다 최신 공식 문서를 우선하는가 |
| 한글 검색 | 조사·띄어쓰기 변형에도 관련 문서를 찾는가 |
| 코드/필드명 | 영문 코드와 한글 필드명을 함께 찾는가 |
| 표 이해 | 행·열 관계와 단위를 훼손하지 않는가 |
| 응답시간 | 허용시간과 timeout 정책을 만족하는가 |
| context 사용량 | 문서 크기 대비 context가 통제되는가 |
| 권한 | 회사·사용자 범위를 벗어난 문서를 사용하지 않는가 |

### 4.3 판정

- **기본 기능 채택:** 정확성, 근거, 권한과 성능이 운영 Gate를 만족
- **보완 후 채택:** 추출·metadata·version 일부만 SIMS AI에서 보완
- **자체 RAG 검토:** 기본 기능으로 권한·버전·검색 품질을 충족하지 못함
- **보류:** 모델·환경·운영비용이 요구를 충족하지 못함

## 5. 실시간 정보와 Tool

후보 Tool:

| Tool | 입력 | 출력·감사 계약 |
|---|---|---|
| `get_current_datetime` | timezone | 현재시각, timezone, 호출시각 |
| `web_search` | query, 기간, 허용 도메인 | 제목, URL, 수집시각, 요약 |
| `search_sims_docs` | query, 문서 범위 | 문서 ID, 버전, 섹션, 근거 |
| `search_verified_examples` | 질문·action | 승인 사례와 expected contract |
| `run_sims_nlq` | canonical action과 검증 params | deterministic 결과와 provenance |

모든 Tool은 allowlist, schema 검증, timeout, 권한, 호출 횟수와 오류 상태를
기록한다. `run_sims_nlq`는 기존 SIMS router를 재사용하며 LLM이 SQL을 만들거나
재조회하지 않는다.

## 6. 질문 라우팅 목표

```text
현재 날짜/시간  -> Time Tool
최신 뉴스       -> Web Tool
사내 지식       -> RAG
첨부문서        -> Document/RAG
이미지/스캔     -> VLM/OCR
음성            -> STT -> 사용자 확인 -> 기존 SIMS NLQ
ERP 현재 데이터 -> deterministic SIMS NLQ/DB
일반 지식       -> LLM
```

경로를 확정할 수 없으면 임의 Tool이나 ERP action으로 대체하지 않고 명확한
질문 또는 unsupported 상태로 종료한다.

## 7. 멀티모달과 음성

- VLM 지원은 설치 모델과 API의 image input을 각각 확인한다.
- OCR은 스캔 품질, 한글, 숫자, 표, 개인정보 마스킹을 평가한다.
- STT 직접 지원 여부는 환경조사 후 확정한다.
- 음성 질문은 텍스트 변환 결과를 사용자에게 확인시킨 뒤 SIMS NLQ로 전달한다.
- 이미지와 음성 원본의 보존기간, 회사 격리와 삭제정책을 운영 연결 전에 정한다.

## 8. Structured Output, Tool Use와 MCP

Structured Output은 `action`, `params`, `result_status`, `notice`처럼 검증 가능한
계약을 만드는 데 사용한다. schema를 통과하지 않은 출력은 Tool에 전달하지 않는다.

Tool Use와 MCP는 다음 순서로 검증한다.

1. 읽기 전용 단일 Tool
2. 명시적인 Tool allowlist와 파라미터 validator
3. timeout/no-data/unsupported/권한 실패
4. 감사 로그와 호출 횟수
5. 사용자 승인 필요 동작 분리
6. 여러 Tool routing과 재시도·중복 실행 방지

LM Studio가 각 기능을 직접 제공하는지는 환경조사와 PoC 후 확정한다.

## 9. Fine-tuning과 LoRA 원칙

다음은 학습 대상이 아니다.

- 제품코드, 재고, 가격과 현재 ERP 값
- 현재 날짜와 시간
- 최신 뉴스와 변경되는 외부 정보
- 개정될 수 있는 운영 문서와 업무 정책 원문

검증된 NLQ 사례가 충분히 쌓인 뒤에도 업무 표현과 의도 해석 정확도에 고정적인
격차가 남을 때만 LoRA/Fine-tuning을 검토한다. 그 전에 RAG, Tool, Few-shot,
Structured Output과 parser 개선으로 해결 가능한지 입증한다.

## 10. 일정과 단계 Gate

| 기간 | 작업 | Gate |
|---|---|---|
| 2026-08-14 | 1·2호기 환경 읽기 전용 조사 | 기능 지원표와 버전 차이 확보 |
| 2026-08-15~16 | 문서 주입/RAG 1차 PoC | 근거·버전·한글·표·성능 평가 |
| 2026-08-17 | 날짜/시간 Tool | timezone과 감사 계약 |
| 2026-08-18~19 | Web/latest Tool | URL·수집시각·신뢰도 |
| 2026-08-20~21 | 첨부 분석 조사 | 파일·표·권한 경계 |
| 2026-08-22~23 | VLM/OCR | 이미지·스캔 적용 판정 |
| 2026-08-24~25 | STT | 사용자 확인 포함 음성 NLQ |
| 2026-08-26~28 | 공식 사례·업무자료 | 승인된 RAG 평가자료 |
| 2026-08-29~31 | Structured Output/Tool routing | schema와 오라우팅 차단 |
| 2026-09-01~05 | Embeddings/자체 RAG 판단 | 기본/자체/보류 판정 |
| 2026-09-06~10 | Tool Use/MCP 통합 PoC | 권한·감사·복구 Gate |

예정일은 구현·검증 결과에 따라 조정할 수 있으나 기능 의미를 축소해 일정을
맞추지 않는다.

## 11. 운영 채택 완료 기준

- 환경별 기능 지원 여부와 버전 차이가 문서화되어 있다.
- 기능별 정확성·성능·권한·실패 회귀가 있다.
- 문서와 외부정보에 출처·기준일이 표시된다.
- ERP 값은 deterministic SIMS 경로에서만 생성된다.
- 미승인 Tool 호출과 DB 쓰기가 없다.
- 회사·사용자 데이터 격리와 삭제정책이 검증된다.
- PoC 결과가 기본 채택, 보완, 자체 구축 또는 보류로 명시된다.
