# SIMS AI Dashboard · KPI · NLQ 공통 조회조건 확정안 ver03

- **최초 작성일:** 2026-07-22
- **최종 개정일:** 2026-08-15
- **기준 브랜치/커밋:** `feat/dashboard-stock-extension-20260727` / `091876e`
- **상태:** 현재 운영 공식 정책
- **연계 문서:** [NLQ·현재고·현재표 계약](SIMS_NLQ_CURRENT_STOCK_CURRENT_TABLE_CONTRACT.md)

## 1. 목적과 적용 범위

Dashboard, KPI와 deterministic NLQ가 회사별 저장 profile을 공통 Default로
사용하되 질문이나 화면에서 명시한 조건을 최우선으로 적용하도록 기준을 정한다.
조건 이름이 비슷하더라도 ERP 역할과 지원 grouping이 다르면 서로 대체하지 않는다.

## 2. 조건 우선순위

```text
질문/화면의 명시 조건
  > 회사별 저장 profile
  > action별 안전한 Default
```

- 명시 조건은 같은 dimension의 profile 값을 대체한다.
- 명시하지 않은 dimension만 profile에서 가져온다.
- 명시한 값의 resolver가 실패하면 전체 범위로 fallback하지 않는다.
- action이 지원하지 않는 grouping을 유사한 grouping으로 바꾸지 않는다.
- 회사 변경 시 이전 회사의 profile, current table과 export cache를 재사용하지 않는다.

## 3. 기간 정책

기간은 action의 원천·grain·업무 의미에 따라 다르다. 하나의 기본기간을 모든
조회에 강제로 적용하지 않는다.

| 영역 | 기간 미지정 Default | 명시기간 | 비고 |
|---|---|---|---|
| Dashboard | 완료된 직전 6개월, 평가월은 현재 판단월 | 명시기간 우선 | 내부 추세 보조월은 표시 합계에서 제외 |
| KPI/Analytics | action별 월·일 정책 | 명시기간 우선 | 완료월·평가월·예상월 의미 보존 |
| 제품수불현황 | 기존 수불 action의 일자/월 범위 | 명시기간 우선 | 제품과 재고위치 조건 필요 |
| 제품재고현황 | 기준월/조회일에 따른 전월 누계+기간 입출고 | 명시 기준일 우선 | 재고장과 현재고 계약 구분 |
| 현재고 조회 | 현재월 시작일부터 기준일 현재까지 | 기준일 명시 시 우선 | 전월 누계+당월 상세 as-of |
| 입고·출고 상세 | action별 거래일자 범위 | 명시기간 우선 | 다운로드 예상/상한 meta 보존 |
| 현재표 후속질문 | 새 DB 기간을 만들지 않음 | 현재표 원본 범위 안에서만 적용 | 마지막 확정 full source 사용 |

Dashboard 화면의 `시작월`, `종료월`, `평가월` 관계는 다음과 같다.

- 시작월: 완료월 평균·추세를 계산할 첫 월
- 종료월: 완료 실적에 포함할 마지막 월
- 평가월: 현재매출·예상·재고위험의 판단월
- 기간 미지정 NLQ는 완료된 직전 6개월을 사용한다.
- 질문에 연도·월·기간이 있으면 기본 6개월보다 우선한다.

## 4. 공통 profile 조건

날짜를 제외한 조건과 표시기준은 회사별 profile에 코드값으로 저장한다.

| 구분 | 조건 | 선택 방식 | 적용 원칙 |
|---|---|---|---|
| 재고 | 재고기준 | 단일 | 실재고/장부재고 선택을 재고 facts 전체에 적용 |
| 재고 | 재고위치 | 복수 | 미명시 시 저장 코드 목록 사용 |
| 거래처 | 거래처그룹 | 복수 | 매출·출고 등 지원 action에 적용 |
| 거래처 | 거래처종류 | 복수 | 매출·출고 등 지원 action에 적용 |
| 제품 | 제품그룹 | 복수 | 제품 범위 공통 적용 |
| 제품 | 제품구분 | 복수 | 제품 범위 공통 적용 |
| 제품 | 제품분류 | 복수 | 제품 범위 공통 적용 |
| 입출고 | 입출고구분 | 복수 | 지원 action에 전체 Tcode로 적용 |
| 공급 | 대표 매입처 판정기간 | 숫자 | 저장 profile 또는 현재 설정값 |
| 위험 | 위험 분석기간 | 숫자 | 저장 profile 또는 현재 설정값 |
| 위험 | 과잉·저활성 기준 | 숫자 | 저장 profile 또는 현재 설정값 |
| 위험 | 준비율 경고기준 | 숫자 | 회사 profile 또는 현재 설정값, 문서 상수 아님 |
| 표시 | 위험품목 바로보기 건수 | 숫자 | 화면 표시범위, 원본 집계와 구분 |
| 표시 | 금액 표시 단위 | 단일 | 화면 단위만 변경, export 원 단위 유지 |

현재고 조회는 저장된 재고위치와 재고기준을 사용하지만 저장 `io_gu_list`는
적용하지 않는다. 현재고는 저장재고의 현재 상태이며, 이 예외를 다른 IO action에
확대하지 않는다.

## 5. 공급 역할 canonical 구분

| 역할 | canonical | 의미 | 대체 가능 여부 |
|---|---|---|---|
| 매입처 | `purchase_vendor` | 실제 매입·입고 거래처 | 발주처와 대체 금지 |
| 발주처 | `order_vendor` | 제품마스터 발주 기준 거래처 | 매입처와 대체 금지 |
| 제약사/제조사 | `manufacturer` | 제품의 제약사·제조사 범위 | 발주처와 독립 조건 |
| 공급처 | `supplier` | 질문의 일반 공급 주체 | canonical 의미 미확정 |

`supplier`는 질문 문맥만으로 매입처 또는 발주처로 자동 치환하지 않는다. 지원
action과 grouping을 하나로 확정할 수 없으면 `unsupported` 또는 `input_required`로
종료한다.

현재 공식 지원 조합:

- `stock_shortage × purchase_vendor`: 지원
- `stock_shortage × order_vendor`: 지원하지 않으면 `unsupported`
- `stock_shortage × supplier`: canonical 미확정이므로 `unsupported`

발주처 또는 공급처 질문을 매입처 결과로 fallback하는 것은 금지한다.

## 6. Dashboard 공급조건과 담당자

- 공급 mode 미지정 상태에서 담당자만 명시하면 `order_vendor`를 기본으로 한다.
- 질문에 제약사/제조사가 명시되면 `manufacturer` mode를 우선한다.
- 질문에 발주처가 명시되면 `order_vendor` mode를 우선한다.
- 제약사/제조사 조건과 발주처 조건은 별도 dimension이며 서로 덮어쓰지 않는다.
- 담당자 resolver는 확정된 mode 안에서 일치하는 전체 코드 범위를 사용한다.
- Dashboard에서는 후보 선택표와 `candidate_required`를 사용하지 않는다.
- resolver 성공 후 facts 유효행이 0건이면 `no_data`/tableless로 종료한다.

예:

| 질문 | 적용 |
|---|---|
| `SIMS 운영점검 담당자 신민우` | `supplier_mode=order_vendor` 기본 |
| `SIMS 운영점검 제약사 담당자 신민우` | `supplier_mode=manufacturer` |
| `SIMS 운영점검 발주처 담당자 신민우` | `supplier_mode=order_vendor` |
| `오늘의 경영점검 한미` | 무라벨 제약사 shorthand가 확정되면 manufacturer 범위 |

## 7. unsupported와 실행 meta

지원하지 않는 metric/grouping/action 조합은 다른 결과로 대체하지 않는다.

필수 meta:

```text
execution_status=unsupported
result_status=unsupported
requested_metric=<질문의 metric>
requested_grouping=<질문의 grouping>
source_call_count=0
table_created=false
```

- `requested_grouping`은 지원하지 않더라도 원문 계약을 보존한다.
- `column_unavailable`, `no_data`, `input_required`, `routing_error`와 구분한다.
- unsupported를 success 또는 no-data로 바꾸지 않는다.
- 신규 DB 조회와 LLM fallback으로 우회하지 않는다.

## 8. 현재표 후속질문

- 현재 회사·현재 방의 마지막 확정 full source를 사용한다.
- 화면의 일부 행, 반복값 공란 display copy와 compact snapshot을 원본으로 쓰지 않는다.
- 원본 current-table은 다음 성공한 일반 신규 표 조회 전까지 유지한다.
- 현재표 후속질문의 파생 표시·집계·필터·TOP 결과는 자체 full source를 보존할 수
  있지만 새 원본 current-table로 승격하지 않는다.
- 현재표 후속질문은 원본 full source를 사용하며 `source_call_count=0`을 유지한다.
- Dashboard payload(`data=None`)와 Dashboard local detail·필터·Excel은
  current-table을 교체하지 않는다.
- 실패, `no_data`, `unsupported`와 compact 렌더도 기존 current-table을 유지한다.
- 일반 신규 표 조회가 성공한 경우에만 해당 full source로 current-table을 교체한다.
- 부모표로 자동 fallback하지 않는다.
- current-table deterministic handler가 처리 가능한 질문은 DB와 LLM보다 우선한다.
- source table에 필요한 컬럼이 없으면 `column_unavailable`로 종료한다.

상세 계약은
[NLQ·현재고·현재표 계약](SIMS_NLQ_CURRENT_STOCK_CURRENT_TABLE_CONTRACT.md)을 따른다.

## 9. profile 저장과 권한

- 회사별 활성 Default profile은 하나만 사용한다.
- 명칭이 아니라 코드와 숫자 설정값을 저장한다.
- 일반 사용자는 조회조건을 임시 변경할 수 있으나 Default 저장 권한과 분리한다.
- 관리자 저장 후 같은 회사의 Dashboard/KPI/NLQ 초기값에 반영한다.
- action-change, 회사 변경, 로그인 변경 시 stale widget/profile 상태를 제거한다.

## 10. 완료 기준

- 명시조건이 같은 dimension의 Default보다 우선한다.
- 기간·vendor role·담당자·제품·재고조건이 서로 침범하지 않는다.
- 준비율 경고기준은 profile/current 설정값과 일치한다.
- 현재고의 `io_gu_list` 예외가 다른 action에 확산되지 않는다.
- unsupported 요청의 requested metric/grouping이 로그와 JSONL에 보존된다.
- 현재표는 다음 성공한 일반 신규 표 조회 전까지 마지막 확정 원본 full source를
  유지하며 파생 결과, Dashboard, 실패 상태와 compact 렌더로 교체하지 않는다.
- vendor role fallback과 회사 간 profile 혼입이 없다.
