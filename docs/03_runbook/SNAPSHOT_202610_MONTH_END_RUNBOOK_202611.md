# 2026-10 월마감 Snapshot 운영 Runbook (evaluation_month=202611)

## 범위와 불변 계약

- 대상은 2026-10 마감, 평가월 `202611`, Snapshot schema `2.1`/algorithm `outbound_frequency_product_v3+saved_io_avg_purchase_v1`이다. 정상 출고의 기준월은 `202608`~`202610`이다. 회사 3을 포함해 회사별 다른 신규 알고리즘을 적용하지 않는다.
- 기존 승인·게시본은 보존한다. 신규 Draft 생성, Exact Inspection, 별도 업무 승인, 공식 CLI 승인·게시 순서로 진행한다.
- 대상 회사는 마감 시점의 활성 상태와 회사별 ERP/Analytics 연결을 재확인해 확정한다. 비활성 회사는 연결·생성·승인하지 않는다. 202610 작업에서 회사16은 비활성이었다.
- 회사별 Dashboard 저장 프로필의 재고위치·제품그룹·제품구분·제품분류·입출고구분·장부/실재고 모드를 확인한다. 다른 회사 설정이나 `.env` 기본 ERP DB로 대체하지 않는다. `--saved-io-full` 경로는 저장된 IO 역할을 원천 SQL과 profile fingerprint에 반영한다. dry-run 출력만으로 실제 ERP 결과와 Exact 검증 완료를 주장하지 않는다.
- 저장된 `io_gu_list` **전체**가 회사별 선택 범위다. 회사13은 45개(0xx 8개, 1xx 5개, 5xx 24개, 6xx 8개)이며 0xx만을 전체 설정으로 축소하지 않는다. 입고·출고·반품·재고잔액·가격·등급은 같은 선택 목록을 참조하되 각자의 업무 산식을 구분한다. 선택됐다는 이유만으로 모든 코드를 가격 분자·분모에 더하지 않는다.
- 평균매입단가가 없어도 임의 계약단가나 마지막 매입가로 채우지 않는다. 제품정보 품질 현황을 승인 검토에 포함한다.

## 1. 사전점검

1. 작업 창구, 실제 운영자, DBA의 정기 백업 운영 연락체계, 배치 시간과 시간 상한을 확인한다. `SSAI_ANALYTICS` 백업은 DBA가 별도 장비에 보관하고 장애 시 복구도 전담한다. SIMS AI 작업자의 별도 백업·복원 시험은 Migration 선행 Gate가 아니다. 생성·승인은 회사별 순차 실행하고 자동 재시도하지 않는다.
2. Git HEAD/작업트리를 기록한다. 회사 활성 상태, 실제 ERP/Analytics DB 신원, Migration 001~010(월별 Fact)과 011(작업 이력) 적용 여부, 202611 manifest/generation 및 기존 202610 operating을 읽기 전용으로 확인한다. Migration/DDL은 이 절차에서 적용하지 않는다. 미적용 Migration은 별도 승인·검증 단계로 분리하고, 011이 없다면 작업 이력 기록이 누락됨을 명시한다. 회사 40처럼 Analytics에 Snapshot schema가 없으면 생성하지 않는다.
   Migration 별도 적용 단계에서는 저장/코드 Checksum과 권한, 공유 DB 영향, 기존 승인본 보존을 확인한다. 적용 전후 Schema·Reader·관리자 작업 이력을 검증하고 실패 시 Rollback 후 실제 상태를 확인한다. 백업 카탈로그 미조회나 작업자 복원 시험 미실시만으로 HOLD하지 않는다. 자세한 책임 분리는 [Snapshot 운영 가이드](SNAPSHOT_OPERATION_GUIDE.md)를 따른다.
3. Dashboard profile의 저장 재고위치, **입출고구분 전체 목록**, fingerprint를 확인한다. 202610 승인본의 scope를 무조건 복사하지 말고, 202611에 실제 적용할 프로필을 확정한다. 저장된 0xx 전체를 매입가격 후보 범위로 사용하되 실제 유상수량·공급가액·보정거래 적격성을 현행 공통 규칙으로 판정한다. 1xx/5xx/6xx도 각 업무 계산의 선택 범위와 현행 반품·출고 판정에 대조한다.
4. 공식 생성 프로세스·예약작업·동일 company/evaluation_month generation lock을 확인한다. 과거 잠금이 남아 있더라도 프로세스·예약작업·manifest 상태가 확인되기 전에는 파일을 옮기거나 삭제하지 않는다. 별도 승인하에 정확한 잠금 하나만 격리하고 원본 경로와 시각을 보존한다. SQL Server 세션을 임의로 KILL하지 않는다.
5. 생성 전 202611의 원천 기간을 확인한다. 출고 기준은 `20260801`~`20261031`, 매입단가 lookback은 코드의 `basis_from` 9개월 전 월초인 `20251101`~`20261031`이다. lifecycle/current-stock cutoff는 생성일과 평가월 말 중 이른 날이다. 회사 간 생성일 차이를 기록한다.

## 2. 공식 계획 확인과 Draft 생성

1호기 `venv`에서 회사별로 실행한다. `--apply` 없는 명령은 **계획·프로필·scope 확인용 dry-run**이다. ERP 건수, checksum 또는 생성 가능성을 실측하는 명령이 아니다.

```powershell
venv\Scripts\python.exe tools\generate_dashboard_inventory_frequency_snapshot.py --company-id <회사번호> --evaluation-month 202611 --dashboard-profile --saved-io-full --timeout-seconds 120
```

신원·scope·기존 상태와 작업 시간대를 확인하고, 저장 IO 역할·제품분류·가격 정책의 공통 Gate를 통과한 후에만 회사별 정확히 한 번 Draft를 생성한다. dry-run은 원천 조회나 checksum 검사가 아니다.

```powershell
venv\Scripts\python.exe tools\generate_dashboard_inventory_frequency_snapshot.py --company-id <회사번호> --evaluation-month 202611 --dashboard-profile --saved-io-full --timeout-seconds 120 --apply
```

- 생성 CLI는 ERP 원천 2개 질의를 계획하며 질의별 timeout은 120초, 자동 재시도는 0이다. 프로세스 전체 시간 상한도 별도로 정한다.
- PID, 시작/종료 시각, 단계별 시간, source call 수, manifest/generation, 제품·정상 이벤트·source row·등급별 건수, 전체 checksum, profile fingerprint를 저장한다.
- routine 작업에서 `--force`를 사용하지 않는다. 실패·중단 시 manifest와 lock을 읽기 전용으로 확인하고 중복 실행하지 않는다.

## 3. Exact Inspection과 품질 검토

생성 결과의 **실제 값**으로 공식 검사 인수를 구성한다. 아래 꺾쇠 값은 예시 자리표시자이며 전월 값을 재사용하지 않는다. `--stock-code`는 확정 scope 수만큼 반복한다.

```powershell
venv\Scripts\python.exe tools\inspect_approve_dashboard_inventory_frequency_snapshot.py --company-id <회사번호> --evaluation-month 202611 --stock-code <코드1> --stock-code <코드2> --generation <세대> --expected-checksum <전체SHA256> --expected-product-count <제품수> --expected-normal-event-count <정상이벤트수> --expected-source-row-count <원천행수> --expected-grade-counts "A=<수>,B=<수>,C=<수>,D=<수>,E=<수>,F=<수>,X=<수>" --contract-version 2.1 --saved-io-full --expected-profile-fingerprint <프로필SHA256>
```

- Row checksum, projection digest, whole checksum, decimal 정합성, ProductUniverse, R046 관리용 제외, lifecycle·등급 분포, source diagnostics와 중복 제품을 검사한다.
- 승인 전 목표 상태는 manifest `draft`, approval `pending`, integrity `unapproved`다. 기존 202610 operating의 `published/approved` 상태가 유지돼야 한다.
- 매입/매출단가 `unavailable`·`stale`, 정상 출고 제품 중 매입단가 부재, A/B 등급의 수익성 결측을 회사별로 검토한다. 202610 회사13은 평균매입단가 결측 11,404/17,809건, 최근 출고 중 결측 10,867건이었다. 같은 현상이 재발하면 원천 적격 조건을 확인하고 업무 승인 없이 가격을 대체하거나 stale 정책을 완화하지 않는다.
- 회사13의 과거 legacy v2.1 경로에서는 저장 `008`이 고정 `001/002` 가격 SQL에 전달되지 않은 사례가 있었다. 이 과거 원인을 현행 `--saved-io-full` 평균매입단가 경로에도 그대로 적용하지 않는다. 현재 경로는 저장된 0xx 전체를 후보로 보되 양수 유상수량·양수 공급가액과 확정된 극단값·보정 규칙으로 적격성을 판정한다. 새 가격이 과거와 다르다는 이유만으로 실패 처리하거나 임의로 대체하지 않는다.
- 회사별 컷오프가 달라지면 기준월이 같아도 lifecycle/current-stock/classification 결과가 달라질 수 있다. 차이를 기록하고 임의 SQL로 날짜를 맞추지 않는다.

## 4. 승인·게시

Exact Inspection과 업무 검토가 끝나고 **사용자 승인과 실제 운영자 식별자**가 확인된 회사만 진행한다. 3절의 동일한 필수 인수에 `--approve --approved-by <확인된ID> --approval-reason <확정사유>`를 추가한다. 202610 작업의 ID `1`을 다음 달에 자동 전용하지 않는다.

공식 도구의 `approve_checked`가 승인과 게시를 함께 처리한다. 별도 게시 SQL은 실행하지 않는다. 회사별 순차 실행, 자동 재시도 0, `--force` 0. 실패 시 실제 commit 여부를 읽기 전용으로 확인하고 이미 승인된 회사를 재승인하거나 자동 롤백하지 않는다.

각 승인 직후 `mode=approve`, `approval=ready`, `integrity_status=ready`, `manifest_status=published`, `approval_status=approved`, generation/checksum/건수 일치, 최신 eligible operating의 202611 전환, 기존 202610 manifest 보존을 확인한다. 하나라도 어긋나면 다음 회사로 진행하지 않는다.

## 5. 운영 Smoke와 장애 대응

- 승인 회사별 `현재고 출고빈도 A`, `SIMS 일일점검`, `제품재고장 출고빈도 A`를 한 번씩 확인한다. 상태·건수·시간·오류를 기록하고 Dashboard `source_call_count=3`을 확인한다. 테스트 DB의 정상 no_data는 원인 증거와 함께 분리한다.
- 결과표의 실제 화면은 필요한 회사에서 별도 수동 확인한다. bare-script의 Streamlit 경고나 한글 캡처 손실을 기능 PASS의 근거로 대체하지 않는다.
- 장시간 정체는 단계·PID·DB 상태를 확인한다. 안전한 클라이언트 프로세스만 종료하고, SQL 세션 KILL/무제한 대기/무조건 재시도/lock 임의 삭제를 하지 않는다.
- 마감 기록에는 회사별 manifest/generation, checksum, scope/fingerprint, 컷오프, Exact Inspection, 승인자·사유, 기존/신규 operating, 3종 Smoke와 미해결 데이터 품질 이슈를 남긴다.

## 근거

- 공식 CLI: `tools/generate_dashboard_inventory_frequency_snapshot.py`, `tools/inspect_approve_dashboard_inventory_frequency_snapshot.py`
- 원천/가격 계약: `app/services/dashboard_inventory_frequency_snapshot_service.py`, `app/services/monthly_frequency_aggregate.py`, `app/services/dashboard_inventory_frequency_snapshot.py`
