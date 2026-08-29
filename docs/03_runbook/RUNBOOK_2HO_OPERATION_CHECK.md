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

## 7. Knowledge corpus 배포 및 검증

Git source 배포와 Knowledge corpus 배포는 별도 단계다. Knowledge의 manifest 또는
artifact가 변경된 경우에는 source Git pull만으로 2호기 Knowledge 자료가 갱신되지
않는다.

| 구분 | 경로 |
|---|---|
| 1호기 검증 corpus | `C:\SSAI_TEST_DATA\knowledge_poc` |
| 2호기 운영 corpus | `D:\SSAI_DATA\knowledge_poc` |

### 7.1 배포 전 확인과 백업

1. 1호기 검증 corpus의 `manifest.json`과 artifact 파일이 같은 검증 시점의 묶음인지
   확인한다.
2. 2호기 기존 corpus의 `manifest.json`, 문서 수, 승인 상태를 읽기 전용으로 기록한다.
3. 기존 2호기 corpus를 덮어쓰기 전에 timestamp를 붙인 별도 백업을 만들고, 백업
   경로와 manifest hash를 기록한다.
4. `manifest.json`만 단독 복사하지 않는다. manifest가 참조하는 artifact와 함께
   일관된 corpus 단위로 staging 후 배포한다.

운영 중인 corpus를 부분 복사하거나, artifact만 교체하거나, Git rollback으로 corpus가
자동 복구된다고 가정하면 안 된다.

### 7.2 배포 후 read-back

2호기 corpus를 배포한 뒤 다음을 확인한다.

```powershell
$root = 'D:\SSAI_DATA\knowledge_poc'
$manifestPath = Join-Path $root 'manifest.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json

Test-Path -LiteralPath $manifestPath
"document_count=$($manifest.documents.Count)"

$manifest.documents | ForEach-Object {
  $artifactPath = Join-Path (Join-Path $root 'artifacts') ("{0}.json" -f $_.content_hash)
  [pscustomobject]@{
    source_name = $_.source_name
    document_id = $_.document_id
    approval_status = $_.approval_status
    artifact_exists = Test-Path -LiteralPath $artifactPath
    artifact_hash = if (Test-Path -LiteralPath $artifactPath) { (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash } else { '' }
  }
} | Format-Table -AutoSize
```

manifest 존재, 문서 수, 각 문서의 `approval_status`, artifact 존재와 artifact hash를
1호기 검증 기록과 대조한다. 불일치하면 Streamlit을 시작하지 말고 corpus 배포를
중단하고 백업/원본 묶음을 다시 확인한다.

### 7.3 재기동과 권한 smoke

corpus read-back이 끝난 뒤 4절의 ScheduledTask 재기동 절차를 따르고, 5.2절의 Health
HTTP `200`/`ok`를 확인한다. 그 다음 실제 UI에서 최소 다음을 실행한다.

1. 권한 사용자: `/knowledge-tech Rddbc120의 출고 입출고구분 필드는 무엇인가?`
   - ERP Knowledge 답변과 `Rddbc120.txt` citation이 표시되어야 한다.
2. 비권한 사용자: 같은 질문
   - ERP 내부 내용, source 이름/key, citation, conflict notice가 노출되지 않아야 한다.

Git rollback과 corpus rollback은 서로 별개다. Git commit을 되돌려도 `D:\SSAI_DATA\knowledge_poc`
corpus는 바뀌지 않으며, corpus를 복원해도 source Git HEAD는 바뀌지 않는다. 두 rollback은
각각 승인된 Git 절차와 7.1절 백업을 기준으로 별도로 수행한다.

## 8. smoke

최소 다음을 실제 UI에서 실행한다.

1. 로그인과 회사 선택
2. `제품재고장 제조사 한미`
3. `제품재고장 한미`
4. `현재고 바이엘`
5. `SIMS 일일점검`
6. 채팅방 저장·전환과 현재표 후속질문

결과, 조건, 행 구조, 처리시간과 로그를 함께 확인한다.

## 9. 장애별 판정

| 증상 | 먼저 확인할 항목 | 금지되는 우회 |
|---|---|---|
| Health 실패 | 8501 Listen, server log, process 시작시각 | 반복 Start |
| Health 성공·구 코드 | OwningProcess와 HEAD, 시작시각 | Health만 보고 완료 처리 |
| 8501 PID 복수 | parent tree와 ScheduledTask 상태 | 모든 Python 종료 |
| task 중지 후 PID 잔존 | wrapper/cmd/python parent 관계 | PID 하드코딩 |
| smoke만 실패 | app.log, 회사/권한, 해당 회귀 | 결과 범위 축소 |

## 10. 완료 기준

- ScheduledTask가 정상 상태다.
- 8501 Listen PID가 정확히 하나다.
- PID와 parent tree가 2호기 프로젝트에 속한다.
- Health가 HTTP 200/`ok`다.
- `HEAD`가 승인된 배포 commit과 같다.
- 신규 `ERROR`/`Traceback`이 없고 최소 smoke가 통과한다.
- Knowledge corpus 변경이 있었으면 manifest/artifact read-back과 권한/no-leak smoke가 통과한다.
