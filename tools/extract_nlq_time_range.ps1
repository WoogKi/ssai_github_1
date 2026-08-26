<#
.SYNOPSIS
  Append-only NLQ JSONL files from a requested time range without interpreting NLQ results.

.EXAMPLE
  .\tools\extract_nlq_time_range.ps1 -InputPath 'C:\SSAI_TEST_DATA\logs\nlq_cases.jsonl' -From '2026-08-26T16:50:00+09:00'

.EXAMPLE
  .\tools\extract_nlq_time_range.ps1 -InputPath 'C:\SSAI_TEST_DATA\logs\nlq_feedback.jsonl' -LogKind feedback -From '2026-08-26T16:50:00+09:00'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$InputPath,

    [string]$OutputPath,

    [ValidateNotNullOrEmpty()]
    [string]$OutputDirectory = 'C:\New\Python_Project\_codex_diffs\LmStudion_project1',

    [Parameter(Mandatory = $true)]
    [DateTimeOffset]$From,

    [Nullable[DateTimeOffset]]$To,

    [ValidateSet('cases', 'feedback')]
    [string]$LogKind = 'cases'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-RecordOccurredAt {
    param([object]$Record)

    foreach ($field in @('occurred_at', 'logged_at', 'created_at', 'timestamp')) {
        $property = $Record.PSObject.Properties[$field]
        if ($null -eq $property) {
            continue
        }
        $value = $property.Value
        if ($null -eq $value -or [string]::IsNullOrWhiteSpace([string]$value)) {
            continue
        }
        $parsed = [DateTimeOffset]::MinValue
        if ([DateTimeOffset]::TryParse([string]$value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AllowWhiteSpaces, [ref]$parsed)) {
            return $parsed
        }
        throw "timestamp parse failed field=$field"
    }
    throw 'timestamp missing'
}

$toValue = if ($null -ne $To) { [DateTimeOffset]$To } else { [DateTimeOffset]::Now }
if ($From -gt $toValue) {
    throw 'From은 To보다 클 수 없습니다.'
}

$resolvedInput = [System.IO.Path]::GetFullPath($InputPath)
if (-not (Test-Path -LiteralPath $resolvedInput -PathType Leaf)) {
    throw "원본 JSONL 파일을 찾을 수 없습니다: $resolvedInput"
}

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $dateDirectory = $toValue.ToString('yyyyMMdd')
    $fromTag = $From.ToString('yyyyMMdd_HHmmss')
    $toTag = $toValue.ToString('yyyyMMdd_HHmmss')
    $OutputPath = Join-Path -Path (Join-Path -Path $OutputDirectory -ChildPath $dateDirectory) -ChildPath ("nlq_{0}_{1}_to_{2}.jsonl" -f $LogKind, $fromTag, $toTag)
}

$resolvedOutput = [System.IO.Path]::GetFullPath($OutputPath)
if ([string]::Equals($resolvedInput, $resolvedOutput, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw '출력 파일은 원본 JSONL과 달라야 합니다.'
}

$parent = Split-Path -Parent $resolvedOutput
if ($parent) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
}

$selected = 0
$parseFailed = 0
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$writer = New-Object System.IO.StreamWriter($resolvedOutput, $false, $utf8NoBom)
try {
    foreach ($line in [System.IO.File]::ReadLines($resolvedInput, $utf8NoBom)) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        try {
            $record = $line | ConvertFrom-Json -ErrorAction Stop
            $occurredAt = Get-RecordOccurredAt -Record $record
        }
        catch {
            $parseFailed++
            continue
        }

        if ($occurredAt -ge $From -and $occurredAt -le $toValue) {
            $writer.WriteLine($line)
            $selected++
        }
    }
}
finally {
    $writer.Dispose()
}

Write-Output "원본: $resolvedInput"
Write-Output "출력: $resolvedOutput"
Write-Output "시간 범위(포함): $($From.ToString('o')) ~ $($toValue.ToString('o'))"
Write-Output "선택 건수: $selected"
Write-Output "JSON/시간 파싱 실패: $parseFailed"
