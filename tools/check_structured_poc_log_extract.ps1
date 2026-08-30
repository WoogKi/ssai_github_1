[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$extractor = Join-Path $PSScriptRoot 'extract_sims_diagnostics.ps1'
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('structured_poc_extract_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempRoot | Out-Null

try {
    $logPath = Join-Path $tempRoot 'fixture.log'
    $outputPath = Join-Path $tempRoot 'extract.txt'
    @'
[2026-08-30 20:00:00] INFO [ssai] [nlq.case] request_id=request-one action=품목별 매출 예상 result_status=success total_rows=5
[2026-08-30 20:00:00] INFO [ssai] [structured.response] schema=sims.response.v1 route=analytics result_status=success source_call_count=2 table_key=sims_one source_table_key=
[2026-08-30 20:00:00] INFO [ssai] [structured.presentation_poc] status=ready reason_code=schema_valid elapsed_ms=412 retry_count=0 tool_call_count=0
[2026-08-30 20:00:01] INFO [ssai] [chat.sims.push] user_id=1 room_id=room-one action=품목별 매출 예상 rows=5 cols=3 sig=abc123 count=1 table_key=sims_one
[2026-08-30 20:00:01] INFO [ssai] [sims.response_timing] action=품목별 매출 예상 message_id=message-one request_started_at=2026-08-30T20:00:00 response_completed_at=2026-08-30T20:00:01 elapsed_ms=1000 persisted=True restored=False
[2026-08-30 20:00:01] INFO [ssai] [nlq.trace.result] request_id=request-one question='sensitive prompt' action='품목별 매출 예상' result_status=success source_call_count=2 elapsed_ms=1000 total_elapsed_ms=1000
[2026-08-30 20:01:00] INFO [ssai] [nlq.case] request_id=request-two action=매출처별 매출 예상 result_status=no_data total_rows=0
[2026-08-30 20:01:00] INFO [ssai] [structured.presentation_poc] status=failed reason_code=timeout elapsed_ms=90000 retry_count=0 tool_call_count=0
[2026-08-30 20:01:01] INFO [ssai] [chat.sims.push] user_id=2 room_id=room-two action=매출처별 매출 예상 rows=0 cols=0 sig=def456 count=2 table_key=sims_two
[2026-08-30 20:01:01] INFO [ssai] [sims.response_timing] action=매출처별 매출 예상 message_id=message-two request_started_at=2026-08-30T20:01:00 response_completed_at=2026-08-30T20:01:01 elapsed_ms=1001 persisted=True restored=False
[2026-08-30 20:02:01] INFO [ssai] [chat.sims.push] user_id=3 room_id=room-three action=출고명세 조회 rows=3 cols=4 sig=ghi789 count=3 table_key=sims_three
[2026-08-30 20:02:01] INFO [ssai] [sims.response_timing] action=출고명세 조회 message_id=message-three request_started_at=2026-08-30T20:02:00 response_completed_at=2026-08-30T20:02:01 elapsed_ms=1002 persisted=True restored=False
[2026-08-30 20:02:01] INFO [ssai] password=never-log connection_string=never-log question='never-log'
'@ | Set-Content -LiteralPath $logPath -Encoding utf8

    & $extractor -LogPath $logPath -OutputPath $outputPath -StructuredPresentation | Out-Null
    $output = Get-Content -LiteralPath $outputPath -Raw -Encoding utf8

    foreach ($expected in @(
        'Request records count=3',
        'poc_status=ready',
        'poc_reason_code=schema_valid',
        'poc_elapsed_ms=412',
        'retry_count=0',
        'tool_call_count=0',
        'poc_status=failed',
        'poc_reason_code=timeout',
        'poc_status=not_observed',
        'result_status=success',
        'source_call_count=2',
        'business_elapsed_ms=1000',
        'correlation=message:',
        'table_key=table:'
    )) {
        if ($output -notmatch [regex]::Escape($expected)) {
            throw "missing expected extract value: $expected"
        }
    }
    foreach ($forbidden in @('sensitive prompt', 'never-log', 'request-one', 'room-one', 'message-one', 'sims_one')) {
        if ($output -match [regex]::Escape($forbidden)) {
            throw "sensitive value leaked: $forbidden"
        }
    }
    Write-Output 'STRUCTURED_POC_LOG_EXTRACT_PASS'
} finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
