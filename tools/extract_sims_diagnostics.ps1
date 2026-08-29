<#
.SYNOPSIS
  SIMS 로그에서 Dashboard 성능·중복 렌더링·보안 이상 징후만 마스킹하여 추출합니다.

.EXAMPLE
  .\tools\extract_sims_diagnostics.ps1 -LogPath .\logs\app.log
  .\tools\extract_sims_diagnostics.ps1 -LogPath .\logs\app.log -OutputPath .\dashboard-diagnostic.txt

.NOTES
  - 원본 로그는 수정하지 않습니다.
  - 출력은 선별된 이벤트만 포함하고, 민감 필드는 [REDACTED]로 바꿉니다.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$LogPath,

    [string]$OutputPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $LogPath -PathType Leaf)) {
    throw "로그 파일을 찾을 수 없습니다."
}

function Protect-LogText {
    param([string]$Text)

    $safe = $Text
    # key=value 형태. 회사명처럼 공백이 포함될 수 있는 값은 다음 key= 직전까지 처리합니다.
    $safe = [regex]::Replace(
        $safe,
        '(?i)\b(login_id|user_id|room_id|request_id|company_id|company_name|db_name|server|database|host|uid|username|user|password|pwd|api[_-]?key|connection_string|authorization|token|cookie|session(?:_id)?|secret)=(.*?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)',
        '$1=[REDACTED]'
    )
    $safe = [regex]::Replace($safe, '(?i)(Driver|Server|Database|Uid|Pwd)=([^;\s]+)', '$1=[REDACTED]')
    $safe = [regex]::Replace($safe, '(?i)\bauthorization\s*:\s*bearer\s+[^\s,;]+', 'Authorization: Bearer [REDACTED]')
    $safe = [regex]::Replace($safe, '(?i)\bbearer\s+[^\s,;]+', 'Bearer [REDACTED]')
    $safe = [regex]::Replace(
        $safe,
        '(?i)(["'']?(?:login_id|user_id|room_id|request_id|company_id|company_name|db_name|server|database|host|uid|username|user|password|pwd|api[_-]?key|connection_string|authorization|token|cookie|session(?:_id)?|secret)["'']?\s*:\s*)(?:"[^"]*"|''[^'']*''|[^,\s;}\]]+)',
        '$1[REDACTED]'
    )
    $safe = [regex]::Replace($safe, '(?i)\b(authorization|bearer|token|cookie|session(?:_id)?|secret)\s*:\s*[^\s,;]+', '$1: [REDACTED]')
    $safe = [regex]::Replace($safe, '(?i)[A-Z]:\\[^\s,;]+', '[REDACTED_PATH]')
    $safe = [regex]::Replace($safe, '(?i)\\\\[^\s,;]+', '[REDACTED_PATH]')
    return $safe
}

$eventPattern = '\[dashboard\.(start|scope|source_load|filter|finish)\]|analytics\.(sales_trend\.(fast_path|perf)|stock_shortage\.perf)|company\.change|chat\.(panel\.push|render\.dedupe|sims\.push)|SIMS_PUSH|compact result render'
$securityPattern = '(?i)["'']?\b(login_id|user_id|room_id|request_id|company_id|company_name|db_name|server|database|host|uid|username|user|password|pwd|api[_-]?key|connection_string|authorization|token|cookie|session(?:_id)?|secret)["'']?\s*[:=]'

$selected = New-Object System.Collections.Generic.List[string]
$security = New-Object System.Collections.Generic.List[string]
$finishes = New-Object System.Collections.Generic.List[object]

Get-Content -LiteralPath $LogPath -Encoding UTF8 | ForEach-Object {
    $line = $_
    $hasSecurityField = [regex]::IsMatch($line, $securityPattern)
    if ($hasSecurityField) {
        $security.Add((Protect-LogText $line))
    }
    if ([regex]::IsMatch($line, $eventPattern)) {
        $selected.Add((Protect-LogText $line))
    }
    if ($line -match '\[dashboard\.finish\].*?source_call_count=(\d+).*?elapsed_ms=(\d+)') {
        $finishes.Add([pscustomobject]@{
            SourceCalls = [int]$Matches[1]
            ElapsedMs   = [int]$Matches[2]
            Over30Sec   = ([int]$Matches[2] -gt 30000)
        })
    }
}

$report = New-Object System.Collections.Generic.List[string]
$report.Add('SIMS diagnostic extract (redacted)')
$report.Add(('Generated: {0:yyyy-MM-dd HH:mm:ss}' -f (Get-Date)))
$report.Add(('Source log: {0}' -f (Split-Path -Leaf $LogPath)))
$report.Add('')
$report.Add('Dashboard summary')
if ($finishes.Count -eq 0) {
    $report.Add('  No dashboard.finish event found.')
} else {
    $index = 0
    foreach ($finish in $finishes) {
        $index++
        $report.Add(('  #{0}: elapsed_ms={1}; source_call_count={2}; over_30_seconds={3}' -f $index, $finish.ElapsedMs, $finish.SourceCalls, $finish.Over30Sec))
    }
}
$report.Add('')
$report.Add(('SECURITY_VIOLATION count={0}' -f $security.Count))
if ($security.Count -gt 0) {
    $report.AddRange($security)
}
$report.Add('')
$report.Add(('Selected diagnostic events count={0}' -f $selected.Count))
$report.AddRange($selected)

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $report
} else {
    if (Test-Path -LiteralPath $OutputPath) {
        throw "출력 파일이 이미 존재합니다. 기존 진단 결과를 보존하기 위해 덮어쓰지 않습니다."
    }
    $parent = Split-Path -Parent $OutputPath
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $report | Set-Content -LiteralPath $OutputPath -Encoding utf8
    Write-Output "진단 결과 저장 완료: $OutputPath"
}
