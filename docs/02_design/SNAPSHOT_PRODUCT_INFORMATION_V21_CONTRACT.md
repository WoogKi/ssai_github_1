# Snapshot 2.1·제품정보 운영 조회 계약

- 버전: 1.0 / 기준일: 2026-09-14
- 기준 코드: e75fdc49872b03cc765207715d8bc118833cca9e 및 현재 조회 경로
- 분류: GLOBAL / ERP_DB_INTERNAL
- 성격: 현재 코드 계약. 회사별 운영본 생성·승인 완료를 일괄 주장하지 않는다.

## 코드 authority

`app/services/dashboard_inventory_frequency_snapshot.py`는 계산·검사·checksum,
`dashboard_inventory_frequency_snapshot_service.py`는 profile·생성·운영 읽기,
`ssai_snapshot_repository.py`와 SQL repository는 저장·운영본 선택,
`snapshot_product_information_service.py`는 제품정보 projection을 담당한다.
초안 생성, exact inspection, 승인/게시와 운영본 읽기는 구분한다.
읽기 요청에서 초안을 운영본으로 대신하거나 ERP 자동 재생성을 수행하지 않는다.

## 회사·profile·운영본

운영 읽기는 회사, 평가월, 재고 범위, 제품그룹·구분·분류와 재고모드의 저장 profile을
검증한다. profile fingerprint와 요청 범위가 불일치하면 임의 넓은 범위로 대체하지 않는다.
현재 읽기 키는 2.1, 2.0, 1.0 순으로 기존 운영본을 확인한다.
하위 버전 자료를 2.1의 추가 필드가 완비된 자료라고 설명하지 않는다.
manifest, generation, checksum과 운영 유효 기준을 함께 보존한다.
초안·과거 세대와 현재 운영본을 혼동하지 않는다.

## 출고빈도·수명주기

기존 출고빈도와 정상출고 건수·일수·거래처수 등 원 통계를 유지한다.
최초 정상입고월 기준 월령 0~2는 `new_product`이며 최종 등급 F를 사용하고
이전 빈도 등급도 보존한다. 3개월 이상은 `established`다.
입고 근거가 없으면 `unknown_lifecycle`, 잘못된/미래 월이면 `invalid_lifecycle`이다.
정상출고 없음 상태와 신규 F 판정을 별도로 보존한다. F를 수요 증가로 간주하지 않는다.

## 2.1 가격·손익·통계

3개월 출고수량, 유상출고 및 반품 건수·수량·공급가 등 기존 raw 통계를 보존한다.
평균 매입/매출 단가에는 기준월과 `ready`/`stale`/`unavailable` 상태가 붙는다.
단위이익 = 매출단가-매입단가, 이익률 = 단위이익/매출단가,
기여금액 = 단위이익×3개월 출고수량이다. 비율을 저장값과 화면 백분율로 혼동하지 않는다.
조정전용은 `excluded_adjustment_only`, 분류 충돌·자료부족은 `unavailable`로 처리한다.
손익/기여 등급 순위에는 `ready`만 사용한다. 오래된 가격을 정상 순위 근거로 승격하지 않는다.
Decimal 및 canonical checksum 계약을 화면 반올림이나 문자열 표시로 바꾸지 않는다.
평균가격은 발주 계약단가나 R230 최종 가격 후보와 다른 통계다.

## 제품정보 조회와 source 계약

제품정보는 운영 Snapshot projection에 회사별 제품 마스터를 제품코드로 결합한다.
Snapshot 미준비는 조회 오류/미준비로 반환하며 다른 회사나 초안 자료를 사용하지 않는다.
제조사·규격·보험코드·바코드·공통 제품조건과 lifecycle/등급 조건은 기존 경로를 따른다.
빈도·손익·통계를 현재 시점 실제재고라고 설명하지 않는다.
ERP 마스터 읽기와 Analytics Snapshot 읽기는 별도로 계측한다.
Snapshot generation은 R110 1회+R120 1회의 2-statement 계약,
Dashboard는 기존 `source_call_count=3` 계약을 유지한다.
제품정보 조회 source 수를 generation이나 Dashboard 수와 동일하다고 가정하지 않는다.

## 다른 문서와 경계

- [현재고·현재표](SIMS_NLQ_CURRENT_STOCK_CURRENT_TABLE_CONTRACT.md): 실시간 재고·원본·후속질문
- [Dashboard 설계](DASHBOARD_LITE_V01_DESIGN.md): 운영 적용 부분과 확장 설계 구분
- [마스터·가격](SIMS_MASTER_PRICE_QUERY_CONTRACT.md), [제품재고장](PRODUCT_INVENTORY_LEDGER_CONTRACT.md)
- [발주 계약](ORDER_CALCULATION_PHASE1_BUSINESS_CONTRACT.md): 수요·대표매입처·추천 수량 계산의 별도 기준

월별 분석마트 shadow 설계는 이 운영 읽기를 전환한 완료 근거가 아니다.
본 작업은 운영 Snapshot 데이터·schema·profile 변경을 수행하지 않는다.
