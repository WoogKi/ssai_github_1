# 2026년 9월 마감 Snapshot 운영 절차

## 1. 목적과 적용 범위

- **작업일:** 2026-10-03
- **대상:** 회사별 2026년 9월 마감 Dashboard 출고빈도 및 제품통계 Snapshot
- **작업 전 기준 commit:** `2a68d44dfda66998ab709dd675cbf379affcc03a`
- **대상 계약:** Snapshot v2.1 (`schema_version=2.1`, `algorithm_version=outbound_frequency_product_statistics_v3`)
- **실행 환경:** 이 문서는 이 저장소가 있는 **1호기**에서 `.\venv\Scripts\python.exe`로 실행한다. 2호기 실행 절차가 아니다.

이 문서는 `snapshot.manifest`와 `snapshot.frequency_product`의 관계형 Snapshot을 생성하고, 검증 후 승인하여 운영 조회가 읽는 상태로 전환하는 절차다. 회사별 결과, generation 번호, manifest 번호, checksum은 실행 전에는 알 수 없으므로 추정하지 않는다.

## 2. 작업 전 절대 금지 사항

- 현재 운영 Snapshot을 직접 수정하거나 삭제하지 않는다.
- 검증 전 `--approve`를 실행하지 않는다.
- 승인되지 않은 draft를 운영 결과로 해석하지 않는다.
- migration, DDL, DB write를 임의로 실행하지 않는다. migration은 승인된 별도 작업에서만 `--apply`를 사용할 수 있다.
- 회사, 평가월, 재고위치 scope, profile fingerprint, generation, manifest를 서로 섞지 않는다.
- 검증 실패, UI 이상, 예상 밖의 행수 변화가 있으면 다음 단계로 넘어가지 않는다.
- 일반 정기 작업에서 `--force`를 사용하지 않는다. 중복 draft 또는 복구는 별도 운영 승인 후 원인을 확인한다.

## 3. 현재 구조와 운영 계약

### 3.1 Snapshot v2.1

생성 대상은 `dashboard_inventory_frequency_snapshot`의 제품통계 Snapshot이다. 저장소는 관계형 표현(`relational_frequency_v1`)을 사용하며, 제품별 출고빈도와 제품통계 projection을 읽는다.

- 출고빈도 등급: `A`, `B`, `C`, `D`, `E`, `F`, `X`
- `F`: 신규품목
- `X`: 최근 정상출고 없음
- 자료 없음/알 수 없음은 등급 `X`로 임의 치환하지 않는다.
- 제품수명주기와 출고빈도, 품목손익, 품목기여 등급은 서로 다른 계약이다.

운영 reader는 승인된 정확한 key만 사용한다. 제품정보 조회는 승인된 v2.1 projection을 읽고, Dashboard·현재고·제품재고현황은 동일 projection을 사용해 출고빈도와 제품통계를 붙인다. key가 맞지 않거나 projection이 손상되면 fail-closed 한다.

### 3.2 migration 007 / 008 / 009

- migration `007_snapshot_profile_fingerprint`: `snapshot.manifest.profile_fingerprint`을 추가한다. v2.1 profile 일치성의 전제다.
- migration `008_frequency_product_statistics_extension`: `snapshot.frequency_product`에 3개월 출고·반품, 매입·매출 단가, 추정 손익·기여 관련 제품통계 컬럼을 추가한다.
- migration `009`: 품목손익·기여 등급 제약에 `X`를 허용하도록 확장한다.

이번 작업일에는 아래 inspect만 수행한다. 필요한 migration이 없으면 생성·승인·게시 작업을 중단하고 별도 승인된 migration 절차로 넘긴다.

### 3.3 Decimal, checksum, projection 계약

제품통계 Decimal은 `ROUND_HALF_UP`으로 SQL 저장 scale에 맞춰 정규화한다.

| 컬럼 | precision, scale |
| --- | --- |
| `return_supply_amount_3m` | `(38, 6)` |
| `avg_purchase_unit_cost` | `(38, 10)` |
| `avg_sales_unit_price` | `(38, 10)` |
| `estimated_unit_profit` | `(38, 10)` |
| `estimated_profit_rate` | `(38, 12)` |
| `estimated_contribution_amount` | `(38, 6)` |

정규화 뒤 0은 양수 0으로 통일하고, 0이 아닌 음수는 보존한다. 숫자 변환 불가, 무한값, precision overflow는 계약 오류다. 관계형 Snapshot에서는 payload checksum 여부가 아니라 row checksum, projection digest, relational whole checksum, exact inspection이 정합성 판단 기준이다.

## 4. 사전 점검

각 회사마다 아래 명령을 실행하고 결과를 작업 기록에 남긴다. `<회사ID>`, `<작업자>`, `<승인사유>` 등 꺾쇠 값은 실제 승인된 값으로 바꾼다.

### 4.1 기준 소스와 Python 환경

| 목적 | 명령 | PASS 기준 | 실패 시 조치 |
| --- | --- | --- | --- |
| 기준 commit 확인 | `git rev-parse HEAD` | 지정 기준 또는 사전에 승인된 배포 commit | 버전 차이를 확인하고 운영 책임자 승인 전 중단 |
| 변경 상태 확인 | `git status -sb` | 작업자가 이해한 변경만 존재 | 미확인 변경은 Snapshot 작업과 분리하고 중단 여부 판단 |
| Python 확인 | `.\\venv\\Scripts\\python.exe --version` | 프로젝트 venv가 정상 실행 | venv/의존성 문제를 해결할 때까지 중단 |
| 의존성 확인 | `.\\venv\\Scripts\\python.exe -m pip check` | 오류 없음 | 의존성 오류 해결 후 처음부터 사전 점검 |

### 4.2 회사, DB, migration, 기존 운영 상태

| 목적 | 명령 | PASS 기준 | 실패 시 조치 |
| --- | --- | --- | --- |
| 회사 DB 연결 확인 | `.\\venv\\Scripts\\python.exe tools\\check_company_db_connection.py --company-id <회사ID> --timeout 30` | 설정 DB, 회사 레코드, ERP engine, `SELECT 1`이 모두 성공 | `.env`와 회사 설정을 운영자 권한으로 확인. 비밀값을 로그·문서에 기록하지 않음 |
| migration 상태 확인 | `.\\venv\\Scripts\\python.exe tools\\ssai_analytics_snapshot_migrate.py --company-id <회사ID> --inspect` | 007, 008, 009 및 관련 선행 migration이 적용됨 | `--apply` 금지. 누락 migration과 영향 범위를 기록하고 별도 변경 승인 요청 |
| 기존 상태 확인 | 아래 **4.3 현재 상태 확인 경계**를 따른다. | 기존 published/approved generation, 남은 draft/pending 여부를 회사별 기록에 명시 | 값이 불명확하면 `실행 전 확인 필요`로 기록하고 Snapshot 책임자에게 generation/manifest 확인 요청 |

현재 문서만으로 확인된 회사별 운영 Snapshot 상태는 없다. 과거 문서의 특정 회사·월·generation 기록은 역사적 점검값일 뿐 2026년 9월의 현재 상태로 사용하지 않는다.

### 4.3 현재 operating / draft / pending 상태 확인 경계

현재 저장소에는 회사·평가월의 모든 manifest를 목록으로 반환하는 운영 CLI가 없다. `tools/inspect_approve_dashboard_inventory_frequency_snapshot.py`는 **이미 알고 있는** `company_id`, 평가월, 전체 stock code scope, contract version, profile fingerprint, generation과 기대값을 받아 `SqlServerSnapshotRepository.inspect_generation()`으로 한 generation을 읽기 전용 검사하는 도구다. 따라서 이 명령만으로 미지의 generation을 찾아내거나 모든 draft/pending을 나열할 수는 없다.

실행 당일에는 다음 순서를 따른다.

1. 직전 운영 작업 기록 또는 Snapshot 책임자가 제공한 정확한 scope, generation, checksum, 기대 행수/등급 분포를 확보한다.
2. 확보한 값으로 5.3의 `inspect_approve...` 명령을 **`--approve` 없이** 실행해 해당 generation의 `integrity_status`, `manifest_status`, `approval_status`, manifest/generation, checksum을 확인한다.
3. 직전 기록으로도 generation 또는 남은 draft/pending 여부를 특정할 수 없으면, 이 Runbook에서 명령을 새로 만들거나 생성 명령으로 상태를 탐색하지 않는다. 승인된 운영 DB 읽기 권한으로 Snapshot 책임자/엔지니어가 `SqlServerSnapshotRepository`의 읽기 전용 inspection 경로를 사용해 상태를 확인한 뒤, 그 결과를 회사별 작업 기록에 남긴다.
4. 상태가 불명확한 회사는 draft 생성·승인 단계로 진행하지 않는다.

자동 복구·rollback CLI는 현재 제공되지 않는다. `SqlServerSnapshotRepository`에는 내부 lifecycle 기능이 있으나, Runbook에서 운영자가 호출할 수 있는 자동 복구/이전 generation 복귀 명령은 없다. UI 이상 시에는 기존 published Snapshot을 직접 변경·삭제·무효화하지 않고, 원인 분석과 별도 승인 절차를 따른다.

### 4.4 CLI 옵션 대조표

| 도구 | 옵션 | 실제 형식/제약 | Runbook 사용 |
| --- | --- | --- | --- |
| `generate_dashboard_inventory_frequency_snapshot.py` | `--company-id` | 필수 양의 정수 | `<회사ID>` |
|  | `--evaluation-month` | 필수 문자열 | `202609` |
|  | `--dashboard-profile` | `store_true`, scope 선택지 중 하나 | 기본 운영 scope |
|  | `--stock-code` | `append`, 반복 가능, scope 선택지 중 하나 | 명시 scope일 때 반복 사용 |
|  | `--all-stock-locations` | `store_true`, scope 선택지 중 하나 | 승인된 전체 위치 scope일 때만 사용 |
|  | `--timeout-seconds` | 정수, 기본 120, 실행 시 1 이상으로 보정 | `120` |
|  | `--created-by` | 선택 문자열 | `<작업자>` |
|  | `--apply` | 없으면 dry run, 있으면 draft 생성 | 5.2에서만 사용 |
| `inspect_approve_dashboard_inventory_frequency_snapshot.py` | `--company-id` | 필수 양의 정수 | `<회사ID>` |
|  | `--evaluation-month` | 필수 문자열 | `202609` |
|  | `--stock-code` | 필수 `append`, scope 전체를 반복 지정 | `<코드1>`, `<코드2>` 등 |
|  | `--generation` | 필수 양의 정수 | 생성 결과의 generation |
|  | `--expected-checksum` | 필수 SHA-256 checksum 문자열 | 생성 결과 checksum |
|  | `--expected-product-count` | 필수 정수 | 생성 결과 product count |
|  | `--expected-normal-event-count` | 필수 정수 | 생성 결과 normal event count |
|  | `--expected-source-row-count` | 필수 정수 | 생성 결과 source row count |
|  | `--expected-grade-counts` | 필수 `F=n,A=n,B=n,C=n,D=n,E=n,X=n` 형식 | 생성 결과 grade counts |
|  | `--contract-version` | 필수, `1`/`2`/`2.1` 중 하나 | `2.1` |
|  | `--expected-profile-fingerprint` | v2/v2.1에서 64자리 소문자 SHA-256 hex 필수 | 생성 결과 profile fingerprint |
|  | `--approve` | 없으면 inspection만 수행 | 5.4에서만 사용 |
|  | `--approved-by` | `--approve` 시 비어 있으면 오류 | `<승인자>` |
|  | `--approval-reason` | `--approve` 시 비어 있으면 오류 | 승인 사유 |

`--preserve-generation`은 별도 draft/pending generation을 검사하는 선택 옵션이다. 이전 published generation을 보호하거나 rollback하는 옵션이 아니므로 정기 마감 명령에는 넣지 않는다.

## 5. 회사별 실제 작업 순서

아래 1~6을 한 회사의 확정된 Dashboard profile/재고위치 scope에서 끝낸 뒤 다음 회사로 진행한다. 생성·검증·승인에는 같은 `company_id`, `evaluation_month`, scope와 profile fingerprint를 사용한다.

### 5.1 Scope 확정과 dry run

**목적:** 회사의 Dashboard profile 또는 재고위치 scope를 확인하고 쓰기 전 입력값을 검토한다.

```powershell
.\venv\Scripts\python.exe tools\generate_dashboard_inventory_frequency_snapshot.py `
  --company-id <회사ID> `
  --evaluation-month 202609 `
  --dashboard-profile `
  --timeout-seconds 120 `
  --created-by <작업자>
```

명시된 재고위치로 실행해야 하면 `--dashboard-profile` 대신 실제 scope의 `--stock-code <코드>`를 반복하거나 `--all-stock-locations`을 사용한다. 임의로 두 방식을 섞지 않는다.

**기대 결과와 PASS 기준**

- 회사, 평가월, scope, profile fingerprint가 작업 대상과 일치한다.
- 제품 수, 정상 이벤트 수, source row 수, 등급 분포, checksum이 출력된다.
- ERP source diagnostics가 정상이며 계약 오류가 없다.

**실패 시 조치:** profile/scope/회사 설정을 다시 확인한다. source 진단 오류, 범위 불일치, 비정상 등급 분포는 원인 확인 전 생성하지 않는다.

### 5.2 draft 생성

**목적:** 검토된 동일 입력으로 `draft/pending` Snapshot 1건을 생성한다.

```powershell
.\venv\Scripts\python.exe tools\generate_dashboard_inventory_frequency_snapshot.py `
  --company-id <회사ID> `
  --evaluation-month 202609 `
  --dashboard-profile `
  --timeout-seconds 120 `
  --created-by <작업자> `
  --apply
```

**기대 결과와 PASS 기준**

- `generation_no`, `manifest_id`, checksum, product count, normal event count, source row count, grade counts, profile fingerprint를 작업 기록으로 옮긴다.
- 상태는 `draft`, 승인 상태는 `pending`이다.
- 이 시점의 operating reader는 새 generation을 읽지 않는다.

**실패 시 조치:** 같은 명령을 반복하지 않는다. generation guard 또는 중복 결과인지 확인하고, draft 상태/manifest를 확인한 뒤 운영 책임자와 처리 방향을 결정한다.

### 5.3 승인 전 exact inspection

**목적:** 생성 직후의 정확한 generation이 생성 출력과 동일한지 읽기 전용으로 검증한다.

```powershell
.\venv\Scripts\python.exe tools\inspect_approve_dashboard_inventory_frequency_snapshot.py `
  --company-id <회사ID> `
  --evaluation-month 202609 `
  --stock-code <코드1> `
  --stock-code <코드2> `
  --generation <generation_no> `
  --expected-checksum <checksum> `
  --expected-product-count <product_count> `
  --expected-normal-event-count <normal_event_count> `
  --expected-source-row-count <source_row_count> `
  --expected-grade-counts "F=<n>,A=<n>,B=<n>,C=<n>,D=<n>,E=<n>,X=<n>" `
  --contract-version 2.1 `
  --expected-profile-fingerprint <profile_fingerprint>
```

`--stock-code`는 dry run/생성 출력의 확정 scope 전체를 같은 순서와 값으로 넣는다. profile을 명령에 다시 쓰지 않고, 생성 결과에서 확인한 코드 scope를 사용한다.

**PASS 기준**

- 대상 generation은 `unapproved`, manifest는 `draft`, approval은 `pending`이다.
- checksum, product count, normal event count, source row count, A/B/C/D/E/F/X 등급 분포, profile fingerprint가 모두 생성 출력과 일치한다.
- projection digest, relational whole checksum, row checksum, exact inspection, Decimal 저장값 검증이 모두 성공한다.
- 손상·부분 projection·중복 제품·예상되지 않은 lifecycle 상태가 없다.

**실패 시 조치:** `--approve`를 실행하지 않는다. 입력 scope, profile fingerprint, Decimal 계약, source diagnostics, projection header와 generation을 이 순서로 확인한다. 기존 operating Snapshot은 유지한다.

### 5.4 승인과 운영 상태 전환

**목적:** 5.3과 같은 exact generation을 승인하고 운영 reader가 읽는 published 상태로 전환한다.

```powershell
.\venv\Scripts\python.exe tools\inspect_approve_dashboard_inventory_frequency_snapshot.py `
  --company-id <회사ID> `
  --evaluation-month 202609 `
  --stock-code <코드1> `
  --stock-code <코드2> `
  --generation <generation_no> `
  --expected-checksum <checksum> `
  --expected-product-count <product_count> `
  --expected-normal-event-count <normal_event_count> `
  --expected-source-row-count <source_row_count> `
  --expected-grade-counts "F=<n>,A=<n>,B=<n>,C=<n>,D=<n>,E=<n>,X=<n>" `
  --contract-version 2.1 `
  --expected-profile-fingerprint <profile_fingerprint> `
  --approve `
  --approved-by <승인자> `
  --approval-reason "2026년 9월 마감 검증 완료"
```

현재 lifecycle에는 별도 publish CLI가 없다. 위 `--approve`가 검증된 generation을 승인하고 published operating 상태로 전환한다.

**PASS 기준**

- 결과는 `status=ready`, `manifest_status=published`, `approval_status=approved`다.
- generation, manifest, checksum 및 모든 검증값이 5.3과 동일하다.
- operating reader가 새 generation을 `ready`로 읽는다.

**실패 시 조치:** 재시도·강제 게시하지 않는다. 검증 출력과 generation/manifest 상태를 보존하고, lock·중복 generation·기존 operating 상태를 확인한다. 이전 published Snapshot은 삭제하거나 수정하지 않는다.

### 5.5 게시 후 화면 확인

회사별로 승인된 같은 scope와 2026년 9월 기준으로 아래 화면을 조회한다.

| 화면 | 확인 내용 | PASS 기준 |
| --- | --- | --- |
| `SIMS 일일점검` | 출고빈도, 재고 위험, Snapshot 출처 상태 | 승인된 Snapshot이 `ready`로 붙고 결측/손상 경고가 없음 |
| `현재고 조회` | `출고빈도 A` 필터와 재고수량 | 승인 projection의 A 코드 범위와 화면 결과가 일관됨 |
| `제품재고현황 조회` | `출고빈도등급`, `품목손익등급`, `품목기여등급`, 재고 위치/수량 | Snapshot attachment와 기존 재고 집계가 함께 정상 |
| `제품정보 조회` | `출고빈도등급`, `품목손익등급`, `품목기여등급`, `제품수명주기` | 승인된 제품정보 Snapshot projection을 읽고 상태 오류가 없음 |

UI에서 `missing`, `corrupt`, profile/scope mismatch, 자료상태 오류가 나타나면 게시 완료로 판단하지 않는다. reader key(회사·월·scope·profile fingerprint), 승인 상태, projection 검사 결과를 다시 확인한다.

### 5.6 필수 성능 확인 3개

정확도 검증이 끝난 회사에만 수행한다. 고정 초수 합격선은 두지 않고, 작업 전 기준과 비교해 설명되지 않는 큰 회귀가 없는지 확인한다.

| 회사 | 조회 | 작업 전 결과 건수 | 작업 전 시간 | 작업 후 결과 건수 | 작업 후 시간 | 판정/비고 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
|  | 현재고 출고빈도 A |  |  |  |  |  |
|  | SIMS 일일점검 |  |  |  |  |  |
|  | 제품재고장 출고빈도 A |  |  |  |  |  |

결과 범위가 바뀌거나 성능 저하가 설명되지 않으면 source call, Snapshot attachment 상태, projection scope를 확인하고 배포 책임자에게 보고한다.

## 6. 중단과 복구 기준

다음 중 하나라도 발생하면 승인·운영 전환을 중단한다.

- checksum, projection digest, relational whole checksum, exact inspection 불일치
- Decimal 정규화·precision 오류 또는 signed zero/음수 처리 불일치
- profile fingerprint 또는 재고위치 scope 불일치
- A/B/C/D/E/F/X 합계가 제품 수와 맞지 않음
- 예상하지 않은 행수 변화, lifecycle/등급 상태 이상, source diagnostics 오류
- 기존 operating Snapshot이 새 draft에 의해 노출되었거나 훼손될 가능성
- 게시 후 Dashboard, 현재고, 제품재고현황, 제품정보 조회 이상

확인 순서는 다음과 같다.

1. 회사·평가월·재고위치 scope·profile fingerprint가 생성과 inspection에서 동일한지 확인한다.
2. generation과 manifest 상태가 draft/pending인지, 또는 approved/published인지 정확히 확인한다.
3. 생성 출력과 inspection의 행수, checksum, normal event count, source row count, grade count를 비교한다.
4. Decimal 오류면 입력값을 임의 변환하지 말고 해당 컬럼의 scale, 원천 값, overflow 오류를 확인한다.
5. projection 오류면 header, 제품코드 중복/누락, checksum과 reader key를 확인한다.
6. UI 오류면 승인 상태와 reader의 company/month/scope/profile key를 확인한다.

새 draft는 승인하지 않은 한 운영 reader에 노출되지 않는다. 현재 운영자용 자동 rollback/복구 CLI는 없으므로, UI 이상 시 현재 published generation을 직접 변경·삭제·무효화하거나 `--force`로 덮어쓰지 않는다. 기존 approved/published Snapshot을 보존한 채 company/month/scope/profile key와 inspection 결과를 확인하고, 복구는 원인 분석 후 별도 승인된 절차로 수행한다.

## 7. 회사별 작업 기록

작업마다 다음을 기록한다.

| 회사 | 평가월 | scope/stock code | profile fingerprint | 이전 operating generation | 새 manifest/generation | checksum | inspection | 승인/운영 상태 | UI 확인 | 성능 확인 | 작업자/승인자 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
|  | 202609 |  |  |  |  |  |  |  |  |  |  |

이 문서에는 실제 회사의 현재 상태를 채우지 않는다. 실행 당일의 inspect·생성·승인 결과만 기록한다.

## 8. 10월 3일 한 페이지 체크리스트

- [ ] 기준 commit과 작업 트리 상태를 확인했다.
- [ ] Python venv와 `pip check`를 확인했다.
- [ ] 회사 DB 연결을 읽기 전용으로 확인했다.
- [ ] migration inspect에서 007, 008, 009 및 선행 상태를 확인했다.
- [ ] 회사별 현재 operating/draft/pending 상태를 확인하고 기록했다.
- [ ] 기존 operating Snapshot을 보존한다는 점을 확인했다.
- [ ] 회사별 Dashboard profile 또는 재고위치 scope를 확정했다.
- [ ] dry run의 제품 수, source 수, 등급 분포, checksum, profile fingerprint를 검토했다.
- [ ] `--apply`로 draft/pending generation을 하나만 생성했다.
- [ ] 생성 출력의 manifest/generation과 모든 expected 값을 기록했다.
- [ ] 승인 전 inspection에서 행수, checksum, projection digest, relational whole checksum, exact inspection, Decimal, 등급, lifecycle을 모두 통과했다.
- [ ] 승인자와 승인 사유를 확인했다.
- [ ] `--approve` 결과가 `ready / published / approved`인지 확인했다.
- [ ] Dashboard를 확인했다.
- [ ] 현재고 출고빈도 A를 확인했다.
- [ ] 제품재고장 출고빈도 A를 확인했다.
- [ ] 제품정보 조회를 확인했다.
- [ ] 회사별 3개 성능 결과와 건수를 작업 전/후 표에 기록했다.
- [ ] 최종 manifest/generation/checksum/승인 상태를 작업 기록에 남겼다.

## 9. 오늘 수행하지 않는 작업

이 문서 작성일에는 Snapshot 생성, 승인, 운영 전환, migration 적용, DB write를 수행하지 않는다. 이 문서는 2026-10-03 운영 절차만 확정한다.
