# 회사3 Snapshot 수동 Smoke - 2026-09-21

## 기준

- 회사3 `TEST_SIMS02`
- Snapshot: manifest 16 / generation 1 / `published / approved / ready`
- 평가월: Dashboard `202607`, operating Snapshot `202606`

## 실행 결과

| 기능 / 질의 | 결과 | 처리시간 | source call | 판정 |
| --- | ---: | ---: | ---: | --- |
| SIMS 일일점검 | 정상 | 20.426초 | 3 | Dashboard, 분포, heatmap 정상 |
| 현재고 출고빈도 A | 465건 | 6.286초 | 0 | Snapshot 등급 표시 |
| 현재고 제약사 베링거 | 93건 | 2.787초 | 0 | 제조사 routing 및 등급 표시 |
| 제품재고장 제약사 베링거 | 41건 | 3.997초 | 0 | 제조사 routing 및 Snapshot 필드 표시 |
| 제품정보 제약사 베링거 | 40건 | 17.063초 | ERP 1, Snapshot 1 | manifest/generation·lifecycle·등급 표시 |
| 제약사 베링거 발주계산 | 0건 성공 | 103.016초 | 10 | 발주필요대상 모드 정상 |
| 제약사 베링거 발주계산 조회구분 전체 | 40건 | 92.879초 | 10 | Snapshot 등급 전달 |
| 기본 발주계산 | 300건 | 352.247초 | 15 | 정상 완료, 성능 backlog |
| 품목별 매출 추세 분석 | 54,931건 | 137.087초 | 미기록 | 정상 완료, 성능 backlog |
| 품목별 매출 추세 요약 | 9,757건 | 116.477초 | 미기록 | 정상 완료, 성능 backlog |

Dashboard의 `Snapshot 행 없음`과 가격 stale은 [20680 단건 원인 보고서](snapshot_company3_20680_root_cause_20260921.md)를 따른다. 성능은 Snapshot 무결성 Gate와 별도로 관리한다.

## 결론

수동 Smoke는 PASS다. Snapshot core의 checksum, approval, projection 무결성과 별개로 발주·분석 경로의 장시간 처리는 후속 성능 backlog다.
