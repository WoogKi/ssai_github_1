---
title: "SIMS NLQ·현재고·현재표 공식 계약"
date: "2026-08-15"
version: "v1.1"
status: "official-design"
baseline_branch: "feat/dashboard-stock-extension-20260727"
baseline_commit: "091876e0149c5db6bd093885aebb7fe1d9a8e7e2"
---

# SIMS NLQ·현재고·현재표 공식 계약

## 1. 목적

현재고 조회, 일반 제품재고현황과 현재표 후속질문의 action, resolver, 원본,
표시, 상태와 감사 계약을 한 문서로 고정한다.

관련 문서:

- [Dashboard 설계](DASHBOARD_LITE_V01_DESIGN.md)
- [공통 조회조건 ver03](DASHBOARD_KPI_NLQ_공통조회조건_확정안_ver03.md)
- [6c83962 공식 테스트 결과](../04_test_results/TEST_RESULT_6C83962_NLQ_DASHBOARD_STOCK_20260809.md)

## 2. 공통 원칙

- deterministic parser·resolver·handler가 표와 숫자를 확정한다.
- LLM은 확정 결과의 분석·설명 보조이며 계산, 재조회, 재라우팅을 대체하지 않는다.
- 화면 display, full source, current-table source와 export 원본을 구분한다.
- 명시 조건은 저장조건보다 우선하고 resolver 실패를 전체 범위로 바꾸지 않는다.
- 회사·채팅방이 다른 current table과 export cache를 재사용하지 않는다.

## 3. 현재고 조회

### 3.1 action과 조건

- canonical action: `현재고 조회`
- 제조사/제약사 또는 제품 검색어가 필요하다.
- 명시 제조사·제품은 기존 LIKE resolver로 코드 집합을 먼저 구한다.
- resolver 결과 코드를 서비스의 code-IN 조건으로 전달한다.
- 명시 제조사와 명시 제품이 함께 있으면 두 범위를 독립적으로 resolve한 뒤
  **AND** 조건으로 조회한다.
- 무라벨 단일어는 제조사 후보와 제품 후보를 각각 찾고 제품 범위를 OR
  union/deduplicate하여 조회한다.
- 후보 수가 여러 개여도 현재고에서는 후보 선택표를 표시하지 않는다.
- `candidate_required`는 제품수불현황에서만 허용한다.
- 두 resolver가 모두 0건이면 `no_data`로 종료한다.

### 3.2 저장조건과 재고 계산

- 재고위치 미지정 시 회사 profile의 저장 재고위치 목록을 사용한다.
- 재고기준 미지정 시 profile의 stock basis(실재고/장부재고)를 사용한다.
- 현재고는 저장 `io_gu_list`를 적용하지 않는다.
- 실재고/장부재고 계산의 기존 prefix 제외 규칙은 유지한다.
- 재고수량은 전월 누계와 당월 상세를 기준일 현재까지 반영하는 as-of 계산이다.
- 조회 범위를 줄이거나 화면 행을 줄여 재고 계산을 대신하지 않는다.

### 3.3 최종 20열

`6c83962`의 서비스 상수와 화면·CSV·Excel 계약 순서:

1. 순번
2. 제품코드
3. 제품명
4. 규격
5. 재고위치명
6. 재고수량
7. 현보험약가
8. 보험금액
9. 표준코드
10. 제품그룹명
11. 제품구분명
12. 제품분류명
13. 발주처코드
14. 발주처명
15. 제조사코드
16. 재고위치코드
17. KD코드
18. EDI코드
19. 제조사명
20. 포장단위

### 3.4 display와 원본 분리

- 같은 제품의 첫 위치행에는 제품정보 전체를 표시한다.
- 두 번째 이후 위치행은 위치와 수량 중심으로 표시하고 반복정보를 비운다.
- 위치가 둘 이상일 때만 화면에 제품합계 행을 표시한다.
- 반복 공란과 제품합계는 display copy에만 적용한다.
- full/current-table/export 원본의 제품정보와 숫자는 변경하지 않는다.
- 숫자 공란은 실제 0과 구분한다. 실제 데이터 0은 0으로 유지한다.
- 숫자열은 numeric dtype, 우측정렬과 천단위 표시를 유지한다.

## 4. 일반 제품재고현황

### 4.1 명시 조건

- 명시 `제조사명`은 제조사 조건으로 표시한다.
- 명시 `제품명`은 제품명 조건으로 표시한다.
- 명시 조건을 무라벨 통합검색으로 중복 표시하지 않는다.
- 기존 상세행과 최종 전체 합계 계약을 유지한다.
- 현재고 전용 제품합계 행을 일반 제품재고현황에 추가하지 않는다.

### 4.2 무라벨 통합검색

무라벨 검색어는 다음 의미의 OR-LIKE를 유지한다.

```text
매입처명 OR 발주처명 OR 제품명 OR 제조사명 LIKE <검색어>
```

화면 조회조건에는 특정 vendor나 제품으로 오해되지 않도록 다음처럼 표시한다.

```text
통합검색 <검색어>
```

### 4.3 알려진 성능 과제

`제품재고장 한미`는 의미와 결과는 정상이나 약 60초가 걸리는 후속 성능 과제다.
병목은 무라벨 OR-LIKE가 적용된 `month_carry`와 `last_cost` SQL에 집중된다.

읽기 전용 조사 결과:

- 단순 후보 code-IN 변환은 SQL Server expression limit 8632 위험이 확인되었다.
- 후보 CTE/semi-join 비교는 제한시간 안에 완료되지 않아 동등성과 개선을 입증하지 못했다.
- 결과 제품 집합과 수량·금액 완전 동등성이 입증되기 전에는 운영 SQL을 바꾸지 않는다.
- 성능 문제를 이유로 검색 역할 또는 결과 범위를 축소하지 않는다.

## 5. 현재표 후속질문

### 5.1 실행 우선순위와 원본

- 처리 가능한 질문은 deterministic current-table handler가 우선한다.
- 현재 회사·방의 마지막 확정 full/current-table source를 사용한다.
- display subtotal, 반복 공란, 화면 표본과 compact snapshot을 계산 원본으로 쓰지 않는다.
- 원본 current-table은 다음 성공한 일반 신규 표 조회 전까지 유지한다.
- 현재표 후속질문의 파생 표시·집계·필터·TOP 결과는 자체 full source를 보존할 수
  있지만 원본 current-table로 승격하지 않는다.
- 후속질문은 원본 full source만 사용하며 `source_call_count=0`을 유지한다.
- Dashboard payload(`data=None`)와 Dashboard local detail·필터·Excel은
  current-table을 교체하지 않는다.
- 실패, `no_data`, `unsupported`와 compact 렌더는 기존 current-table을 유지한다.
- 일반 신규 표 조회가 성공한 경우에만 해당 full source로 current-table을 교체한다.
- 필요한 컬럼이 없을 때 부모표로 자동 fallback하지 않는다.

### 5.2 현재고 제품별 재고수량 TOP

현재고 source의 `제품별 재고수량 TOP N`은 generic grouping보다 inventory 전용
handler가 우선한다.

출력 7열:

1. 순번
2. 제품코드
3. 제품명
4. 규격
5. 제조사명
6. 재고수량
7. 보험금액

- 제품합계 display 행에 의존하지 않고 full/current-table source를 제품별로 집계한다.
- 제품코드·제품명·규격·제조사명을 보존한다.
- 재고수량 내림차순과 기존 안정 동률 정렬을 사용한다.
- 한 제품을 상세행과 합계행으로 중복 합산하지 않는다.

### 5.3 결과 상태

| 상태 | 의미 | 표 생성 |
|---|---|---|
| `unsupported` | metric/grouping/action 미지원 | 없음 |
| `column_unavailable` | 현재 source에 필수 컬럼 없음 | 없음 |
| `no_data` | 유효한 조건이나 결과행 없음 | 없음 |
| `routing_error` | deterministic 처리 중 예외 | 없음, text payload 1회 |
| `success` | 계약에 맞는 결과 생성 | 계약에 따라 생성 |

상태를 다른 action, grouping 또는 LLM 표로 대체하지 않는다.

## 6. NLQ 로그와 감사

진단 단계:

```text
parsed -> resolved -> query -> result
```

| 단계 | 필수 확인 |
|---|---|
| parsed | canonical action, 명시 조건, 기간, grouping/metric |
| resolved | resolver 종류, 후보·코드 수, fallback 여부 |
| query | 실제 predicate 종류, source call, elapsed, rows |
| result | status, metric, grain, rows, table_created, notice |

현재 무라벨 제품재고현황에서 `nlq.trace.parsed search_fields=['physic_nm']`로
기록될 수 있으나 실제 실행은 4개 역할 OR-LIKE다. 이는 기능 결과가 아니라 로그
정합성 후속 과제로 관리한다.

결과행이 존재한다는 사실만으로 PASS 처리하지 않는다. 다음을 함께 검증한다.

- requested/result metric 일치
- requested grouping/result grain 일치
- 명시 조건과 resolved predicate 일치
- 요청 기간과 원천 기간 일치
- source_call_count와 추가 조회 여부
- table_created와 result_status 정합성

## 7. 회귀 Gate

- 현재고 20열과 순서
- 명시 제조사+제품 AND, 무라벨 제조사/제품 OR union
- 현재고 `candidate_required` 0건
- 저장 재고위치·stock basis와 `io_gu_list` 예외
- display/full/current-table/export 분리와 실제 0 보존
- 제품재고현황 무라벨 4-role 통합검색과 조건 라벨
- 현재표 inventory TOP 7열과 중복 없는 합계
- 후속 파생표 비승격, 다음 성공 신규 표에서만 원본 교체
- Dashboard·실패 상태·compact 렌더의 current-table 비변경
- 후속 `source_call_count=0`과 foreign company/room 차단
- 상태·metric·grouping·grain·기간 meta 정합성
