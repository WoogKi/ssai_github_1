[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Join-Path ([System.IO.Path]::GetTempPath()) ("nlq-time-range-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root | Out-Null
try {
    $extractor = Join-Path $PSScriptRoot 'extract_nlq_time_range.ps1'
    foreach ($scriptPath in @($extractor, $PSCommandPath)) {
        $scriptBytes = [System.IO.File]::ReadAllBytes($scriptPath)
        if ($scriptBytes.Length -lt 3 -or $scriptBytes[0] -ne 0xEF -or $scriptBytes[1] -ne 0xBB -or $scriptBytes[2] -ne 0xBF) {
            throw "PowerShell 5.1 한글 출력을 위해 UTF-8 BOM이 필요합니다: $scriptPath"
        }
    }
    $cases = Join-Path $root 'nlq_cases.jsonl'
    $feedback = Join-Path $root 'nlq_feedback.jsonl'
    $outputDirectory = Join-Path $root 'out'
    $explicitCaseOut = Join-Path $root 'cases_explicit.jsonl'

    @(
        '{"occurred_at":"2026-08-26T09:00:00+09:00","question":"한글 유지","marker":"case-from"}',
        '{"logged_at":"2026-08-26T09:30:00+09:00","marker":"case-fallback"}',
        '{"occurred_at":"not-a-time","marker":"case-bad-time"}',
        '{bad json',
        '{"occurred_at":"2026-08-26T10:00:00+09:00","marker":"case-to"}',
        '{"occurred_at":"2026-08-26T10:00:01+09:00","marker":"case-after"}'
    ) | Set-Content -LiteralPath $cases -Encoding utf8
    @(
        '{"occurred_at":"2026-08-26T09:15:00+09:00","feedback":"like","marker":"feedback-in"}',
        '{"created_at":"2026-08-26T10:00:00+09:00","feedback":"dislike","marker":"feedback-to"}',
        '{"timestamp":"2026-08-26T08:59:59+09:00","marker":"feedback-before"}'
    ) | Set-Content -LiteralPath $feedback -Encoding utf8

    $common = @{ From = '2026-08-26T09:00:00+09:00'; To = '2026-08-26T10:00:00+09:00' }
    $caseRun = & $extractor -InputPath $cases -OutputDirectory $outputDirectory @common
    $feedbackRun = & $extractor -InputPath $feedback -LogKind feedback -OutputDirectory $outputDirectory @common
    & $extractor -InputPath $cases -OutputPath $explicitCaseOut @common | Out-Null

    $caseOut = Join-Path $outputDirectory '20260826\nlq_cases_20260826_090000_to_20260826_100000.jsonl'
    $feedbackOut = Join-Path $outputDirectory '20260826\nlq_feedback_20260826_090000_to_20260826_100000.jsonl'
    if (-not (Test-Path -LiteralPath $caseOut -PathType Leaf) -or -not (Test-Path -LiteralPath $feedbackOut -PathType Leaf) -or -not (Test-Path -LiteralPath $explicitCaseOut -PathType Leaf)) {
        throw 'default date folder/file name or explicit output path was not created'
    }

    $caseLines = Get-Content -LiteralPath $caseOut -Encoding utf8
    $feedbackLines = Get-Content -LiteralPath $feedbackOut -Encoding utf8
    $expectedCases = @(
        '{"occurred_at":"2026-08-26T09:00:00+09:00","question":"한글 유지","marker":"case-from"}',
        '{"logged_at":"2026-08-26T09:30:00+09:00","marker":"case-fallback"}',
        '{"occurred_at":"2026-08-26T10:00:00+09:00","marker":"case-to"}'
    )
    if ($caseLines.Count -ne 3 -or [string]::Join("`n", $caseLines) -ne [string]::Join("`n", $expectedCases)) {
        throw "case extraction mismatch: $caseLines"
    }
    if ($feedbackLines.Count -ne 2 -or $feedbackLines[0] -notmatch 'feedback-in' -or $feedbackLines[1] -notmatch 'feedback-to') {
        throw "feedback extraction mismatch: $feedbackLines"
    }
    if ($caseLines[0] -notmatch '한글 유지') {
        throw 'UTF-8 Korean preservation failed'
    }
    if (([string]::Join("`n", $caseRun)) -notmatch 'JSON/시간 파싱 실패: 2') {
        throw "parse failure label/count mismatch: $caseRun"
    }
    if (([string]::Join("`n", $feedbackRun)) -notmatch 'JSON/시간 파싱 실패: 0') {
        throw "feedback parse failure label/count mismatch: $feedbackRun"
    }
    Write-Output 'RESULT: OK'
}
finally {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
