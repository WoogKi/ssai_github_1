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

    [string]$OutputPath,

    [switch]$StructuredPresentation
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

function Get-SafeReference {
    param(
        [AllowEmptyString()]
        [string]$Value,
        [string]$Prefix
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ''
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha256.ComputeHash($bytes)
    } finally {
        $sha256.Dispose()
    }
    $hex = ([BitConverter]::ToString($hash) -replace '-', '').ToLowerInvariant()
    return ('{0}:{1}' -f $Prefix, $hex.Substring(0, 12))
}

function Get-LogTimestamp {
    param([string]$Line)

    if ($Line -match '^\[(?<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]') {
        try {
            return [datetime]::ParseExact($Matches.stamp, 'yyyy-MM-dd HH:mm:ss', $null)
        } catch {
            return $null
        }
    }
    return $null
}

function Get-SafeAction {
    param([string]$Value)

    $text = [regex]::Replace([string]$Value, '[\x00-\x1f]', ' ').Trim()
    if ($text.Length -gt 160) {
        return $text.Substring(0, 160)
    }
    return $text
}

function Get-HashtableText {
    param(
        [hashtable]$Values,
        [string]$Name,
        [string]$Default = ''
    )

    if ($Values.ContainsKey($Name) -and $null -ne $Values[$Name]) {
        return [string]$Values[$Name]
    }
    return $Default
}

function Add-StructuredPresentationRecord {
    param(
        [System.Collections.Generic.List[object]]$Records,
        [hashtable]$Values
    )

    $Records.Add([pscustomobject]@{
        Sequence             = $Records.Count + 1
        Timestamp            = Get-HashtableText $Values 'Timestamp'
        Action               = Get-SafeAction (Get-HashtableText $Values 'Action')
        ResultStatus         = Get-HashtableText $Values 'ResultStatus'
        SourceCallCount      = Get-HashtableText $Values 'SourceCallCount'
        BusinessElapsedMs    = Get-HashtableText $Values 'BusinessElapsedMs'
        PocStatus            = Get-HashtableText $Values 'PocStatus' 'not_observed'
        PocReasonCode        = Get-HashtableText $Values 'PocReasonCode'
        PocElapsedMs         = Get-HashtableText $Values 'PocElapsedMs'
        PocRetryCount        = Get-HashtableText $Values 'PocRetryCount'
        PocToolCallCount     = Get-HashtableText $Values 'PocToolCallCount'
        Correlation          = Get-HashtableText $Values 'Correlation'
        TableKeyReference    = Get-SafeReference (Get-HashtableText $Values 'TableKey') 'table'
        SourceTableReference = Get-SafeReference (Get-HashtableText $Values 'SourceTableKey') 'source_table'
    })
}

function Write-StructuredPresentationExtract {
    param([string]$Path)

    $records = New-Object System.Collections.Generic.List[object]
    $lastCaseByAction = @{}
    $lastSourceByTable = @{}
    $pendingPoc = $null
    $lastRecord = $null

    Get-Content -LiteralPath $Path -Encoding UTF8 | ForEach-Object {
        $line = $_
        $timestamp = Get-LogTimestamp $line

        if ($line -match '\[nlq\.case\].*?action=(?<action>.*?)\s+result_status=(?<status>[A-Za-z_]+)') {
            $lastCaseByAction[(Get-SafeAction $Matches.action)] = [pscustomobject]@{
                Status = $Matches.status
                Timestamp = $timestamp
            }
            return
        }

        if ($line -match '\[structured\.response\].*?source_call_count=(?<calls>\d+).*?table_key=(?<table>\S*).*?source_table_key=(?<source>\S*)') {
            $lastSourceByTable[$Matches.table] = [pscustomobject]@{
                SourceCallCount = $Matches.calls
                SourceTableKey = $Matches.source
                Timestamp = $timestamp
            }
            return
        }

        if ($line -match '\[structured\.presentation_poc\]\s+status=(?<status>[A-Za-z_]+)\s+reason_code=(?<reason>[A-Za-z_]+)\s+elapsed_ms=(?<elapsed>\d+)\s+retry_count=(?<retry>\d+)\s+tool_call_count=(?<tools>\d+)') {
            $pendingPoc = [pscustomobject]@{
                Status = $Matches.status
                ReasonCode = $Matches.reason
                ElapsedMs = $Matches.elapsed
                RetryCount = $Matches.retry
                ToolCallCount = $Matches.tools
                Timestamp = $timestamp
            }
            return
        }

        if ($line -match '\[chat\.sims\.push\].*?action=(?<action>.*?)\s+rows=.*?\s+table_key=(?<table>\S+)') {
            $action = Get-SafeAction $Matches.action
            $poc = $null
            if ($null -ne $pendingPoc) {
                $withinDelivery = $true
                if ($null -ne $timestamp -and $null -ne $pendingPoc.Timestamp) {
                    $withinDelivery = [math]::Abs(($timestamp - $pendingPoc.Timestamp).TotalSeconds) -le 5
                }
                if ($withinDelivery) {
                    $poc = $pendingPoc
                }
                $pendingPoc = $null
            }
            $case = $lastCaseByAction[$action]
            $source = $lastSourceByTable[$Matches.table]
            $values = @{
                Timestamp = if ($null -ne $timestamp) { $timestamp.ToString('s') } else { '' }
                Action = $action
                ResultStatus = if ($null -ne $case) { $case.Status } else { '' }
                SourceCallCount = if ($null -ne $source) { $source.SourceCallCount } else { '' }
                PocStatus = if ($null -ne $poc) { $poc.Status } else { 'not_observed' }
                PocReasonCode = if ($null -ne $poc) { $poc.ReasonCode } else { '' }
                PocElapsedMs = if ($null -ne $poc) { $poc.ElapsedMs } else { '' }
                PocRetryCount = if ($null -ne $poc) { $poc.RetryCount } else { '' }
                PocToolCallCount = if ($null -ne $poc) { $poc.ToolCallCount } else { '' }
                TableKey = $Matches.table
                SourceTableKey = if ($null -ne $source) { $source.SourceTableKey } else { '' }
            }
            Add-StructuredPresentationRecord -Records $records -Values $values
            $lastRecord = $records[$records.Count - 1]
            return
        }

        if ($line -match '\[sims\.response_timing\]\s+action=(?<action>.*?)\s+message_id=(?<message>\S+).*?elapsed_ms=(?<elapsed>\d*)') {
            if ($null -ne $lastRecord -and $lastRecord.Action -eq (Get-SafeAction $Matches.action)) {
                $lastRecord.Correlation = Get-SafeReference $Matches.message 'message'
                $lastRecord.BusinessElapsedMs = $Matches.elapsed
            }
            return
        }

        if ($line -match '\[nlq\.trace\.(?:result|finish)\].*?action=''(?<action>[^'']*)''.*?result_status=(?<status>[A-Za-z_]+).*?source_call_count=(?<calls>\d+)') {
            if ($null -ne $lastRecord -and $lastRecord.Action -eq (Get-SafeAction $Matches.action)) {
                $lastRecord.ResultStatus = $Matches.status
                $lastRecord.SourceCallCount = $Matches.calls
            }
        }
    }

    $report = New-Object System.Collections.Generic.List[string]
    $report.Add('SIMS structured presentation smoke extract (allowlisted)')
    $report.Add(('Generated: {0:yyyy-MM-dd HH:mm:ss}' -f (Get-Date)))
    $report.Add(('Source log: {0}' -f (Split-Path -Leaf $Path)))
    $report.Add('Correlation: PoC marker immediately preceding chat delivery, then hashed message_id from sims.response_timing.')
    $report.Add('Security: raw prompt/response, SQL, binds, credentials, request_id, user/room/company identifiers, and DataFrame rows are not emitted.')
    $report.Add('')
    $report.Add(('Request records count={0}' -f $records.Count))
    foreach ($record in $records) {
        $report.Add((
            '[{0}] timestamp={1}; correlation={2}; action={3}; result_status={4}; source_call_count={5}; business_elapsed_ms={6}; poc_status={7}; poc_reason_code={8}; poc_elapsed_ms={9}; retry_count={10}; tool_call_count={11}; table_key={12}; source_table_key={13}' -f
            $record.Sequence, $record.Timestamp, $record.Correlation, $record.Action, $record.ResultStatus,
            $record.SourceCallCount, $record.BusinessElapsedMs, $record.PocStatus, $record.PocReasonCode,
            $record.PocElapsedMs, $record.PocRetryCount, $record.PocToolCallCount,
            $record.TableKeyReference, $record.SourceTableReference
        ))
    }
    return $report
}

if ($StructuredPresentation) {
    $report = Write-StructuredPresentationExtract -Path $LogPath
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
    return
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
