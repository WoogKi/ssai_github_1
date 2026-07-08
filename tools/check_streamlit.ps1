param(
    [int]$Port = 8501
)

Write-Host "=== Streamlit Port Check: $Port ==="
netstat -ano | findstr ":$Port"

$streamlitPid = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Listen" } |
    Select-Object -First 1 -ExpandProperty OwningProcess

if ($streamlitPid) {
    Write-Host "`n=== CommandLine ==="
    Get-CimInstance Win32_Process -Filter "ProcessId=$streamlitPid" |
        Select-Object ProcessId, CommandLine |
        Format-List
}
else {
    Write-Host "Streamlit LISTENING process not found."
}

Write-Host "`n=== Health Check ==="
try {
    Invoke-WebRequest "http://127.0.0.1:$Port/_stcore/health" -UseBasicParsing | Select-Object StatusCode, Content
}
catch {
    Write-Host "Health check failed:"
    Write-Host $_.Exception.Message
}
