# Dashboard Lite v0.1 Design

## 작성 목적

Dashboard Lite v0.1은 SIMS AI 사용자가 매출, 매입, 재고 상태를 긴 표보다 먼저 짧게 파악하고 오늘 처리할 일을 결정하도록 돕는 화면이다.

설계 원칙은 다음 순서로 고정한다.

```text
상태 -> 근거 -> 무엇을 해야 하나
```

v0.1은 기존 분석/KPI 서비스와 SIMS 표 결과를 재사용한다. SQL, 운영 데이터, 사용자 저장 schema, LLM 답변 정책을 한 번에 바꾸지 않고, 생산 코드가 계산한 결정론적 facts를 화면과 LLM이 함께 쓰는 구조를 목표로 한다.

## 사용자 역할

| 역할 | 주요 관심 |
|---|---|
| 회원사 일반 사용자 | 오늘 확인할 매출, 매입, 재고 이상 항목 |
| 회원사 관리자 | 담당자, 거래처, 제약사, 품목 단위의 조치 우선순위 |
| 플랫폼 운영 관리자 | 화면 동작, facts 품질, 데이터 품질 진단 |

v0.1은 회원사 업무 사용자 기준으로 설계한다. 플랫폼 운영 진단은 별도 expander 또는 로그로 분리한다.

## 화면 와이어프레임

```text
Dashboard Lite

[오늘의 업무 브리핑]
  상태: 매출/매입/재고 중 주의가 필요한 영역
  근거: 주요 KPI 3~5개
  조치: 오늘 확인할 항목 요약

[매출 그래프]
  완료월 매출추세
  당월 현재매출
  당월 예상매출
  집중도/기여도/고매출 감소 위험

[매입 그래프]
  최근 90일 매입 거래 회전일
  최근 매입 경과일
  반품 별도 표시
  매입 중단 또는 지연 후보

[재고 그래프]
  재고커버일
  수량/SKU/금액 재고준비율
  부족수량
  98% 침묵 정책 및 예외

[오늘의 조치 5~10건]
  우선순위, 이유, 연결 상세표

[기존 상세표로 이동]
  제약사별 매출 추세
  품목별 재고부족현황
  매입처별 재고부족 현황
  품목/거래처/영업사원/지역별 매출 예상
```

## 차트 목록과 선택 이유

현재 환경 확인 결과:

- 전역 Python: Streamlit 1.59.0
- 전역 Python: Altair 5.5.0
- 전역 Python: Plotly 미설치
- 프로젝트 `venv`: Streamlit/Altair/Plotly 미설치
- `requirements.txt`: `streamlit==1.59.0`

Streamlit은 Altair를 의존성으로 포함한다. 새 패키지 설치 없이 v0.1 구현이 가능하다.

권장 차트 라이브러리: Altair.

선택 이유:

- Streamlit 1.59 환경에서 별도 설치 없이 사용 가능
- line/bar/layer/rule 차트가 명확함
- 부분월, 예상치, 기준선, 임계값을 한 차트에 표현하기 쉬움
- Plotly보다 의존성 추가 부담이 낮음

기존 차트 사용 위치:

| 파일 | 사용 |
|---|---|
| `app/sims/views/dashboard.py` | `st.line_chart` |
| `app/ui/sims_panel.py` | `st.metric` |
| `app/ui/ssai_admin.py` | `st.metric` |

v0.1에서는 `st.metric`과 Altair chart를 조합한다.

## 지표별 계산식과 provenance

### 매출

| 지표 | 계산식 | provenance | grain | 비교 정책 |
|---|---|---|---|---|
| 완료월 매출추세 | 완료월별 매출 합계 | `analytics_*_sales_trend_service`의 완료월 월별 매출 | 월 | 완료월끼리 비교 가능 |
| 당월 현재매출 | 평가월/당월 현재 월매출 | `당월 현재매출` 또는 `월시점 실제매출` | 평가월 | 완료월 누계와 직접 비교 금지 |
| 당월 예상매출 | 예상기준값 * 보정증감률 | `_forecast_projection_from_row()` | 평가월 | 당월 현재매출과 진척률 계산 가능 |
| 예측 오차 | 당월 현재매출 - 당월 예상매출 | 서비스 meta 또는 facts | 평가월 | 예상 기준과 함께 표시 |
| 예측 기준 이탈 | 오차율 또는 진척률 임계 초과 | facts에서 계산 | 평가월 | 원인 단정 금지 |
| 상위 제약사 집중도 | 상위 N 매출 / 전체 매출 * 100 | 제약사별 요약표 | 제약사 | N과 기준 기간 명시 |
| 증가 기여 제약사 | 제약사별 증감액 상위 | 완료월 평균 대비 평가월 또는 최근기간 | 제약사 | 부분월이면 예상 기준 사용 |
| 감소 기여 제약사 | 제약사별 음의 증감액 상위 | 동일 | 제약사 | 업체 수 감소와 금액 감소 구분 |
| 고매출 감소 위험 | 매출 상위이면서 감소판정 또는 음의 기여 | 제약사 facts | 제약사 | 매출액과 판정 동시 근거 필요 |

### 매입

회전일 정의는 v0.1에서 고정한다.

```text
매입 거래 회전일 = 최근 90일의 정상 매입 고유 입고일 사이 평균 일수
매출 거래 회전일 = 최근 90일의 정상 매출 고유 출고일 사이 평균 일수
```

정책:

- 같은 날 여러 명세서는 1개 거래일
- 반품은 제외하고 별도 표시
- 거래일이 0~1개이면 자료부족
- 최근 거래 경과일과 거래 회전일은 별도 지표

| 지표 | 계산식 | provenance | grain | 비교 정책 |
|---|---|---|---|---|
| 최근 90일 매입 거래 회전일 | 정상 매입 고유 입고일 간격 평균 | Rddbc110 `Rd11_In_YyMmDd` 또는 정책상 `Rd11_Trans_YyMmDd` | 제품/매입처/제약사 | 반품 제외 |
| 최근 매입 경과일 | 기준일 - 마지막 정상 매입일 | Rddbc110 | 제품/매입처/제약사 | 회전일과 별도 |
| 반품 표시 | `Rd11_Io_Gu` prefix 1 계열 | Rddbc110 | 행/일 | 정상 거래와 분리 |
| 매입 중단 후보 | 최근 경과일 > 회전일 * 임계값 | facts | 제품/매입처 | 자료부족이면 판정 보류 |
| 매입 지연 후보 | 최근 경과일이 회전일 대비 초과 | facts | 제품/매입처 | 수요/재고와 함께 판단 |

### 재고

재고준비율 정책:

```text
준비가능수량 = min(max(현재재고, 0), 당월 잔여예상수요)
수량 준비율 = 준비가능수량 / 당월 잔여예상수요 * 100
잔여수요 0이면 100%
화면 상한 100%
98% 미만 알림
98% 이상 일반 알림 없음
99% 이상 정상 해제 후보
```

예외:

- 재고 0
- 음수 재고
- 핵심품목
- 커버일 부족
- 장부/실재고 불일치

| 지표 | 계산식 | provenance | grain | 비교 정책 |
|---|---|---|---|---|
| 재고커버일 | 재고커버월수 * 30 또는 별도 일수 기준 | `재고커버월수` | 제품 | 월/일 환산 기준 표시 |
| 수량 재고준비율 | 준비가능수량 / 잔여예상수요 | `현재재고수량`, `당월 잔여예상출고수량` | 제품 | 98% 임계값 |
| SKU 재고준비율 | 준비율 충족 SKU 수 / 수요 SKU 수 | facts | 제품 집합 | 수요 0 SKU 제외 여부 명시 |
| 금액 재고준비율 | 준비가능금액 / 잔여예상수요금액 | `재고평가단가`, 부족/잔여수요 | 제품/전체 | 단가 기준 명시 |
| 부족수량 | max(잔여예상수요 - 현재재고, 0) | `부족예상수량` | 제품 | 현재재고 음수 별도 표시 |
| 98% 침묵 정책 | 준비율 >= 98이면 일반 알림 숨김 | facts 상태 | 제품/전체 | 예외는 표시 |
| 정상 해제 후보 | 준비율 >= 99 | facts 상태 | 제품/전체 | 이력 저장소 없으면 v0.2 |

반복 억제와 2회 연속 정상 해제는 알림 이력 저장소가 필요하므로 v0.2로 둔다.

## 부분월 표시 정책

부분월은 완료월과 직접 비교하지 않는다.

표시 규칙:

- 완료월: 월 마감 데이터
- 당월/평가월: 현재까지 집계된 진행 데이터
- 당월 예상: 생산 코드가 계산한 월말 예상값
- 진척률: 현재 / 예상
- 완료월 누계와 당월 현재값을 직접 비교한 결론 금지

차트 표현:

- 완료월은 실선
- 당월 현재는 점 또는 강조 marker
- 당월 예상은 점선 또는 rule
- 부분월에는 `진행 중` badge 표시

## 상태, 임계값, 예외

| 상태 | 조건 |
|---|---|
| 정상 | 재고준비율 >= 98%, 고매출 감소 없음, 거래 경과 정상 |
| 주의 | 재고준비율 90~98%, 매출 감소판정 또는 거래 경과 증가 |
| 위험 | 재고준비율 < 90%, 부족수량/부족금액 발생, 고매출 감소 |
| 자료부족 | 거래일 0~1개, 완료월 부족, 예상 기준 없음 |

예외는 침묵 정책보다 우선한다.

- 재고 0 또는 음수
- 핵심품목
- 커버일 부족
- 장부/실재고 불일치
- 고매출 감소
- 반품주의

## Today's Actions 생성 규칙

오늘의 조치는 5~10건으로 제한한다.

우선순위:

1. 재고 위험: 부족수량/부족금액이 크고 준비율 < 98%
2. 고매출 감소: 상위 매출 제약사/품목 중 감소판정
3. 매입 지연: 최근 매입 경과일이 회전일 대비 초과
4. 매출 이상: 당월 진척률이 예상 범위를 크게 벗어남
5. 데이터 품질: 자료부족, 재고 음수, 장부/실재고 불일치

각 action은 다음 구조를 가진다.

```text
상태: 위험/주의/자료부족
근거: 지표명, 값, 기준
조치: 확인할 상세표와 필터
drill_down: action, params, table_key 후보
```

## 상세표 drill-down 연결

Dashboard Lite는 상세표를 대체하지 않는다.

연결 대상:

- 제약사별 매출 추세 분석
- 제약사별 매출 추세 분석 요약표
- 품목별 매출 추세 분석
- 품목별 매출 추세 요약표
- 품목별 매출 예상
- 매출처별/영업사원별/지역별 매출 예상
- 품목별 재고부족현황
- 매입처별 재고부족 현황

drill-down은 기존 SIMS 패널/채팅 결과 렌더링 경로를 재사용한다. v0.1에서 새 저장 schema를 만들지 않는다.

## SIMS_DASHBOARD_FACTS_V01 초안

```json
{
  "kind": "SIMS_DASHBOARD_FACTS_V01",
  "generated_at": "",
  "company_scope": "",
  "period": {
    "date_from": "",
    "date_to": "",
    "completed_months": [],
    "evaluation_month": "",
    "partial_period": true
  },
  "sales": {
    "totals": {},
    "monthly_series": [],
    "current_month": {},
    "forecast": {},
    "concentration": {},
    "growth_contributors": [],
    "decline_contributors": [],
    "high_sales_decline_risks": [],
    "comparison_rules": [],
    "forbidden_comparisons": []
  },
  "purchase": {
    "turnover_days_90d": [],
    "days_since_last_purchase": [],
    "returns": {},
    "delayed_candidates": [],
    "stopped_candidates": [],
    "data_quality": []
  },
  "inventory": {
    "coverage_days": [],
    "quantity_readiness": {},
    "sku_readiness": {},
    "amount_readiness": {},
    "shortage": {},
    "silent_policy": {
      "threshold_pct": 98,
      "normal_release_candidate_pct": 99,
      "history_required_for_repeat_suppression": true
    },
    "exceptions": []
  },
  "today_actions": []
}
```

## 자료부족과 데이터 품질 표시

자료부족은 오류가 아니다. 화면에서 다음처럼 표시한다.

- 회전일: 거래일 0~1개이면 자료부족
- 예상: 완료월/최근월 기준값이 없으면 자료부족
- 재고: 현재고 원천월이 없으면 자료부족
- 집중도: entity 수가 0이면 자료부족
- 기여도: 비교 기준 기간이 없으면 자료부족

데이터 품질 경고:

- 부분월 직접 비교 금지
- sample_records 기반 전체 판단 금지
- 업체 수 비중과 매출액 비중 혼용 금지
- 완료월 누계와 당월 현재값 혼용 금지

## 예상 수정 파일

| 파일 | 예상 역할 |
|---|---|
| `app/services/analytics_dashboard_lite_service.py` | Dashboard facts builder 신규 |
| `app/sims/views/dashboard_lite.py` | Dashboard Lite 화면 |
| `app/sims/views/analytics_views.py` | 메뉴/패널 연결 |
| `app/ui/sims_analysis_profiles.py` | Dashboard facts/profile 명확화 |
| `app/ui/chat_middleware.py` | LLM context에 dashboard facts 연결 시 |
| `tools/check_analytics_regression.py` | fixture 회귀 |

v0.1에서는 기존 분석/KPI 서비스 자체의 SQL을 최대한 재사용한다.

## 회귀 fixture

1. 완료월 3개월 + 당월 부분월 fixture
2. 완료월총매출과 당월 현재매출 직접 비교 금지
3. 당월 진척률 = 현재 / 예상
4. 제약사별 상위 5/10 집중도
5. 증가/감소 기여 제약사
6. 고매출 감소 위험 대상
7. 정상 매입 고유 입고일 0개, 1개, 3개
8. 정상 매출 고유 출고일 0개, 1개, 3개
9. 반품 포함/제외 분리
10. 재고준비율 잔여수요 0이면 100%
11. 현재재고 음수는 예외 표시
12. 준비율 97.9%는 알림, 98.0%는 일반 알림 없음
13. 99% 이상 정상 해제 후보
14. 핵심품목 예외
15. 장부/실재고 불일치 예외
16. 오늘의 조치 최대 10건
17. drill-down action/params 보존
18. sample_records만으로 KPI 계산하지 않음

## v0.1 포함 범위

- Dashboard Lite 단일 화면
- 매출/매입/재고 3개 섹션
- Altair 기반 line/bar/rule 차트
- Today’s actions 5~10건
- 기존 상세표 drill-down 연결
- 결정론적 facts 기반 KPI
- 부분월/자료부족 표시

## v0.1 제외 범위

- 알림 이력 저장소
- 반복 알림 억제
- 2회 연속 정상 해제 자동 처리
- 사용자별 dashboard layout 저장
- 운영 DB schema 변경
- 신규 패키지 설치
- Plotly 도입
- LLM이 raw DataFrame에서 KPI 직접 계산
- 09 명세 조회 LLM 계수/컬럼 의미 보정

## 구현 순서와 예상 작업량

1. `SIMS_DASHBOARD_FACTS_V01` builder 설계 확정: 0.5일
2. 매출 facts 구현: 1일
3. 매입 회전일 facts 구현: 1일
4. 재고준비율 facts 구현: 1일
5. Dashboard Lite 화면 구현: 1일
6. Today’s actions 생성: 0.5~1일
7. 회귀 fixture 및 py_compile: 1일
8. 1호기 수동 화면 검증: 0.5일

예상 총량: 5~6일.

## 구현 전 결정 필요 사항

- 매입 회전일 날짜 기준: 입고일자 `Rd11_In_YyMmDd` 우선 또는 명세서일자 `Rd11_Trans_YyMmDd` 우선
- 매출 회전일 날짜 기준: 출고일자 `Rd12_Out_YyMmDd` 우선 또는 명세서일자 `Rd12_Trans_YyMmDd` 우선
- 핵심품목 판정 기준
- 금액 재고준비율의 단가 기준
- 장부재고/실재고 기본값
- 98% 침묵 정책 예외의 우선순위
