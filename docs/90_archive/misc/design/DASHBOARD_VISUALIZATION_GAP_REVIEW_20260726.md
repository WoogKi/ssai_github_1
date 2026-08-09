# Dashboard 전체 마감 1단계: 설계 대비 구현 잔여 항목 조사

- 조사 일자: 2026-07-26
- 프로젝트: `LmStudion_project1`
- 조사 브랜치: `feat/dashboard-visualization-review-20260726`
- 조사 기준 HEAD: `1ad2c691f45c31cd4f094db21f38ba5ae25795c6`
- 성격: 읽기 전용. 소스, 설계 문서, DB/schema/data를 수정하지 않았다.

## 설계 기준 및 조사 범위

구현 기준 문서는 `DASHBOARD_LITE_V01_DESIGN.md`와 `DASHBOARD_KPI_NLQ_공통조회조건_확정안_ver03.md`만 사용했다. ver02는 구현 기준에서 제외했다.

조사 파일: `app/sims/views/dashboard_lite.py`, `app/services/dashboard_lite_facts.py`, `app/services/analytics_sales_trend_service.py`, `app/services/analytics_manufacturer_sales_trend_service.py`, `app/sims/views/analytics_views.py`, `app/ui/sims_panel.py`, `app/ui/chat_middleware.py`, `app/services/ssai_analysis_profile_service.py`, `tools/check_analytics_regression.py`.

## 설계 대비 구현 현황

| 설계 영역 | 세부 항목 | 현재 구현 | 판정 | 재사용 가능 코드/facts | 추가 facts 필요 | 시각화 필요 | 상세 연결 필요 | 추천 작업 단계 |
|---|---|---|---|---|---|---|---|---|
| 날짜 | 완료월/평가월/내부 3개월 | 기본 6완료월, 평가월 분리, 추세 보조월 분리 구현 | 충족 | `default_dashboard_lite_scope`, `_dashboard_internal_source_params` | 없음 | 유지 | 없음 | 유지 |
| 공통조건 | 재고·거래처·제품·입출고·임계값·표시단위 | Dashboard UI/정규화/프로필 저장 구현 | 대체로 충족 | `normalize_dashboard_lite_params`, profile service | KPI/NLQ 초기값 소비 연결 | 없음 | 없음 | 2단계 |
| 프로필 | 회사 Default, 관리자 저장 | 회사 단위 load/save와 권한 버튼 구현 | 부분 충족 | `ssai_analysis_profile_service` | `profile_name/is_default/is_active` 설계 메타, KPI/NLQ 적용 경로 | 없음 | 없음 | 2단계 |
| 업무 브리핑 | 전체 상태, 영역별 위험·주의, 최우선 조치 | KPI 카드와 재고 조치표 있음 | 부분 충족 | sales/inventory metrics, `today_actions` | 통합 상태/영역별 건수/최우선 근거 facts | 브리핑 카드 | 상세 대상 이동 | 1단계 |
| 매출 | 완료월 추세, 평가월 현재·예상·진척률 | 월별 실제·사전예상·당월 현재·당월 예상, KPI 구현 | facts 충족 / 표현 차이 | `_build_sales_facts`, chart rows, decline targets | 감소 기여도 기준을 명시한 drill-down params | 설계는 실선·marker·점선, 현재는 겹친 막대 | 제약사/품목 추세·매출예상 | 1단계 |
| 매입 | 입고 추세 | 월별 매입 최소 frame은 공통 bundle에 존재 | 부분 충족 | `purchase_vendor_df`, `get_dashboard_sales_source_bundle` | 월별 입고 facts/반품 분리 | 입고 추세 차트 | 입고명세 | 3단계 |
| 매입 | 대표 매입처 | 최근 6완료월 월집계로 품목당 1개 귀속, 매입처 위험 차트 구현 | 부분 충족 | `_attach_major_purchase_vendors`, vendor risk rows | 입력 일수 기준의 실제 일자 입고 facts, 발주처 fallback 명시 | 현재 매입처 위험 누적 막대 | 매입처별 재고부족 | 3단계 |
| 매입 | 최근 입고 경과일/회전일/입고 지연 | 구현 없음, turnover는 자료부족 안내 | 미구현 | 입고명세 화면/서비스 | `Rddbc110` 일자 기반 정상 입고일·거래일 간격·지연 후보 | 경과일/지연 표시 | 입고명세/재고부족 | 3단계 |
| 재고 | 실재고/장부재고 선택 | 선택 테이블을 사용해 준비율·부족·위험 계산 | 충족 | `_build_inventory_facts`, stock shortage service | 비교 조회용 두 기준 동시 facts | 현재 위험차트 유지 | 상세표 구현됨 | 유지 |
| 재고 | 준비율·부족·과잉/저활성·수요급증 | 4상태, 위험보정, 과잉 후보, 수요급증 세부 구현 | 대체로 충족 | inventory summaries/readiness/risk/detail rows | 재고커버일, 최근 출고일 기반 저활성 근거 | 요약·Top N·매입처 차트 구현 | 위험 상세·Excel 구현 | 1단계 보강 |
| 재고 | 장부/실재고 차이 | 선택 비교 조회 없음 | 미구현 | 단일 stock mode 흐름 | 두 재고 기준 동시 집계와 차이 data quality | 보조 지표 | 원장/상세 | 4단계 |
| 조치 | 재고 부족 5~10건 | 상위 10 재고 위험 actions 구현 | 부분 충족 | `_build_today_actions`, `risk_targets` | 명시 drill-down payload/공통 handoff | 표 유지 | 품목별 재고부족 자동 이동 | 1단계 |
| 조치 | 고매출 감소·입고지연·과잉·데이터품질 | 감소 targets facts는 있으나 action 우선순위 통합 미완성, 나머지는 미구현 | 부분/미구현 | `sales.decline_targets`, overstock summary, data_quality | 각 유형의 공통 action facts 및 우선순위 | 영역별 조치 카드 | 관련 상세 화면 | 3단계 |
| 상세 이동 | Dashboard에서 기존 KPI/SIMS 이동 | 대상 화면은 registry에 존재하지만 Dashboard 클릭 handoff 없음 | 미구현 | `sims_panel` action registry, analytics views | 공통 drill-down condition payload/라우팅 계약 | 링크/버튼 | 제약사 추세·매출예상·재고부족·매입처 재고부족·입고명세 | 1단계 |
| 채팅/결과 | 결과 1회 저장·compact snapshot·current primary | 최신 primary와 과거 snapshot 분리, 시간순 렌더 정책 보완 이력 존재 | 충족 전제(회귀 확인 필요) | chat middleware, dashboard renderer/snapshot | 없음 | 없음 | 없음 | 유지 |
| 성능 | facts 1회 생성·source 2회 | shared sales/purchase bundle + stock shortage 재사용, 성능 로그 구현 | 충족 | source bundle, raw DataFrame reuse, stock timing | 매입 일자 facts 추가 시 호출 계약 검증 | 없음 | 없음 | 모든 단계 |

## 빠진 facts

1. `오늘의 업무 브리핑`용 통합 상태, 영역별 위험/주의 수, 최우선 조치 근거.
2. 입고 월 추세, 정상/반품 분리, 마지막 정상 입고일, 최근 입고 경과일, 고유 거래일 간 평균 회전일, 입고 지연 후보.
3. 재고커버일, 최근 정상 출고일/경과일을 근거로 한 과잉·저활성 세부 facts.
4. 장부·실재고를 동시에 읽어 계산하는 비교 facts 및 data-quality 예외.
5. 조치별 공통 drill-down 조건 payload: action, 대상코드, 기간, 공통 Dashboard 조건.
6. KPI/NLQ가 회사 Default를 초기값으로 소비하는 공통 profile-adapter.

## 시각화 전환 대상

- 매출: 현재 비교 막대는 facts를 충분히 사용한다. 설계 기준의 완료월 실선/평가월 marker/예상 점선 전환은 **표현 변경만**으로 가능하다. 단, 이전 합의된 막대 정책과 설계 문서가 충돌하므로 사용자 결정 후 진행한다.
- 매입: 입고 추세와 최근 입고 경과일은 일자 기반 facts가 생긴 뒤 차트로 추가한다.
- 재고: 현재 준비율 Top N, 상태 카드, 주요 매입처별 누적 위험금액 차트, 수요급증 요약은 재사용 가능하다.
- 장부/실재고 차이: 두 기준 동시 facts 확보 후 보조 비교 차트를 별도 추가한다.

## 상세 연결 대상

- 매출 감소: `제약사별 매출 추세 분석`, `품목별 매출 추세 분석`, `품목별 매출 예상`.
- 재고 부족/과잉: `품목별 재고부족현황`.
- 주요 매입처 위험: `매입처별 재고부족 현황`.
- 입고 지연/회전: `입고명세 조회`.

대상 화면은 `app/ui/sims_panel.py` registry에 이미 있다. 다만 Dashboard facts의 대상 코드와 공통 조건을 안전하게 전달해 해당 화면을 열어 주는 router/handoff는 아직 없다.

## 성능·재사용 판단

- 현재 Dashboard는 `get_dashboard_sales_source_bundle()`의 sales/purchase bundle과 `get_stock_shortage_result()`를 재사용하여 정상 조회 기준 source_call_count=2 계약을 유지한다.
- 매출 facts, 예측, 재고부족 수요 산출은 shared sales DataFrame을 재사용한다.
- 주요 매입처 위험은 purchase vendor minimum frame을 pandas 집계로 처리한다.
- 입고 지연·입고 회전일은 현 월집계 purchase frame만으로는 정확히 만들 수 없다. `Rddbc110` 일자 기반 원천이 필요하며, 이를 새 SQL 호출로 만들지 또는 existing bundle에 안전하게 확장할지는 성능 설계 단계에서 결정해야 한다.
- 장부/실재고 차이도 현재 단일 stock mode 구조에서는 불가능하다. 별도 비교 옵션과 두 기준 집계가 필요하다.

## 문서 충돌·수정 후보

1. `DASHBOARD_LITE_V01_DESIGN.md`는 화면 이름이 v01이지만 문서 상태는 v0.2이다. 릴리스 명칭을 통일할 필요가 있다.
2. 두 기준 문서는 입출고구분을 첫 자리 `0/1/5/6`으로 설명한다. 현재 구현은 후속 확정 운영 정책에 따라 `Rddbc010`의 `0012` 전체 Tcode를 저장·바인딩한다. 이는 설계 문서와 구현이 다르므로 문서 갱신 또는 정책 재확인이 필요하다.
3. 설계는 매출 차트를 실선/marker/점선으로 규정하지만 현재 구현은 실제/예상을 겹친 막대로 표시한다. 이전 화면 정책과의 충돌이므로 사용자가 최종 표시 정책을 확정해야 한다.
4. 설계의 `SSAI_ANALYSIS_PROFILES` 컬럼 예시는 profile_name/is_default/is_active/audit columns를 포함하지만 현재 runtime은 company_id 한 건과 profile_json 중심으로 동작한다. 회사 단일 Default 정책의 기능은 충족하나 schema 명세는 실제와 다르다.
5. `DASHBOARD_LITE_V01_DESIGN.md` 4.2는 저장 목록/불러오기 미제공이라면서 9장에 “관리자 전용 저장·목록·Default”를 언급한다. ver03의 “목록/불러오기 미제공”을 우선으로 문구 정리가 필요하다.
6. 두 설계 문서는 대표 매입처를 입력 일수 기준의 실제 정상 입고수량으로 정의하지만 현재 주요 매입처 위험은 최근 6완료월 월집계 기준이다. 화면 안내는 이 차이를 이미 밝히며, 설계 기준에 맞추려면 일자 facts가 필요하다.

## 추천 구현 순서

1. **조치·드릴다운 정비:** 기존 sales/inventory facts를 이용해 재고 부족·매출 감소의 공통 action payload와 Dashboard→기존 SIMS handoff를 추가한다. 신규 SQL 없이 시작 가능하다.
2. **공통 Default 연결:** Dashboard profile을 KPI/NLQ 초기값 adapter로 연결하고, 명시 조건 우선/회사 격리 회귀를 추가한다.
3. **매입 일자 facts:** 정상 입고일·최근 입고 경과일·회전일·입고 지연 후보 및 입력 일수 기준 대표 매입처를 설계한다. source_call_count=2 유지 가능성/성능을 먼저 검증한다.
4. **재고 확장:** 재고커버일·저활성 상세 근거, 장부/실재고 비교 조회를 별도 옵션으로 도입한다.
5. **표현 정리:** 매출 차트 최종 정책 확정 후 전환하고 업무 브리핑/영역별 상태 요약을 추가한다.

## 회귀 및 수동 테스트 기준

- Dashboard 신규 조회: source_call_count=2, 동일 조건 UI 조작은 DB 재조회 0회.
- 모든 Dashboard 조건이 drill-down payload와 대상 상세 화면에 코드값으로 전달되는지 확인.
- 회사 Default가 Dashboard/KPI/NLQ에 동일 초기값으로 적용되고 NLQ 명시 조건이 우선하는지 확인.
- 실재고/장부재고 각각에서 준비율·위험·상세가 일관되는지 확인.
- 입고 일자 facts 추가 전후에 최근 입고일·회전일·반품 제외·자료부족 정책 확인.
- 장부/실재고 비교는 두 원천의 기준월·재고위치·제품 조건이 동일한지 확인.
- 차트 정책 확정 후 실제/예상·진행중 평가월·금액 단위/tooltip을 점검.

## Git 상태

- 소스 변경: 없음
- 설계 문서 변경: 없음
- DB/schema/data 변경: 없음
- git add / commit / push: 실행하지 않음
- 조사 전후 추적 파일 diff: 없음
- 제외 untracked 파일은 읽기/수정/삭제/staging/산출물 포함을 하지 않았다.
