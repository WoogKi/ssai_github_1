# 회사3 Dashboard Snapshot 행 없음 - 제품 20680 원인 - 2026-09-21

## 기준

- 제품: `20680`, `(회수)가스트로그라핀/100ML`
- Dashboard 평가월: `202607`
- 선택된 operating Snapshot: `202606`, manifest 16 / generation 1

## 확인 결과

202607 Dashboard가 202606 Snapshot을 읽는 것은 latest-eligible completed-basis reader 계약에 따른 정상 동작이다. 20680은 R040 및 profile의 제품그룹 `0013:9998`, 제품구분 `0004:3`, 제품분류 `0031:01` 범위에 포함된다. CHAR/trim 불일치는 없다.

다만 Snapshot 후보는 profile 포함만으로 정해지지 않는다. 20680은 다음 세 증거가 모두 0이었다.

| 후보 증거 | 결과 |
| --- | --- |
| 선택 재고코드 R210 current stock | 99 source 행 합계 0 |
| 202603~202605 basis 정상입고 | 0건 |
| 202603~202605 basis 정상출고 | 0건 |

따라서 20680은 정상 lifecycle/product-statistics 후보집합 경계로 projection에서 제외됐다. 실제 Snapshot 누락이나 무결성 결함이 아니다.

Dashboard `current_stock_qty=0`은 202606 cutoff의 선택 재고코드 R210 기준이다. 수불 화면의 재고 6은 date-exact 수불 carry(이월 10 + 입고 7 - 출고 11) 기준이므로 원천·기준일·범위가 달라 직접 비교 대상이 아니다.

가격 stale 행 `30678` `망가나주3ml/10A`는 매입가 기준월 `202511`, 매출가 기준월 `202605`에 따른 정상 freshness 경고다.

## 결론

Snapshot core 결함 없음. `READY_FOR_DOC_PLACEMENT=YES`.

관련 문서: [회사3 최종 검증](snapshot_company3_final_review_20260921.md), [운영 가이드](../03_runbook/SNAPSHOT_OPERATION_GUIDE.md).
