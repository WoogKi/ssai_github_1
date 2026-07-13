param(
    [string]$TaskName = "\SIMS_AI_2HO_Streamlit",
    [int]$Port = 8501
)

Write-Host "=== Stop Streamlit on port $Port ==="

$streamlitPids = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Listen" } |
    Select-Object -ExpandProperty OwningProcess -Unique

foreach ($streamlitPid in $streamlitPids) {
    Write-Host "Stop Streamlit PID=$streamlitPid"
    taskkill /PID $streamlitPid /F | Out-Host
}

Start-Sleep -Seconds 2

Write-Host "`n=== Start scheduled task: $TaskName ==="
schtasks /Run /TN $TaskName | Out-Host

Start-Sleep -Seconds 5

Write-Host "`n=== Check Streamlit ==="

$newStreamlitPid = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Listen" } |
    Select-Object -First 1 -ExpandProperty OwningProcess

if ($newStreamlitPid) {
    netstat -ano | findstr ":$Port"

    Write-Host "`n=== CommandLine ==="
    Get-CimInstance Win32_Process -Filter "ProcessId=$newStreamlitPid" |
        Select-Object ProcessId, CommandLine |
        Format-List
}
else {
    Write-Host "ERROR: Streamlit is not listening on port $Port"
}
