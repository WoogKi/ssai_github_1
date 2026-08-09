---
title: "6c83962 NLQ·Dashboard·재고 공식 테스트 결과"
date: "2026-08-09"
status: "official-test-evidence"
baseline_branch: "feat/dashboard-stock-extension-20260727"
baseline_commit: "6c83962bc1b079fe440d56a313de536cf9490651"
---

# 6c83962 NLQ·Dashboard·재고 공식 테스트 결과

## 1. 문서 성격

이 문서는 `6c83962` 배포 시점의 1호기 회귀·pre-commit 감사와 2호기 적용·smoke
결과를 보존하는 증적이다. 향후 구현이 바뀌어도 이 문서를 최신 값으로 덮어쓰지
않고 새 기준 커밋의 테스트 결과를 별도 작성한다.

관련 계약:

- [Dashboard 설계](../02_design/DASHBOARD_LITE_V01_DESIGN.md)
- [공통 조회조건 ver03](../02_design/DASHBOARD_KPI_NLQ_공통조회조건_확정안_ver03.md)
- [NLQ·현재고·현재표 계약](../02_design/SIMS_NLQ_CURRENT_STOCK_CURRENT_TABLE_CONTRACT.md)
- [공통 운영 Runbook](../03_runbook/RUNBOOK_SIMSAI.md)

## 2. 기준

| 항목 | 값 |
|---|---|
| 브랜치 | `feat/dashboard-stock-extension-20260727` |
| 커밋 | `6c83962bc1b079fe440d56a313de536cf9490651` |
| 검증일 | 2026-08-09 |
| 1호기 | pre-commit 회귀·감사 후 commit/push 완료 |
| 2호기 | fetch 후 ff-only 적용, 동일 `HEAD` 확인 |

## 3. 1호기 pre-commit 결과

| 검증 | 결과 |
|---|---|
| Python `py_compile` | PASS |
| `tools/check_io_nlq_regression.py` | PASS |
| `tools/check_analytics_regression.py` | 허용 marker 1건 외 계약 통과 |
| `tools/check_nlq_action_inventory.py` | PASS |
| `python -m pip check` | PASS |
| `git diff --check` | PASS |
| 누적 diff 감사 | commit 가능 판정 |

Analytics의 기존 허용 실패:

```text
sales_chart_target_markers_missing
```

이 1건 외 실패를 허용 실패로 합산하지 않았다.

## 4. Git·2호기 적용

1. 1호기에서 검토 파일만 선택 stage
2. cached stat/check 확인
3. `6c83962` commit 및 원격 push 완료
4. 2호기 `git fetch`
5. 2호기 `git pull --ff-only`
6. 2호기 `HEAD=6c83962` 확인
7. 잔존 process tree 정리
8. Streamlit 단일 process 재기동
9. 포트 8501 OwningProcess 1개 확인
10. Health HTTP 200 및 `ok` 확인

`Stop-ScheduledTask` 뒤 child process가 남을 수 있어 PID/Parent PID와 프로젝트
CommandLine을 확인한 뒤 정확한 tree만 정리했다.

## 5. 2호기 smoke 결과

| 질문 | 결과 | 행/계약 | 처리시간 | 진단 |
|---|---|---|---:|---|
| `제품재고장 제조사 한미` | success | 541건 | 약 4.9초 | 명시 제조사 범위 |
| `제품재고장 한미` | success | 558건 | 약 60.9초 | `unlabeled_like_or`, `month_carry`/`last_cost` 병목 |
| `현재고 바이엘` | success | 63행, 20열 | 약 3.1초 | `code_in_used=True`, `fallback_to_like_or=False` |
| `SIMS 일일점검` | success | `valid_fact_rows=10015` | 약 25.4초 | `source_call_count=3` |

## 6. 계약별 판정

### 6.1 제품재고현황

- 명시 제조사 조회는 개별 제조사 조건으로 표시되었다.
- 무라벨 `한미`는 매입처·발주처·제품·제조사 OR-LIKE 통합검색으로 실행되었다.
- 화면 조회조건에 `통합검색 한미`가 표시되었다.
- 60초 응답은 결과 오류가 아니라 별도 성능 과제로 분리했다.

### 6.2 현재고

- 최종 20열과 제품·재고위치 행 구조가 유지되었다.
- 제조사/제품 resolver 결과가 code-IN 경로로 전달되었다.
- LIKE-OR fallback 없이 63행을 반환했다.
- display 반복 공란과 full/current-table/export 원본 분리가 유지되었다.

### 6.3 Dashboard

- deterministic `SIMS 일일점검` 경로로 실행되었다.
- 유효 facts 10,015행이 확인되었다.
- logical source는 sales/stock/inbound 3개였다.
- 정상 계약 `source_call_count=3`이 유지되었다.

### 6.4 현재표

- 현재고 source의 제품별 재고수량 TOP은 inventory handler가 우선했다.
- 제품 식별정보를 포함한 7열 계약이 확인되었다.
- display subtotal이 아니라 full/current-table source를 사용했다.

## 7. 알려진 후속 과제

1. `제품재고장 한미` 무라벨 OR-LIKE 약 60초 성능
2. 2호기 ScheduledTask 하위 child-process lifecycle 안정화
3. 무라벨 제품재고현황 `parsed search_fields` 로그 정합성
4. Analytics `sales_chart_target_markers_missing` marker 보완

후속 과제는 해당 기능의 현재 의미를 줄이거나 검증 결과를 성공으로 오인시키지
않고 별도 작업에서 처리한다.

## 8. 최종 판정

`6c83962`는 1호기 회귀·pre-commit 감사, 원격 push, 2호기 ff-only 적용,
단일 Streamlit 재기동, Health와 핵심 smoke를 통과한 안정 기준이다. 위 후속 과제는
운영 안정 기준과 분리하여 추적한다.
