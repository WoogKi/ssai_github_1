---
title: "SIMS AI 공통 운영 Runbook"
date: "2026-08-09"
version: "v1.0"
status: "official"
baseline_branch: "feat/dashboard-stock-extension-20260727"
baseline_commit: "6c83962bc1b079fe440d56a313de536cf9490651"
---

# SIMS AI 공통 운영 Runbook

## 1. 목적과 적용 범위

이 문서는 1호기 개발, 자동 회귀, pre-commit 감사, 선택적 commit/push,
2호기 fast-forward 적용, Streamlit 재시작과 smoke까지의 공식 운영 순서를 정한다.

| 구분 | 공식 기준 |
|---|---|
| 1호기 프로젝트 | `C:\New\Python_Project\LmStudion_project1` |
| 1호기 Python | `venv` |
| 2호기 프로젝트 | `C:\New\Python_Project\LmStudion_project1` |
| 2호기 Python | `.venv` |
| 공식 브랜치 | `feat/dashboard-stock-extension-20260727` |
| 현재 안정 기준 | `6c83962` |

2호기 프로세스 점검과 안전한 재기동은
[2호기 운영 점검 Runbook](RUNBOOK_2HO_OPERATION_CHECK.md)을 함께 따른다.

## 2. 변경 전 확인

```powershell
cd C:\New\Python_Project\LmStudion_project1
git status --short --branch
git rev-parse HEAD
git branch --show-current
```

확인 기준:

- 브랜치와 작업 목적이 일치한다.
- 기존 unrelated 변경과 untracked 파일을 기록하고 보존한다.
- `.env`, 로그, 업로드, 다운로드, 고객 데이터와 DB 연결정보를 diff에 넣지 않는다.
- 작업 범위 밖 파일을 reset, restore, checkout 또는 stash로 정리하지 않는다.

## 3. 1호기 검증

### 3.1 Python 문법 검사

프로젝트 필수 파일과 이번 수정 Python 파일을 함께 검사한다.

```powershell
.\venv\Scripts\python.exe -m py_compile `
  app\Lmstudio_SSAI_chat_main.py `
  app\ui\sims_panel.py `
  app\ui\chat_middleware.py `
  app\services\rddbc060_service.py `
  app\services\ssai_auth_service.py `
  app\services\ssai_storage_service.py `
  app\db\mssql_client.py
```

### 3.2 회귀 순서

```powershell
.\venv\Scripts\python.exe tools\check_io_nlq_regression.py
.\venv\Scripts\python.exe tools\check_analytics_regression.py
.\venv\Scripts\python.exe tools\check_nlq_action_inventory.py
.\venv\Scripts\python.exe -m pip check
git diff --check
```

현재 알려진 허용 실패는 Analytics의
`sales_chart_target_markers_missing` 1건뿐이다. 그 외 실패를 이 항목으로 묶어
합격 처리하지 않는다.

### 3.3 pre-commit 감사

```powershell
git status --short --branch
git diff --stat
git diff --check
git diff --name-only
```

감사 항목:

- 변경 파일이 요청 범위와 일치한다.
- 신규 SQL, fallback, 임시 debug 코드와 fixture 전용 값이 운영 코드에 없다.
- 회사 격리와 current-table 원본 경계가 유지된다.
- 허용 실패 외 회귀 실패가 없다.
- 실제 수동 테스트와 로그에서 신규 `ERROR`, `Traceback`, 예상 밖 `WARNING`이 없다.

## 4. Git 선택 stage와 배포 준비

`git add .`과 `git add -A`는 사용하지 않는다. 검토가 끝난 대상만 경로를
명시하여 stage한다.

```powershell
git add -- <검토가_끝난_파일1> <검토가_끝난_파일2>
git diff --cached --name-only
git diff --cached --stat
git diff --cached --check
```

기존 unrelated untracked 파일, 로그, export와 로컬 진단자료가 staged 목록에
있으면 commit하지 않는다. commit 후에는 다음을 확인한다.

```powershell
git status --short --branch
git log -1 --oneline
git push origin feat/dashboard-stock-extension-20260727
```

push는 1호기에서 승인된 브랜치에만 수행한다.

## 5. 2호기 적용

2호기는 운영 작업트리에서 임의 commit 또는 push하지 않는다.

```powershell
cd C:\New\Python_Project\LmStudion_project1
git status --short --branch
git fetch origin
git pull --ff-only origin feat/dashboard-stock-extension-20260727
git rev-parse HEAD
```

적용 기준:

- pull 전 로컬 변경이 없어야 한다.
- `--ff-only`가 실패하면 merge나 rebase로 우회하지 않는다.
- 1호기 push commit과 2호기 `HEAD`가 같은지 확인한다.
- 전체 Streamlit process tree를 안전하게 재기동한다.
- 단순 Health 성공만으로 새 코드 반영을 확정하지 않는다.

## 6. 배포 후 Health와 smoke

2호기 재기동 후 다음 순서로 확인한다.

1. ScheduledTask 상태와 실제 process tree 확인
2. 포트 8501 Listen PID 1개 확인
3. `http://127.0.0.1:8501/_stcore/health`의 HTTP 200 및 `ok` 확인
4. 실제 OwningProcess 시작시각이 이번 재기동과 일치하는지 확인
5. `app.log`와 Streamlit 서버 로그의 신규 `ERROR`/`Traceback` 확인
6. 로그인·회사 선택·채팅방 격리 확인
7. 최소 smoke 실행

최소 smoke:

- `제품재고장 제조사 한미`
- `제품재고장 한미`
- `현재고 바이엘`
- `SIMS 일일점검`

smoke에서는 결과 상태, 조건 표시, 행 구조, 응답시간, 채팅 저장 1회와
현재표 후속질문 가능 여부를 함께 본다.

## 7. 배포 완료 기준

- 1호기와 2호기의 commit이 같다.
- 2호기 8501 Listen PID가 하나다.
- Health가 HTTP 200/`ok`다.
- 최소 smoke가 기존 계약과 일치한다.
- 신규 `ERROR`, `Traceback`과 설명되지 않은 `WARNING`이 없다.
- 롤백이 필요하면 승인된 이전 commit으로 별도 절차를 수립한다.

## 8. 현재 알려진 후속 과제

- `제품재고장 한미` 무라벨 통합검색 약 60초 성능
- 무라벨 제품재고현황 `nlq.trace.parsed search_fields` 정합성
- 2호기 ScheduledTask 하위 Streamlit child-process lifecycle

후속 과제를 이유로 결과 범위를 줄이거나 의미를 바꿔 smoke를 통과시키지 않는다.
