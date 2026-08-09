---
title: "SIMS AI 2호기 운영 점검 Runbook"
date: "2026-08-09"
version: "v1.0"
status: "official"
baseline_commit: "6c83962bc1b079fe440d56a313de536cf9490651"
---

# SIMS AI 2호기 운영 점검 Runbook

## 1. 고정 운영 정보

| 항목 | 값 |
|---|---|
| 프로젝트 | `C:\New\Python_Project\LmStudion_project1` |
| 가상환경 | `.venv` |
| ScheduledTask | `SIMS_AI_2HO_Streamlit` |
| Streamlit 포트 | `8501` |
| Health | `http://127.0.0.1:8501/_stcore/health` |
| 앱 로그 | `D:\SSAI_DATA\logs\app.log` |
| Streamlit 로그 | `D:\SSAI_DATA\logs\streamlit_server_2ho.log` |
| wrapper | `D:\SSAI_DATA\tools\run_streamlit_2ho_rotating.ps1` |
| inner cmd | `D:\SSAI_DATA\tools\run_streamlit_2ho_inner.cmd` |

공통 배포 순서는 [SIMS AI 공통 운영 Runbook](RUNBOOK_SIMSAI.md)을 따른다.

## 2. 실제 process 구조와 발견사항

`Stop-ScheduledTask`는 작업 시작 프로세스를 중지하지만 이미 분리된 Streamlit
child Python process가 남을 수 있다.

```text
ScheduledTask
  -> powershell wrapper
  -> cmd
  -> .venv python
  -> 실제 python/uvicorn
```

따라서 ScheduledTask 상태, process tree, 8501 OwningProcess와 Health를 함께
확인해야 한다. Health 200만으로 새 코드가 반영되었다고 판단하지 않는다.

## 3. 재기동 전 읽기 전용 점검

관리자 PowerShell에서 실행한다.

```powershell
$taskName = 'SIMS_AI_2HO_Streamlit'
$project = 'C:\New\Python_Project\LmStudion_project1'
$healthUrl = 'http://127.0.0.1:8501/_stcore/health'

Get-ScheduledTask -TaskName $taskName |
  Select-Object TaskName, State

Get-CimInstance Win32_Process |
  Where-Object {
    $_.CommandLine -and (
      $_.CommandLine -like "*$project*" -or
      $_.CommandLine -like '*run_streamlit_2ho_rotating.ps1*' -or
      $_.CommandLine -like '*run_streamlit_2ho_inner.cmd*'
    )
  } |
  Select-Object ProcessId, ParentProcessId, CreationDate, Name, CommandLine

Get-NetTCPConnection -LocalPort 8501 -ErrorAction SilentlyContinue |
  Select-Object State, LocalAddress, LocalPort, OwningProcess, CreationTime
```

각 PID는 다음으로 parent와 시작시각을 재확인한다.

```powershell
Get-CimInstance Win32_Process -Filter 'ProcessId=<확인할_PID>' |
  Select-Object ProcessId, ParentProcessId, CreationDate, Name, CommandLine
```

PID는 문서나 스크립트에 하드코딩하지 않는다.

## 4. 안전한 재기동 절차

### 4.1 ScheduledTask 중지

```powershell
Stop-ScheduledTask -TaskName 'SIMS_AI_2HO_Streamlit'
Get-ScheduledTask -TaskName 'SIMS_AI_2HO_Streamlit' |
  Select-Object TaskName, State
```

중지 직후 3절의 process와 포트 명령을 다시 실행한다.

### 4.2 잔존 process tree 판정

다음 조건을 모두 확인한다.

- CommandLine이 2호기 프로젝트 또는 고정 wrapper/inner cmd와 연결된다.
- 8501 OwningProcess 또는 그 parent tree다.
- 다른 서비스의 Python process가 아니다.
- 구 프로세스의 시작시각과 parent PID 관계가 확인된다.

광범위한 `python.exe` 종료, 이름만으로 종료, PID 추측은 금지한다.

### 4.3 정확한 tree만 종료

잔존이 확인된 경우에만, 사람이 확인한 **cmd parent PID**를 사용한다.

```powershell
taskkill /PID <확인한_CMD_PARENT_PID> /T /F
```

종료 뒤 8501이 완전히 비었는지 확인한다.

```powershell
Get-NetTCPConnection -LocalPort 8501 -ErrorAction SilentlyContinue
```

결과가 남으면 새 작업을 시작하지 않고 PID/parent 관계를 다시 조사한다.

### 4.4 ScheduledTask 1회 시작

```powershell
Start-ScheduledTask -TaskName 'SIMS_AI_2HO_Streamlit'
Start-Sleep -Seconds 5
Get-ScheduledTask -TaskName 'SIMS_AI_2HO_Streamlit' |
  Select-Object TaskName, State
```

반복 Start로 중복 process를 만들지 않는다.

## 5. 재기동 후 확인

### 5.1 Listen PID와 process tree

```powershell
$listeners = Get-NetTCPConnection -LocalPort 8501 -State Listen `
  -ErrorAction SilentlyContinue
$listeners |
  Select-Object LocalAddress, LocalPort, OwningProcess, CreationTime

$listeners | ForEach-Object {
  Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)" |
    Select-Object ProcessId, ParentProcessId, CreationDate, Name, CommandLine
}
```

합격 기준은 Listen OwningProcess가 1개이고, 프로젝트·가상환경·시작시각이 이번
재기동과 일치하는 것이다.

### 5.2 Health

```powershell
$response = Invoke-WebRequest `
  -Uri 'http://127.0.0.1:8501/_stcore/health' `
  -UseBasicParsing `
  -TimeoutSec 10
$response.StatusCode
$response.Content
```

HTTP `200`과 본문 `ok`를 모두 확인한다.

### 5.3 로그

```powershell
Get-Content 'D:\SSAI_DATA\logs\streamlit_server_2ho.log' -Tail 200
Get-Content 'D:\SSAI_DATA\logs\app.log' -Tail 300
```

이번 재기동 시각 이후의 `ERROR`, `Traceback`, 반복 재시작, 포트 충돌과
예상하지 않은 `WARNING`을 확인한다. 로그 원문에 포함된 연결정보나 고객 데이터는
보고서에 복사하지 않는다.

## 6. 배포 코드 확인

```powershell
cd C:\New\Python_Project\LmStudion_project1
git status --short --branch
git rev-parse HEAD
git log -1 --oneline
```

Health와 별개로 1호기에서 push한 commit과 같은지 확인한다. 2호기에서는 임의
commit/push, merge, rebase를 하지 않는다.

## 7. smoke

최소 다음을 실제 UI에서 실행한다.

1. 로그인과 회사 선택
2. `제품재고장 제조사 한미`
3. `제품재고장 한미`
4. `현재고 바이엘`
5. `SIMS 일일점검`
6. 채팅방 저장·전환과 현재표 후속질문

결과, 조건, 행 구조, 처리시간과 로그를 함께 확인한다.

## 8. 장애별 판정

| 증상 | 먼저 확인할 항목 | 금지되는 우회 |
|---|---|---|
| Health 실패 | 8501 Listen, server log, process 시작시각 | 반복 Start |
| Health 성공·구 코드 | OwningProcess와 HEAD, 시작시각 | Health만 보고 완료 처리 |
| 8501 PID 복수 | parent tree와 ScheduledTask 상태 | 모든 Python 종료 |
| task 중지 후 PID 잔존 | wrapper/cmd/python parent 관계 | PID 하드코딩 |
| smoke만 실패 | app.log, 회사/권한, 해당 회귀 | 결과 범위 축소 |

## 9. 완료 기준

- ScheduledTask가 정상 상태다.
- 8501 Listen PID가 정확히 하나다.
- PID와 parent tree가 2호기 프로젝트에 속한다.
- Health가 HTTP 200/`ok`다.
- `HEAD`가 승인된 배포 commit과 같다.
- 신규 `ERROR`/`Traceback`이 없고 최소 smoke가 통과한다.
