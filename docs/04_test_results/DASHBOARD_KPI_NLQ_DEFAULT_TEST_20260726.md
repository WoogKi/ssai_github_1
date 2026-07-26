# Dashboard·KPI·NLQ Company Default Adapter 테스트 결과

- 테스트일: 2026-07-26
- 대상 브랜치: `feat/dashboard-default-adapter-20260726`
- 기준 커밋: `2fb459d55ec24573a9a87f9f91b12532c0abc2d7`
- 테스트 환경: 1호기 / 실제 회사 DB
- 최종 판정: 합격
- 후속 NLQ 흐름 과제: 별도 분리

## 1. 테스트 목적

- 회사별 Dashboard 공통 조회조건이 KPI와 NLQ에도 동일하게 적용되는지 확인한다.
- 회사 Default와 사용자가 질문에서 명시한 조건의 우선순위를 확인한다.
- 업무코드 Gcode/Tcode 및 `Gcode:Tcode` 복합코드가 문자열로 보존되는지 확인한다.
- 제품분류 `0031`과 특수관리제품 `0028` 계약이 분리되는지 확인한다.
- Dashboard, KPI, NLQ의 조건 해석이 동일한 의미로 적용되는지 확인한다.
- 회사별 profile 격리 및 재진입 적용을 확인한다.

## 2. 적용 범위

### Default 지원 action

- 품목별 매출 추세 분석
- 품목별 매출 추세 요약표
- 품목별 매출 예상
- 제약사별 매출 추세 분석
- 제약사별 매출 추세 분석 요약표
- 품목별 재고부족현황
- 매입처별 재고부족 현황

### Default 미적용 action

- 매출처별 매출 예상
- 영업사원별 매출 예상
- 지역별 매출 예상

## 3. 주요 구현 계약

### 3.1 회사 Default 우선순위

- 질문의 명시 조건은 회사 Default보다 우선한다.
- `전체`를 명시하면 해당 Default를 재적용하지 않는다.
- 사용자가 KPI widget 값을 바꾸면 같은 rerun에서 Default가 다시 덮어쓰지 않는다.
- 회사 변경 시 이전 회사 조건과 결과를 재사용하지 않고, 새 회사 Default를 분리 적용한다.

### 3.2 업무코드 문자열 계약

Gcode, Tcode, `Gcode:Tcode`를 모두 문자열 코드로 유지한다.

예시 코드:

- `0004`
- `0031`
- `01`
- `1`
- `J`
- `0031:01`

확정 규칙:

- `01`과 `1`은 서로 다른 코드다.
- `0031`과 `31`은 서로 다른 코드다.
- 숫자 변환을 하지 않는다.
- 선행 0을 보존한다.
- SQL bind까지 문자열로 전달한다.

### 3.3 제품분류 계약

| 구분 | 계약 | 원천 필드 | 의미 |
|---|---|---|---|
| legacy `product_class*` | Gcode `0028` | `Rd04_Physic_Gu` | 특수관리제품 |
| `dashboard_product_class_list` | Gcode `0031` | `Rd04_Physic_Tax_Gcode` / `Rd04_Physic_Tax` | 제품분류 |

회사 Default의 제품분류는 `0031` 계약만 사용한다. 부분 선택 예시는 다음과 같다.

```python
product_class_list = []
dashboard_product_class_list = [
    "0031:01",
    "0031:02",
    "0031:03",
    "0031:08",
]
```

이 분리로 `0028` 조건과 `0031` 조건이 동시에 생성되어 실제 조회가 0건이 되던 문제를 해소했다.

## 4. 자동 테스트 결과

| 검증 항목 | 결과 |
|---|---|
| `py_compile` | PASS |
| runtime import/helper | PASS |
| 업무코드 문자열 및 선행 0 보존 | PASS |
| `0031` detail predicate | PASS |
| `0031` monthly predicate | PASS |
| `0028` legacy predicate | PASS |
| fast-path pair 필터 보호 | PASS |
| Company Default adapter | PASS |
| Dashboard drill-down 배치 | PASS |
| analytics regression | 130/130 PASS |
| `pip check` | PASS |
| `git diff --check` | PASS, CRLF 안내만 존재 |

추가 ERP 원천 SQL은 없으며, DB/schema/data 변경도 없다. Dashboard `source_call_count=2` 계약도 유지한다.

## 5. 실제 회사 DB 테스트 결과

| 번호 | 사용자 질문 | 결과 건수 | 컬럼 수 | table_key | 판정 |
|---:|---|---:|---:|---|---|
| 1 | 품목별재고부족현황 조회 | 10,113 | 98 | 생성됨 | 합격 |
| 2 | 품목별매출추세요약 조회 | 10,113 | 43 | 생성됨 | 합격 |
| 3 | 품목별 매출 예상 조회 | 10,113 | 49 | 생성됨 | 합격 |

실제 로그 시간:

- 품목별 재고부족현황: 2026-07-26 22:05:28~22:05:53
- 품목별 매출 추세 요약표: 2026-07-26 22:06:27~22:06:40
- 품목별 매출 예상: 2026-07-26 22:06:55~22:07:13

세 테스트의 공통 최종 params는 다음과 같다.

```python
stock_cd_list = ["00001", "00247", "00901"]
product_di_list = ["1", "2", "3", "5", "6", "7", "J"]
dashboard_product_di_list = [
    "0004:1", "0004:2", "0004:3", "0004:5", "0004:6", "0004:7", "0004:J"
]
product_class_list = []
dashboard_product_class_list = ["0031:01", "0031:02", "0031:03", "0031:08"]
product_class = ""
product_class_nm = ""
product_class_nm_list = []
```

## 6. 성능 측정

다음 수치는 테스트 당시 1호기 실제 DB 기준이며, DB 상태·캐시·서버 부하에 따라 달라질 수 있다.

### 품목별 재고부족현황

- 매출 추세 원자료: 95,440건 / 약 7.838초
- 매출 추세 요약: 10,113건 / 약 11.646초
- 매출 예상: 10,113건 / 약 12.052초
- 재고부족 최종: 10,113건 / 약 18.922초

### 품목별 매출 추세 요약표

- 매출 추세 원자료: 95,440건 / 약 8.149초
- 요약 결과: 10,113건 / 약 3.463초
- 전체 실행 시간에는 사용자 체감 구간과 원천 조회 시간이 함께 포함된다.

### 품목별 매출 예상

- 매출 추세 원자료: 95,440건 / 약 10.575초
- 매출 추세 요약: 10,113건 / 약 17.108초
- 매출 예상 최종: 10,113건 / 약 17.838초

성능 최적화는 기능 정확성과 조건 계약 합격과 분리해 별도 후속 과제로 관리한다.

## 7. 문제 원인과 해결

### 7.1 초기 문제

NLQ params에 다음 값이 동시에 존재했다.

```python
product_class_list = ["01", "02", "03", "08"]
dashboard_product_class_list = ["0031:01", "0031:02", "0031:03", "0031:08"]
```

그 결과 `Rd04_Physic_Gu`와 `Rd04_Physic_Tax` 조건이 동시에 적용되어 실제 DB 조회가 0건이 되었다.

### 7.2 해결

- `0028` legacy 계약과 `0031` 제품분류 계약을 분리했다.
- 회사 Default 제품분류는 `dashboard_product_class_list`만 사용한다.
- legacy `product_class` alias는 비운다.
- fast path가 Dashboard pair 필터를 무시하지 않도록 보호한다.
- 필요한 경우 정확한 slow monthly 경로를 사용한다.

### 7.3 해결 확인

- sales trend rows: 95,440
- summary rows: 10,113
- forecast rows: 10,113
- stock shortage rows: 10,113
- `table_key` 정상 생성

## 8. 최종 합격 기준

- [x] 회사 Default가 KPI에 적용됨
- [x] 회사 Default가 NLQ에 적용됨
- [x] 명시 사용자 조건이 Default보다 우선됨
- [x] 전체 해제 시 Default가 재적용되지 않음
- [x] Gcode/Tcode 문자열 및 선행 0 유지
- [x] 제품분류 `0031`과 특수관리제품 `0028` 분리
- [x] Dashboard/KPI/NLQ 조건 의미 동등성 확인
- [x] 실제 회사 DB 조회 3건 성공
- [x] 결과 DataFrame 및 `table_key` 생성
- [x] 자동 회귀 130/130 PASS
- [x] 추가 ERP SQL 없음
- [x] DB/schema/data 변경 없음

최종 판정: **Dashboard·KPI·NLQ Company Default adapter 합격**

## 9. 별도 후속 과제

이번 기능 합격과 분리해 다음 NLQ 흐름을 별도 과제로 관리한다.

1. `제약사별 매출추세분석`
   - 독립 분석 action으로 정상 판정되어야 하나, 직전 품목별 매출 예상 문의의 후속 질문으로 처리될 수 있는 흐름이 관찰됐다.
2. `영업사원별 매출예산 조회`
   - `매출예산`을 `매출 예상`으로 자동 치환하지 않는다.
   - 자동 DB 조회하지 않고, 직전 표 후속질문으로 자동 처리하지 않는다.
   - NLQ 미일치 또는 사용자 의도 확인 정책이 필요하다.
3. `제약삽`
   - 불명확한 단일 오타이므로 `제약사별`로 자동 보정하지 않는다.
   - 직전 영업사원별 표를 자동 분석하지 않고, 이전 표 컨텍스트를 제외한 확인 질문 정책이 필요하다.

위 항목은 parser 자동 회귀 범위는 통과했지만, 실제 독립 입력과 채팅 흐름 문제를 포함하므로 이번 실제 DB 합격 표에는 포함하지 않는다.

## 10. Git 및 배포 상태

문서 작성 시점 상태:

- 브랜치: `feat/dashboard-default-adapter-20260726`
- `git add`: 미실행
- commit: 미실행
- push: 미실행
- 2호기 pull: 미실행

2호기 반영은 Dashboard 전체 작업 완료 후 main 병합, 안정 태그 생성, push, 2호기 pull, smoke test 순서로 별도 진행한다.
