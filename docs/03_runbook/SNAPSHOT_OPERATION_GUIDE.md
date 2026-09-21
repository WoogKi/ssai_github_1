# Snapshot 운영 가이드

## 적용 범위

이 문서는 회사별 Snapshot 생성, 검증, 승인, 게시 및 화면 Smoke의 표준 운영 절차다. Snapshot은 profile scope와 평가월을 기준으로 immutable relational row로 저장하며, operating reader는 승인·게시된 generation만 사용한다.

## 생성 전 확인

1. Dashboard profile의 재고코드, 제품그룹/구분/분류, stock mode, IO 구분을 확인한다.
2. profile fingerprint와 평가월을 기록한다.
3. 호스트 실행 경로에서 profile read-only와 company resolver `SELECT 1`을 각 1회 확인한다.
4. 실행 중 generation, draft 또는 generation lock이 있으면 재생성하지 않는다.

## 생성과 검증 순서

1. generation을 한 번 생성한다.
2. exact inspection을 실행한다.
3. row checksum mismatch=0, projection digest, relational contract checksum, whole checksum, profile fingerprint, source partition, grade total을 확인한다.
4. 모든 검증이 PASS일 때만 `approve_checked`를 실행한다.
5. post-approval exact inspection에서 `published / approved / ready`를 확인한다.
6. operating reader가 방금 승인한 generation을 선택하는지 확인한다.
7. Dashboard, 현재고, 제품재고장, 제품정보 및 필요한 NLQ를 최소 Smoke한다.

## 핵심 계약

- `ProductUniverse`는 R040 profile 조건을 최종 결과 기준으로 사용한다.
- Snapshot 후보는 profile 제품 중 현재재고, basis 정상입고, basis 정상출고 중 하나 이상의 증거가 있는 제품이다.
- R110의 FirstInbound는 lifetime 최초 정상입고 의미를 유지한다.
- R210/R220 MonthlyStock은 profile ProductUniverse를 집계 전에 선필터하지만 최종 ProductUniverse LEFT JOIN 계약은 유지한다.
- real/book mode, stock code, cutoff, IO 구분, 수량 산식, `current_stock_present`, source call 수는 변경하지 않는다.
- R120 Snapshot lifecycle과 Dashboard 상세 R120은 별도 경로다.
- operating reader는 최신 eligible approved Snapshot을 우선하며 계약 불일치 시 fail-closed 한다.

## 승인 전 필수 PASS

- row checksum mismatch = 0
- projection digest PASS
- whole checksum PASS
- relational contract checksum PASS
- exact inspection PASS
- profile fingerprint 일치
- grade total = product count
- source partition 합계 = source row count
- draft generation의 operating reader 노출 없음

관계형 representation에서는 `payload_json`이 없을 수 있으므로 `storage_checksum_verified=false`만으로 실패로 판정하지 않는다. 관계형 row/projection/contract/whole checksum을 authority로 사용한다.

## 화면 Smoke와 경고 처리

Dashboard에서는 결과 수, source call 수, 처리시간, 출고빈도 분포, 손익×기여 heatmap을 확인한다. 현재고·제품재고장은 등급 필터와 명시 제약사 routing을, 제품정보는 manifest/generation·Snapshot read·lifecycle·등급을 확인한다. 발주 계산은 0건 성공과 결과 존재 모드를 구분해 기록한다.

`Snapshot 행 없음`은 Dashboard 후보와 approved projection의 차집합이다. 발생하면 제품코드 차집합, R040/profile scope, 후보 증거(현재재고·basis 정상입고·basis 정상출고), CHAR/trim, projection 존재 여부를 단건으로 확인한다. 가격 stale은 freshness 정책 경고이며 Snapshot checksum/approval Gate와 분리한다. 성능 이슈 역시 승인 Gate와 별도로 backlog에 기록한다.

## 중단 조건

- checksum 또는 profile fingerprint 불일치
- exact inspection 또는 source partition 검증 실패
- projection header/행 수 불일치
- schema/migration, ERP SQL 의미, source call 수 변경 필요
- 기존 operating Snapshot 손상 위험

중단 시 승인·게시하지 않고 generation과 근거를 보존한다.

## 기록 항목

`generation_total_elapsed_ms`, ERP source별 elapsed, draft save elapsed, pre/post approval inspection elapsed, `approve_checked` elapsed, 생성부터 published까지의 총시간, manifest/generation/product count/grade/lifecycle/source row/smoke 결과를 회사별로 기록한다.
