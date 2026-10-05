# 202609 Snapshot 월마감 실행 및 검증 결과

작성/최종 정리일: 2026-10-06

Snapshot 실행일: 2026-10-05

대상 기준월: 2026-09

평가월: 202610

## 1. 실행 목적

- 2026년 9월 완료월 Snapshot을 `evaluation_month=202610`으로 생성했다.
- 출고빈도 기준월은 `202607 / 202608 / 202609`이며, `basis_from=20260701`, `basis_to=20260930`이다.
- 매입/매출가격 lookback은 `20251001~20260930`이다.
- 생성일 기준 lifecycle/current-stock/classification cutoff는 `20261005`이다. 완료월 말일과 혼동하지 않는다.
- 계약은 schema `2.1`, algorithm `outbound_frequency_product_statistics_v3`이다.
- 근거: `snapshot_202609_all_active_draft_20261005.json`, 회사별 draft/inspect JSON, `1st_ssai_test_20261006_081437.txt` (별도 비공개 실행 산출물).

## 2. R046 반영 내용

유효한 13자리 `880` 표준코드가 유효한 대표코드와 같은 제품은 낱알/소분 관리용으로 판단하여 KPI 분석, 발주, Snapshot `ProductUniverse`에서 제외한다. R046 행이 있다는 사실만으로 제외하지 않으며, 공란/무효 코드도 이 사유로 제외하지 않는다. Snapshot 생성 경로에서 기존 공통 관리용 제외 계약을 사용한다. R120 SQL, 출고 집계 의미, ERP 논리 source call 수(회사별 2회)는 변경하지 않았다.

## 3. company12 정합성 문제 및 수정

초기 draft의 `normal_positive_accepted_row_count=88062`와 최종 `normal_event_count=88058` 사이에 4건 차이가 있었다. 최종 `ProductUniverse`에서 제외된 R046 관리용 제품 4개에 속한 정상 출고 event 각 1건이 원인이었다. 원본 accepted 값은 source diagnostics에 유지하고, 최종 universe 밖 accepted event 4건을 `ignored_product_event_count`에 기록했다. 검증식은 `accepted normal - ignored_product_event_count = final normal_event_count`이다. Ignored event는 accepted의 부분집합이므로 source-row partition에 다시 더하지 않는다. R120 SQL, 등급, lifecycle, 가격, 손익의 업무 의미는 바꾸지 않았다. 실패한 company12 manifest 4 / generation 1은 변경하지 않은 pending 증거이며, 정상 검증 대상은 manifest 5 / generation 2이다.

## 4. 회사별 Snapshot 생성 결과

활성 roster는 `1,2,3,4,6,7,8,9,10,11,12,13,16` (13개)이다. 다음 표의 생성시간은 `draft_total_ms`를 초로 환산한 단일 실행값이다.

| company_id | manifest_id | generation_no | product_count | draft_total | exact inspection | R046 remaining | status |
|---:|---:|---:|---:|---:|---|---:|---|
| 1 | 6 | 1 | 292 | unavailable | PASS (official inspect recovery) | 0 | draft/pending |
| 2 | 18 | 1 | 691 | 55.295초 | PASS | 0 | draft/pending |
| 3 | 19 | 1 | 10,229 | 514.312초 | PASS | 0 | draft/pending |
| 4 | 11 | 1 | 10,185 | 214.628초 | PASS | 0 | draft/pending |
| 6 | 7 | 1 | 2,360 | 40.948초 | PASS | 0 | draft/pending |
| 7 | 10 | 1 | 16,162 | 368.929초 | PASS | 0 | draft/pending |
| 8 | 8 | 1 | 23,916 | 1,904.069초 | PASS | 0 | draft/pending |
| 9 | 5 | 1 | 7,533 | 379.593초 | PASS | 0 | draft/pending |
| 10 | 6 | 1 | 5,730 | 101.793초 | PASS | 0 | draft/pending |
| 11 | 6 | 1 | 6,966 | 180.681초 | PASS | 0 | draft/pending |
| 12 | 5 | 2 | 4,999 | 205.054초 | PASS | 0 | draft/pending |
| 13 | unavailable | unavailable | unavailable | unavailable | N/A (draft 0) | unavailable | ABORTED_FOR_MIDNIGHT_OPERATION_SAFETY |
| 16 | 4 | 1 | 21,386 | 887.656초 | PASS | 0 | draft/pending |

측정 가능한 11개 성공 draft의 `draft_total_ms` 합계는 **4,852.958초 (약 80분 53초)**이다. Apply 출력 decode 실패로 시간이 없는 company1과 draft가 없는 company13은 합산에서 제외했다. 회사8 R120 `fetch_rows=21,364,443`, 회사16 `fetch_rows=5,194,323`이며 대형 회사의 긴 실행시간은 별도 Snapshot 성능개선 과제다. 이 수치는 Dashboard 응답시간이 아니다.

## 5. Exact / Integrity 검증

12개 PASS 회사의 공식 inspect JSON에서 `contract_checksum_verified=true`, `integrity_status=unapproved`, `manifest_status=draft`, `approval_status=pending`이고 기대값 검사 실패가 0건이다. 제품 수, 정상 event 수, F/A/B/C/D/E/X 등급 합계, source partition, `accepted - ignored` 정합성과 관계형 checksum을 확인했다. 생성 직후 full v3 검사에서는 row checksum, projection digest, whole checksum, day/customer, paid quantity, return, 매입/매출가격, profitability, lifecycle/F, 분류 authority 및 Decimal canonicalization을 점검했다. 회사별 R046 관리용 최종 잔존은 모두 0건이다. 기존 `evaluation_month=202609` operating은 보존됐고 신규 pending draft가 operating으로 게시되지 않았다. 특히 회사2의 기존 schema 1.0/v1 operating은 유지한 채 새 schema 2.1/v3 draft를 만들었다.

## 6. 자동 Gate

| Gate | 결과 |
|---|---|
| `check_snapshot_product_statistics_extension.py` | PASS 15/15 |
| `check_snapshot_kpi_scope_alignment.py` | PASS 8/8 |
| `check_dashboard_inventory_frequency_snapshot.py` | PASS 17 |
| `check_ssai_analytics_snapshot_repository.py` | PASS 5 |
| `check_snapshot_draft_visibility_contract.py` | PASS 3 |
| `check_snapshot_approval_postcheck_stall.py` | PASS 3 |
| `check_rddbc046_order_exclusion_20261004.py` | PASS |
| `check_snapshot_frequency_lifecycle_extension.py` | PASS 11 |
| `py_compile`, `pip check`, `git diff --check` | PASS |

## 7. 작업 후 실제 기능 테스트

`1st_ssai_test_20261006_081437.txt`의 `nlq.case`, `chat.sims.push`, `sims.response_timing`에서 같은 요청의 회사, 결과, 행 수, 응답 완료시간을 대조했다. 아래 각 칸은 `결과/행 수/응답 초`이며 Dashboard는 표 행 수가 로그에 없어 `unavailable`로 표시한다. 여러 요청이 있는 경우 마지막 확인값을 적었다.

| 회사 | 발주 계산 | 현재고 조회 | 제품정보 조회 | 제품재고현황 조회 | SIMS 일일점검 |
|---:|---|---|---|---|---|
| 1 | 성공/1/1.862 | 성공/294/0.802 | 성공/294/0.415 | 성공/295/0.869 | 성공/unavailable/1.393 |
| 2 | 성공/9,663/15.982 | 성공/1,099/2.461 | 성공/9,808/9.667 | 성공/1,100/2.850 | no_data/0/5.793 |
| 3 | 성공/2,176/65.750 | 성공/13,395/32.612 | 성공/10,366/35.135 | 성공/8,992/67.510 | 성공/unavailable/18.300 |
| 4 | 성공/1,530/16.113 | 성공/14,009/6.190 | 성공/10,172/2.995 | 성공/9,496/10.697 | 성공/unavailable/30.333 |
| 6 | 성공/1,289/9.020 | 성공/1,347/2.136 | 성공/2,389/0.802 | 성공/1,348/5.848 | 성공/unavailable/8.591 |
| 7 | 성공/1,886/35.636 | 성공/14,922/10.162 | 성공/16,412/3.989 | 성공/14,922/14.529 | 성공/unavailable/40.745 |
| 8 | 성공/2,255/50.471 | 성공/81,018/39.649 | 성공/24,031/4.078 | 성공/22,476/55.558 | 성공/unavailable/69.297 |
| 9 | 성공/1,467/10.821 | 성공/9,097/32.791 | 성공/7,638/1.816 | 성공/6,662/15.322 | 성공/unavailable/19.906 |
| 10 | 성공/2,123/14.283 | 성공/4,794/4.983 | 성공/5,767/2.118 | 성공/4,835/6.051 | 성공/unavailable/14.452 |
| 11 | 성공/1,351/8.445 | 성공/11,729/3.259 | unavailable | 성공/6,254/3.227 | 성공/unavailable/16.766 |
| 12 | 성공/1,727/14.892 | 성공/3,876/27.267 | 성공/5,187/2.339 | 성공/3,898/15.043 | 성공/unavailable/18.807 |
| 16 | 성공/21,335/60.626 | 성공/11,087/24.245 | 성공/21,347/2.763 | 성공/11,126/39.572 | 성공/unavailable/43.469 |

회사2 일일점검의 `no_data`는 exception/failure가 아니다. 회사2 발주 계산에는 이 표와 별도로 성공/0행 요청도 있었다. 회사8은 다섯 기능 모두 실제 Chat/NLQ 경로에서 완료됐다. 이 Smoke는 **기존 approved operating Snapshot이 유지된 상태**의 프로그램 회귀 확인이며, 신규 202610 draft 게시 후 테스트가 아니다.

## 8. company13 처리

첫 apply가 장시간 실행됐고, 사후 repository에서 matching 202610 draft는 0건이었다. 회사13 DB 설정은 RCSI OFF / SNAPSHOT ISOLATION OFF였다. 실제 DB blocking은 권한 제한으로 확정하지 못했지만 자정 백업/배치 안전을 우선하여 leaf Python 프로세스만 종료했고 부모는 자연 종료했다. Snapshot/DB write, approve, publish는 하지 않았다. 다음 공휴일 또는 무사용 시간에 별도 승인과 확인을 거쳐 단독 생성한다.

## 9. 최종 상태

- 활성 회사 13개 중 202610 exact PASS draft 12개, 연기 1개(company13).
- 신규 draft는 모두 pending/unapproved이며 approve 0, publish 0이다.
- 기존 202609 operating Snapshot은 보존됐다.
- company13은 다음 공휴일 또는 무사용 시간의 별도 후속 작업으로 남긴다.
